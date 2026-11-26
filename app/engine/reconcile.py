"""Order reconciliation: unknown orders, and the full startup reconcile (QA section 3.3)."""

from __future__ import annotations

from ..broker.market_data import fresh_positions
import asyncio
import logging
from datetime import datetime

from ..broker import BrokerError
from ..eventhub import hub
from ..market import now_utc
from ..portfolio import ACTIVE, DONE_CANCELLED, parse_orders
from .util import _coid, _is_option_symbol

log = logging.getLogger("app.engine")


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import ReconcileHost as _Host
else:
    _Host = object

class ReconcileMixin(_Host):
    # -- unknown orders --------------------------------------------------------
    async def reconcile_orders(self) -> int:
        """Settle proposals whose order status is unknown (no reply from the broker) or
        stuck in 'submitting' (the app stopped while sending), by looking up their
        client_order_id at the broker. Runs at startup and on every supervisor tick.

        Found at the broker   -> placed (working or filled) or failed (cancelled/rejected).
        Never found           -> failed, once RECONCILE_GIVE_UP_MINUTES have passed.
        Bracket legs, and stops cancelled to make room for a sell, are then settled by
        the portfolio reconcile (status sync, stop restore), which handles those rows.
        """
        rows = self.db.unresolved_proposals()
        if not rows or self._exec_lock.locked():
            return 0
        try:
            open_text = await self.broker.open_orders_text()
            hist_text = await self.broker.order_history_text()
        except BrokerError as e:
            log.debug("reconcile_orders: %s", e)
            return 0
        orders = {o["client_order_id"]: o for o in parse_orders(hist_text)}
        orders.update({o["client_order_id"]: o for o in parse_orders(open_text)})
        changed = 0
        for p in rows:
            what = f"#{p['id']} {p['side']} {p['quantity']} {p['symbol']} @ {p['limit_price']}"
            stamp = p.get("placed_at") or p["created_at"]
            age_min = (now_utc() - datetime.fromisoformat(stamp)).total_seconds() / 60
            coid = p.get("client_order_id")
            o = orders.get(coid) if coid else None
            if o:
                st = (o.get("status") or "").upper()
                if st == "FILLED" or st in ACTIVE or st.startswith(("PENDING", "PARTIAL")):
                    self.db.update_proposal(p["id"], status="placed",
                                            reason=f"confirmed at the broker ({st})")
                    if p.get("side") == "BUY" and self.db.get_trade(p["id"]) is None:
                        # followup-correctness: execution.py records the ledger entry only when
                        # the send is acknowledged. A timed-out send settled here must get it
                        # too, or the working entry is invisible to risk.working_buys.
                        try:
                            sig = (self.db.get_signal(p.get("signal_id"))
                                   if p.get("signal_id") else None)
                            self.db.add_trade_entry(p["id"], p, sig)
                        except Exception:  # noqa: BLE001
                            log.exception("could not record the ledger entry for #%s", p["id"])
                    self.db.log("info", f"Order {what} confirmed at the broker: {st}",
                                alert=True)
                    if st == "FILLED":
                        self._publish_fill(p, o)
                elif st in DONE_CANCELLED:
                    self.db.update_proposal(p["id"], status="failed",
                                            reason=f"the broker reports {st}")
                    self.db.log("warn", f"Order {what} did not go through: broker reports {st}",
                                alert=True)
                else:
                    continue   # a status we don't know: look again next time
                changed += 1
            elif not coid and age_min > 2:
                self.db.update_proposal(p["id"], status="failed",
                                        reason="the app stopped before the order was sent")
                self.db.log("warn", f"Order {what} was never sent (the app stopped first)")
                changed += 1
            elif coid and age_min > self.s.reconcile_give_up_minutes:
                self.db.update_proposal(p["id"], status="failed",
                                        reason=f"not found at the broker after {age_min:.0f} min")
                for x in self.db.protective(p["symbol"]):
                    if x["proposal_id"] == p["id"] and x["status"] == "unknown":
                        self.db.set_protective(x["id"], "closed", "entry never reached the broker")
                self.db.log("warn", f"Order {what} was not found at the broker after "
                                    f"{age_min:.0f} min; marked failed", alert=True)
                changed += 1
        return changed

    @staticmethod
    def _publish_fill(p: dict, order: dict | None = None) -> None:
        """Publish a ``fill`` event: {"proposal_id", "symbol", "side", "quantity", "price",
        "client_order_id", "at"}. Never raises."""
        o = order or {}
        try:
            hub.publish("fill", {
                "proposal_id": p.get("id"), "symbol": p.get("symbol"), "side": p.get("side"),
                "quantity": o.get("filled_qty") or p.get("quantity"),
                "price": o.get("avg_fill_price") or o.get("filled_price") or p.get("limit_price"),
                "client_order_id": p.get("client_order_id") or o.get("client_order_id"),
                "at": now_utc().isoformat(timespec="seconds")})
        except Exception:  # noqa: BLE001
            log.debug("fill publish failed", exc_info=True)

    # -- startup (QA section 3.3) ------------------------------------------------
    async def startup_reconcile(self) -> dict:
        """Everything that must be settled before the schedulers start.

        1. settle unknown/submitting proposals by coid (``reconcile_orders``);
        2. adopt broker open orders this app has no record of as ``orphan`` rows, with an alert;
        3. bring protective-order statuses and the trade ledger up to date from the broker's
           orders and fills (the portfolio's existing sync);
        4. arm the OptionExitManager for every held option;
        5. check that every held equity has a protective stop and re-place a missing one when
           bracket orders are configured.

        Safe with the mock broker; every step is isolated, so a failing step is reported in the
        returned summary and the rest still run. Never raises.
        """
        summary: dict = {"settled": 0, "orphans": 0, "ledger_synced": False,
                         "options_armed": 0, "stops_replaced": 0, "unprotected": [], "errors": []}

        async def step(name: str, coro):
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one failing step must not stop startup
                summary["errors"].append(f"{name}: {type(e).__name__}: {e}")
                log.warning("startup_reconcile %s failed: %s", name, e)
                return None

        summary["settled"] = await step("reconcile_orders", self.reconcile_orders()) or 0
        summary["orphans"] = await step("adopt_orphans", self._adopt_orphan_orders()) or 0
        if self.portfolio is not None:
            await step("sync_statuses", self.portfolio.sync_statuses())
            tracked = await step("track_trade_fills", self.portfolio.track_trade_fills())
            summary["ledger_synced"] = tracked is not None
        armed = await step("arm_option_exits", self.option_exits.arm())
        summary["options_armed"] = len(armed or [])
        replaced = await step("verify_stops", self._verify_protective_stops(summary))
        summary["stops_replaced"] = replaced or 0
        self.db.log("info", "Startup reconcile: " + ", ".join(
            f"{k}={v}" for k, v in summary.items() if k not in ("errors", "unprotected"))
            + (f"; {len(summary['errors'])} step(s) failed" if summary["errors"] else ""))
        return summary

    async def _adopt_orphan_orders(self) -> int:
        """Broker open orders that no proposal or protective row knows: record them as
        ``orphan`` proposals (status only, never executed) and alert."""
        open_text = await self.broker.open_orders_text()
        known = self.db.known_client_order_ids()
        n = 0
        for o in parse_orders(open_text):
            coid = o.get("client_order_id")
            st = (o.get("status") or "").upper()
            if not coid or coid in known:
                continue
            if not (st in ACTIVE or st.startswith(("PENDING", "PARTIAL"))):
                continue
            sym = (o.get("symbol") or "?").upper()
            side = (o.get("side") or "?").upper()
            qty = o.get("quantity") or o.get("filled_qty") or 0
            price = o.get("limit_price") or o.get("stop_price") or 0.0
            row = {"symbol": sym, "side": side, "quantity": qty, "limit_price": price,
                   "confidence": None,
                   "rationale": f"Adopted at startup: open order {coid} not created by this app"}
            extra = {"client_order_id": coid, "decided_by": "startup_reconcile"}
            if _is_option_symbol(sym):
                extra["asset_class"] = "option"
            self.db.add_proposal(None, row, "orphan",
                                 f"open at the broker ({st}) but unknown to this app", price,
                                 **extra)
            known.add(coid)
            n += 1
            self.db.log("warn", f"ORPHAN order adopted: {side} {qty:g} {sym} @ {price} "
                                f"({st}, client_order_id {coid}). Review it in Webull.",
                        alert=True)
        return n

    def _stop_alert(self, sym: str, reason: str, level: str, message: str) -> None:
        """Log a stop problem, alerting once per (symbol, reason) until it clears; repeats are
        logged without an alert at most every 30 minutes."""
        seen = self.__dict__.setdefault("_stop_alerts", {})
        now = now_utc().timestamp()
        last = seen.get((sym, reason))
        if last is None:
            seen[(sym, reason)] = now
            self.db.log(level, message, alert=True)
        elif now - last >= 1800:
            seen[(sym, reason)] = now
            self.db.log("info", message, alert=False)

    def _stop_alerts_clear(self, held: set[str], sym: str | None = None) -> None:
        seen = self.__dict__.setdefault("_stop_alerts", {})
        for k in [k for k in seen if k[0] == sym or (sym is None and k[0] not in held)]:
            del seen[k]

    async def _verify_protective_stops(self, summary: dict | None = None) -> int:
        """Held equities with a known stop price but no stop at the broker or in our records get
        one re-placed (only when BRACKET_ORDERS is on). Positions with no known stop price are
        reported as unprotected and alerted."""
        if not self.s.bracket_orders:
            return 0
        if summary is None:
            summary = {"unprotected": []}
        positions = await fresh_positions(self.broker)
        held = {s: q for s, q in positions.items() if q > 0 and not _is_option_symbol(s)}
        self._stop_alerts_clear(set(held))                  # closed positions reset their alerts
        if not held:
            return 0
        open_orders = parse_orders(await self.broker.open_orders_text())
        working_sell = {(o.get("symbol") or "").upper() for o in open_orders
                        if (o.get("side") or "").upper() == "SELL"
                        and ((o.get("status") or "").upper() in ACTIVE
                             or (o.get("status") or "").upper().startswith(("PENDING", "PARTIAL")))}
        stop_by_symbol: dict[str, float] = {}
        trade_by_symbol: dict[str, dict] = {}
        for t in reversed(self.db.open_trades()):       # oldest first, newest wins
            if t.get("stop_price") and t.get("side") == "BUY":
                stop_by_symbol[t["symbol"]] = float(t["stop_price"])
                trade_by_symbol[t["symbol"]] = t
        if self.portfolio is not None:
            # B1: an 'unknown' stop row that the broker has never shown is not an armed stop.
            # Needs the order history too: if it cannot be read, nothing is judged gone.
            open_ids = {o.get("client_order_id") for o in open_orders}
            try:
                hist_ids = {o.get("client_order_id")
                            for o in parse_orders(await self.broker.order_history_text())}
            except Exception as exc:  # noqa: BLE001 - incl. brokers without a history call
                log.debug("order history unavailable (stale-unknown check skipped): %s", exc)
                hist_ids = None
            for x in (self.db.protective() if hist_ids is not None else []):
                if x["kind"] == "sl" and self.portfolio.stale_unknown(x, open_ids, hist_ids):
                    self.db.set_protective(x["id"], "closed",
                                           "never appeared at the broker (no reply to the placement)")
                    self.db.log("warn", f"{x['symbol']}: the stop placement got no reply and never "
                                        "showed at the broker; treated as not armed, re-placing.")
            # a resting stop above its entry fill and the price is replaced in two phases
            await self.portfolio.correct_stale_stops(held)
            for sym_, t_ in trade_by_symbol.items():     # a pending re-anchor: its stop, not the old one
                m_ = self.db.get_kv(f"reanchor_pending:{t_.get('proposal_id')}")
                if m_:
                    stop_by_symbol[sym_] = float(m_["sl"])
        core = self.s.core_symbol.upper() if self.s.core_enabled else None
        replaced = 0
        for sym, qty in sorted(held.items()):
            if sym.upper() == core:
                continue
            armed = [x for x in self.db.protective(sym) if x["kind"] == "sl"]
            if armed or sym.upper() in working_sell:
                self._stop_alerts_clear(set(held), sym)
                continue
            stop = stop_by_symbol.get(sym)
            if not stop:
                summary.setdefault("unprotected", []).append(sym)
                self._stop_alert(sym, "no_stop", "error",
                                 f"{sym}: {qty:g} share(s) held with NO protective stop and "
                                 "no known stop price. Set one in Webull.")
                continue
            if self.portfolio is not None and sym in trade_by_symbol:
                t = trade_by_symbol[sym]
                stop, _tp, _ch = await self.portfolio.reanchor_stop(t, t.get("entry_fill_price"), stop)
            # followup-correctness: exponential backoff per symbol after a rejected re-place
            # (2.5 -> 5 -> 10 -> 30 min max); reset on success or when the position size changes.
            backoff = self.__dict__.setdefault("_stop_backoff", {})
            bk = backoff.get(sym)
            if bk and bk["qty"] != qty:
                backoff.pop(sym, None)
                bk = None
            if bk and now_utc().timestamp() < bk["next"]:
                summary.setdefault("unprotected", []).append(sym)
                continue
            if self.portfolio is not None:
                live = await self.portfolio.stop_above_market(sym, float(stop))
                if live:    # S7: a sell stop at/above the market: exit instead, once, with an alert
                    await self.portfolio.exit_position(
                        sym, qty, live, f"stop {float(stop):.2f} >= price {live:.2f}")
                    continue
            coid = _coid()
            row = self.db.add_protective(None, sym, "sl", coid, qty, stop, "unknown",
                                         "re-placed by protective stop check: no stop was found")
            try:
                await self.broker.place_stop(sym, qty, stop, coid, allow_when_killed=True)
            except BrokerError as e:
                if not e.transport:
                    self.db.set_protective(row, "closed", f"stop not placed: {e}")
                fails = (bk["fails"] if bk else 0) + 1
                delay = min(1800.0, 150.0 * 2 ** (fails - 1))
                backoff[sym] = {"qty": qty, "fails": fails,
                                "next": now_utc().timestamp() + delay}
                summary.setdefault("unprotected", []).append(sym)
                self._stop_alert(sym, "replace_failed", "error",
                                 f"{sym}: could not re-place the stop at {stop}: {e}. "
                                 "Set one in Webull.")
                continue
            self.db.set_protective(row, "active", "re-placed by protective stop check: no stop was found")
            backoff.pop(sym, None)
            self._stop_alerts_clear(set(held), sym)
            replaced += 1
            self.db.log("warn", f"{sym}: no protective stop was found; re-placed a stop for "
                                f"{qty:g} at {stop}", alert=True)
        return replaced
