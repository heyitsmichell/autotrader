"""Entry chase (ENTRY_CHASE_SECONDS): re-send an unfilled entry once as a marketable limit.

Runs from the risk loop. In the CORE session only: when an entry LIMIT BUY has not filled at all
N seconds after it was placed, and the live price is still inside the price band of the
proposal's reference price, the order is cancelled and re-sent ONCE as a marketable limit (same
quantity, new client_order_id) on the same proposal and ledger row, so working-entry checks and
fill tracking keep working.

Safety rules (each one has a test):
  * never when the order has any fill (partial or full), or when the cancel is not confirmed;
  * never with the kill switch on, outside CORE, or when the stop would be at/above the price;
  * never re-buy an order somebody else cancelled: the chase only continues from its own
    cancel marker (kv ``chase_cancel:<proposal id>``, written BEFORE the cancel is sent);
  * exactly once per proposal: kv ``chase:<proposal id>`` is written BEFORE the new order is sent,
    so a restart (or a crash between the cancel and the send) can never produce a second buy.
    In that crash case the entry is simply lost (fill tracking closes the cancelled trade row).
"""

from __future__ import annotations

from ..broker.market_data import fresh_positions
import asyncio
import logging
import time
from datetime import datetime

from ..broker import BrokerError
from ..market import now_utc, order_session
from ..order_style import cancel_reply_confirmed, marketable_buy_price, within_band
from ..risk import reprice_violation
from ..portfolio import ACTIVE, DONE_CANCELLED, parse_orders
from .util import _coid, _is_option_symbol

log = logging.getLogger("app.engine")

CONFIRM_TRIES = 4
CONFIRM_DELAY_SECONDS = 0.5
RETRY_SECONDS = 15.0


def _age_seconds(row: dict) -> float:
    stamp = row.get("placed_at") or row.get("created_at")
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now_utc().tzinfo)
        return (now_utc() - dt).total_seconds()
    except (TypeError, ValueError):
        return 0.0


class EntryChaser:
    def __init__(self, engine) -> None:
        self.eng = engine
        self.chased = 0
        self._retry_at: dict[int, float] = {}

    # -- candidates ------------------------------------------------------------------------
    def candidates(self) -> list[tuple[dict, dict]]:
        eng = self.eng
        secs = float(getattr(eng.s, "entry_chase_seconds", 0) or 0)
        if secs <= 0:
            return []
        now = time.monotonic()
        out = []
        for t in eng.db.open_trades():
            pid = t.get("proposal_id")
            if (not pid or t.get("side") != "BUY" or t.get("entry_fill_price") is not None
                    or _is_option_symbol(t["symbol"])):
                continue
            if eng.db.get_kv(f"chase:{pid}") or self._retry_at.get(pid, 0.0) > now:
                continue
            prop = eng.db.get_proposal(pid)
            if (not prop or prop.get("status") != "placed" or not prop.get("client_order_id")
                    or _age_seconds(prop) < secs):
                continue
            out.append((t, prop))
        return out

    async def _orders(self) -> dict[str, dict]:
        b = self.eng.broker
        orders = {o["client_order_id"]: o for o in parse_orders(await b.order_history_text())}
        orders.update({o["client_order_id"]: o for o in parse_orders(await b.open_orders_text())})
        return orders

    # -- the decision -----------------------------------------------------------------------
    async def _plan(self, prop: dict) -> float | None:
        """The marketable price to chase at, or None when a safety rule says not to."""
        eng, sym = self.eng, prop["symbol"]
        if eng.kill_switch or order_session() != "CORE":
            return None
        try:
            q = await eng.broker.quote(sym)
        except BrokerError:
            return None
        if q is None or not q.last:
            return None
        ref = prop.get("ref_price") or prop["limit_price"]
        if not within_band(eng.s, q.last, ref):
            return None
        px = marketable_buy_price(eng.s, last=q.last, ask=q.ask, ref_price=ref)
        if px is None:
            return None
        stop = prop.get("sl")
        if stop and float(stop) >= min(px, float(q.last)):
            return None
        # re-run the per-order / risk-per-trade / position / target checks at the NEW price
        try:
            held = (await fresh_positions(eng.broker)).get(sym, 0.0)
        except BrokerError:
            return None
        why = reprice_violation(eng.s, qty=float(prop["quantity"]), px=px, sl=prop.get("sl"),
                                tp=prop.get("tp"), held=held, last=float(q.last))
        if why:
            eng.db.log("info", f"{sym}: entry chase not re-priced: {why}")
            return None
        return px

    async def run_once(self) -> list[int]:
        """One pass; returns the proposal ids that were re-sent."""
        eng = self.eng
        if order_session() != "CORE":
            return []
        cands = self.candidates()
        if not cands:
            return []
        orders = await self._orders()
        done: list[int] = []
        for t, prop in cands:
            try:
                if await self._chase_one(t, prop, orders):
                    done.append(prop["id"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one entry must not stop the others
                log.exception("entry chase for %s failed", prop.get("symbol"))
                self._retry_at[prop["id"]] = time.monotonic() + RETRY_SECONDS
        return done

    async def _chase_one(self, t: dict, prop: dict, orders: dict[str, dict]) -> bool:
        eng, pid, sym = self.eng, prop["id"], prop["symbol"]
        coid = prop["client_order_id"]
        marker = f"chase_cancel:{pid}"
        o = orders.get(coid)
        if o is None:
            return False
        status = (o.get("status") or "").upper()
        filled = float(o.get("filled_qty") or 0.0)
        if filled > 0 or status.startswith("PARTIAL"):
            return False                                    # never chase a (partly) filled entry
        if status in DONE_CANCELLED:
            if not eng.db.get_kv(marker):
                return False                                # cancelled by someone else: leave it
        elif status in ACTIVE:
            if await self._plan(prop) is None:
                return False
            eng.db.set_kv(marker, {"coid": coid, "at": time.time()})   # BEFORE the cancel
            try:
                reply = await eng.broker.cancel_order(coid)
                if not cancel_reply_confirmed(reply):
                    # "Order not found..." / "Permission denied...": not a confirmed cancel. Do not
                    # send a replacement; the status sync / reconcile settles the old order.
                    eng.db.log("warn", f"{sym}: entry chase: cancel of {coid} not confirmed "
                                       f"({str(reply)[:80]}); not chasing")
                    self._retry_at[pid] = time.monotonic() + RETRY_SECONDS
                    return False
            except BrokerError as e:
                eng.db.log("warn", f"{sym}: entry chase: cancel of {coid} not confirmed ({e}); "
                                   "not chasing")
                self._retry_at[pid] = time.monotonic() + RETRY_SECONDS
                return False
            confirmed = False
            for _ in range(CONFIRM_TRIES):
                await asyncio.sleep(CONFIRM_DELAY_SECONDS)
                cur = (await self._orders()).get(coid) or {}
                st = (cur.get("status") or "").upper()
                if float(cur.get("filled_qty") or 0.0) > 0 or st == "FILLED" or st.startswith("PARTIAL"):
                    return False                            # it filled while cancelling
                if st in DONE_CANCELLED:
                    confirmed = True
                    break
            if not confirmed:
                self._retry_at[pid] = time.monotonic() + RETRY_SECONDS
                return False
        else:
            return False                                    # FILLED or an unknown state

        # The old order is cancelled with no fill. Re-check every rule, then send exactly once.
        px = await self._plan(prop)
        if px is None:
            eng.db.set_kv(f"chase:{pid}", {"aborted": True, "at": time.time()})
            eng.db.log("warn", f"{sym}: entry chase aborted after the cancel (kill switch, price "
                               "out of band or stop too close); the entry is not re-sent.",
                       alert=True)
            return False
        eng.db.set_kv(f"chase:{pid}", {"old_coid": coid, "price": px, "at": time.time()})
        return await self._resend(t, prop, px)

    async def _resend(self, t: dict, prop: dict, px: float) -> bool:
        eng, pid, sym = self.eng, prop["id"], prop["symbol"]
        qty, old = prop["quantity"], prop["client_order_id"]
        # The fill tracker may have closed the ledger row ('cancelled') while we waited for our own
        # cancel to confirm. Re-open it atomically with the new limit BEFORE anything is sent; if it
        # cannot be re-opened the fill would never be tracked / protected, so do not send.
        if not eng.db.reopen_trade_for_chase(pid, px):
            eng.db.set_kv(f"chase:{pid}", {"aborted": True, "reason": "ledger row not reopenable",
                                           "at": time.time()})
            eng.db.log("error", f"{sym}: entry chase aborted: the ledger row for proposal #{pid} "
                                "could not be re-opened; the entry is not re-sent.", alert=True)
            return False
        old_legs = [r for r in eng.db.protective(sym)
                    if r.get("proposal_id") == pid and r["status"] == "waiting"]
        bracket = bool(old_legs and eng.s.bracket_orders and prop.get("sl") and prop.get("tp"))
        for r in old_legs:                                  # best effort: the combo cancel took them
            try:
                await eng.broker.cancel_order(r["client_order_id"])
            except BrokerError:
                pass
            eng.db.set_protective(r["id"], "closed", "entry chased: bracket re-sent")
        coids = {"entry": _coid(), "tp": _coid(), "sl": _coid()}
        eng.db.update_proposal(pid, client_order_id=coids["entry"], limit_price=px,
                               order_type="chase",
                               placed_at=now_utc().isoformat(timespec="seconds"))
        rows: list[int] = []
        try:
            if bracket:
                combo = _coid()
                rows = eng._record_bracket(pid, prop, coids, status="unknown", combo_id=combo)
                response = await eng.broker.place_bracket(
                    sym, qty, px, prop["tp"], prop["sl"], coids, combo, trading_session="CORE")
            else:
                response = await eng.broker.place_order(sym, "BUY", qty, px, coids["entry"],
                                                        trading_session="CORE")
        except BrokerError as e:
            if e.transport:
                eng.db.update_proposal(
                    pid, status="unknown", broker_response=str(e),
                    reason=f"Entry chase: no reply from broker. Check open orders in Webull for "
                           f"client_order_id {coids['entry']}.")
                eng.db.log("error", f"{sym}: entry chase status UNKNOWN: {e}", alert=True)
            else:
                eng._settle_bracket_rows(rows, "closed", "chase order rejected")
                eng.db.update_proposal(pid, status="failed", broker_response=str(e),
                                       reason="entry chase rejected by broker")
                eng.db.cancel_trade(pid, "chase_rejected")
                eng.db.log("error", f"{sym}: entry chase rejected by broker: {e}", alert=True)
            return True
        eng.db.update_proposal(pid, broker_response=response)
        if rows:
            eng._settle_bracket_rows(rows, "waiting", "waiting for the entry to fill")
        self.chased += 1
        eng.db.log("warn", f"ENTRY CHASE {sym}: {qty} not filled after "
                           f"{eng.s.entry_chase_seconds:g}s; {old} cancelled, re-sent as a "
                           f"marketable limit @ {px}", alert=True)
        return True
