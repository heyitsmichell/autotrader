"""Account data as structured JSON for the dashboard, an equity curve, and the
status of the protective (stop-loss / take-profit) orders this app placed.

Parsers read the text the Webull MCP server prints (formatters.py in
webull-openapi-mcp); the mock broker prints the same format.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

from .broker import Broker, BrokerError, _num
from .config import Settings
from .db import DB, _now
from .market import market_is_open, now_utc, order_session, start_of_today_et_utc
from .order_style import exit_should_be_market
from .autopsy import run_autopsy, format_autopsy_for_llm

log = logging.getLogger(__name__)

BALANCE_FIELDS = {
    "currency": "Currency", "cash": "Total Cash Balance", "market_value": "Total Market Value",
    "unrealized": "Total Unrealized P&L", "net_liq": "Net Liquidation", "day_pnl": "Day P&L",
    "buying_power": "Buying Power",
}
ACTIVE = {"SUBMITTED", "PARTIAL_FILLED", "PARTIALLY_FILLED", "WORKING", "NEW", "ACCEPTED"}
DONE_CANCELLED = {"CANCELLED", "CANCELED", "FAILED", "REJECTED", "EXPIRED"}
STALE_UNKNOWN_SECONDS = 300.0       # an 'unknown' stop row this old must show up at the broker
EXIT_RETRY_SECONDS = 900.0          # one exit order per symbol per window
REANCHOR_RETRY_SECONDS = 300.0      # stale-stop cancel attempts per trade
# cancel replies that are NOT a confirmation (the MCP returns them as plain text, not an error)
UNCONFIRMED_CANCEL = ("not found", "may have been filled", "permission denied")


def balance_is_empty(b: dict | None) -> bool:
    """True for a balance the broker could not really have meant: every money field missing
    or zero. A real account never reports zero net liquidation, zero cash and zero buying
    power at once, but the broker does answer that way while its connection is being
    re-established. Storing it wrecks the equity curve, the P&L and the drawdown peak."""
    if not b:
        return True
    return all(not (b.get(k) or 0) for k in ("net_liq", "cash", "market_value", "buying_power"))


def parse_balance(text: str) -> dict:
    out: dict = {}
    for key, label in BALANCE_FIELDS.items():
        m = re.search(rf"^\s*{re.escape(label)}:\s*(\S+)", text, re.M)
        if m:
            out[key] = m.group(1) if key == "currency" else _num(m.group(1))
    if out.get("net_liq") is None and out.get("cash") is not None:
        out["net_liq"] = (out.get("cash") or 0) + (out.get("market_value") or 0)
    return out


def parse_position_rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if "Leg:" in line or "Symbol:" not in line:
            continue
        kind = re.search(r"\bType:\s*(\S+)", line)
        if kind and kind.group(1).upper() not in {"EQUITY", "STOCK", "ETF"}:
            continue

        def field(label: str, pattern: str = r"(-?[\d.,]+)"):
            m = re.search(rf"(?<!Available )\b{re.escape(label)}:\s*{pattern}", line)
            return m.group(1) if m else None

        sym, qty = field("Symbol", r"(\S+)"), _num(field("Qty") or "")
        if not sym or qty is None:
            continue
        cost, last = _num(field("Cost") or ""), _num(field("Last") or "")
        mv = _num(field("Market Value") or "")
        upl = _num(field("Unrealized P&L") or "")
        if mv is None and last is not None:
            mv = round(qty * last, 2)       # the US API does not report market value
        if upl is None and None not in (last, cost):
            upl = round((last - cost) * qty, 2)
        rows.append({"symbol": sym.upper(), "qty": qty, "cost": cost, "last": last,
                     "market_value": mv, "unrealized": upl,
                     "unrealized_pct": round((last / cost - 1) * 100, 3)
                     if last and cost else None})
    return rows


_ORDER_KEYS = {
    "Order ID": "order_id", "Symbol": "symbol", "Side": "side", "Status": "status",
    "Order Type": "order_type", "Total Quantity": "quantity", "Filled Quantity": "filled_qty",
    "Limit Price": "limit_price", "Stop Price": "stop_price", "Filled Price": "filled_price",
    "Time In Force": "tif", "Place Time": "placed_at", "Filled Time": "filled_at",
    "Average Fill Price": "avg_fill_price", "Commission": "commission",
    "Combo Type": "combo_type", "Client Order ID": "client_order_id",
}
_NUMERIC = {"quantity", "filled_qty", "limit_price", "stop_price", "filled_price", "avg_fill_price", "commission"}


def parse_orders(text: str) -> list[dict]:
    """Every order (including each leg of a combo) from get_open_orders / history."""
    orders: list[dict] = []
    group, combo, cur = None, None, None
    for line in text.splitlines():
        if m := re.match(r"^Client Order ID:\s*(\S+)", line):
            group, cur = m.group(1), None
        elif m := re.match(r"^Combo Type:\s*(\S+)", line):
            combo = m.group(1)
        elif m := re.match(r"^\s{2,}Client Order ID:\s*(\S+)", line):
            cur = {"client_order_id": m.group(1), "group_id": group, "combo_type": combo}
            orders.append(cur)
        elif cur is not None and (m := re.match(r"^\s{2,}([A-Za-z ]+?):\s*(.*)$", line)):
            key = _ORDER_KEYS.get(m.group(1).strip())
            if key:
                value = m.group(2).strip()
                value = None if value in ("", "N/A", "None") else value
                cur[key] = _num(value) if key in _NUMERIC and value else value
    return orders


def _age(row: dict) -> float:
    """Seconds since a protective-order row last changed."""
    stamp = datetime.fromisoformat(row.get("updated_at") or row["created_at"])
    return (now_utc() - stamp).total_seconds()


class Portfolio:
    def __init__(self, settings: Settings, db: DB, broker: Broker) -> None:
        self.s, self.db, self.broker = settings, db, broker
        self._cache: dict | None = None
        self._cache_at = 0.0
        self._last_good_balance: dict | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def snapshot(self, max_age: float = 5.0) -> dict:
        """Balance, positions and open orders (cached a few seconds)."""
        async with self._lock:
            if self._cache and time.time() - self._cache_at < max_age:
                return self._cache
            out: dict = {"errors": {}}
            for key, fn in (("balance", self.broker.balance_text),
                            ("positions", self.broker.positions_text),
                            ("orders", self.broker.open_orders_text)):
                try:
                    out[key] = await fn()
                except BrokerError as e:
                    out[key] = ""
                    out["errors"][key] = str(e)
            balance = parse_balance(out["balance"])
            if balance_is_empty(balance):
                # Keep showing the last real balance (flagged) instead of a $0 account and a
                # $0 day P&L; there is nothing sensible to show before the first good read.
                if self._last_good_balance is not None:
                    balance = {**self._last_good_balance, "stale": True}
                out["errors"].setdefault(
                    "balance", "broker returned an empty (all-zero) balance; "
                               + ("showing the last good values" if balance.get("stale")
                                  else "no earlier balance to fall back on"))
            else:
                self._last_good_balance = balance
            snap = {
                "balance": balance,
                "positions": parse_position_rows(out["positions"]),
                "open_orders": parse_orders(out["orders"]),
                "errors": out["errors"],
                "raw": {k: out[k] for k in ("balance", "positions", "orders")},
            }
            self._cache, self._cache_at = snap, time.time()
            return snap

    def invalidate(self) -> None:
        self._cache = None

    @property
    def cached(self) -> dict | None:
        return self._cache

    # -- protective orders ---------------------------------------------------------
    async def _orders(self) -> tuple[dict, dict]:
        open_orders = {o["client_order_id"]: o for o in
                       parse_orders(await self.broker.open_orders_text())}
        history = {o["client_order_id"]: o for o in
                   parse_orders(await self.broker.order_history_text())}
        return open_orders, history

    def _sync(self, open_orders: dict, history: dict) -> int:
        """waiting -> active -> filled / cancelled, as the broker reports it."""
        changed = 0
        for p in self.db.protective(open_only=True):
            o = open_orders.get(p["client_order_id"]) or history.get(p["client_order_id"])
            if not o:
                # B1: an 'unknown' row (placement got no reply) the broker never showed must not
                # count as an armed stop for a day: give up after the reconcile window.
                give_up = max(300.0, float(getattr(self.s, "reconcile_give_up_minutes", 15)) * 60)
                if p["status"] == "unknown" and _age(p) > give_up:
                    self.db.set_protective(p["id"], "closed", "never appeared at the broker")
                    changed += 1
                continue
            status = (o.get("status") or "").upper()
            new = ("active" if status in ACTIVE else
                   "filled" if status == "FILLED" else
                   "cancelled" if status in DONE_CANCELLED else
                   "waiting" if status.startswith("PENDING") else None)
            if new and new != p["status"]:
                self.db.set_protective(p["id"], new)
                changed += 1
                if new == "filled":
                    what = "Stop loss" if p["kind"] == "sl" else "Take profit"
                    level = "warn" if p["kind"] == "sl" else "info"
                    self.db.log(level, f"{what} filled at Webull: SELL {p['quantity']:g} "
                                       f"{p['symbol']} @ {o.get('filled_price') or p['price']}")
        return changed

    async def sync_statuses(self) -> int:
        """Bring protective-order statuses up to date. Raises BrokerError on failure."""
        if not self.db.protective(open_only=True):
            return 0
        changed = self._sync(*await self._orders())
        if changed:
            self.invalidate()
        return changed

    async def reconcile(self, min_age_seconds: float = 120) -> int:
        """Keep the protective orders this app placed in line with the broker.

        1. Status sync (see _sync).
        2. Orphans: an armed stop or target whose position is gone is cancelled, so it
           can never sell shares you don't hold.
        3. Restores: a stop that was cancelled to make room for a sell is put back if
           that sell ended without filling (expired, cancelled or rejected).

        Steps 2 and 3 only act on rows older than min_age_seconds, so a broker that is
        a moment behind on positions or orders can't trigger a wrong move.
        """
        if not self.db.protective(open_only=True) and not self.db.restorable_stops():
            return 0
        try:
            open_orders, history = await self._orders()
            changed = self._sync(open_orders, history)
            armed = [p for p in self.db.protective(open_only=True) if p["status"] == "active"]
            pending = self.db.restorable_stops()
            if armed or pending:
                held = await self.broker.positions()
                changed += await self._cancel_orphans(armed, held, min_age_seconds)
                changed += await self._restore_stops(pending, held, open_orders, history,
                                                     min_age_seconds)
        except BrokerError as e:
            log.debug("reconcile failed: %s", e)
            return 0
        if changed:
            self.invalidate()
        return changed

    async def _cancel_orphans(self, armed: list[dict], held: dict, min_age: float) -> int:
        n = 0
        for p in armed:
            if held.get(p["symbol"], 0) > 0 or _age(p) < min_age:
                continue
            try:
                await self.broker.cancel_order(p["client_order_id"])
            except BrokerError as e:
                if e.transport:
                    continue   # try again next round
            kind = "stop" if p["kind"] == "sl" else "target"
            self.db.set_protective(p["id"], "cancelled", "position closed; nothing to protect")
            self.db.log("warn", f"Cancelled the {p['symbol']} {kind}: the position is closed")
            n += 1
        return n

    async def _restore_stops(self, pending: list[dict], held: dict, open_orders: dict,
                             history: dict, min_age: float) -> int:
        n = 0
        for r in pending:
            sell = open_orders.get(r["sell_coid"]) or history.get(r["sell_coid"])
            if sell is None:
                if _age(r) > 3 * 86_400:
                    self.db.settle_stops(r["sell_coid"], "sell never appeared at the broker")
                continue
            status = (sell.get("status") or "").upper()
            if status == "FILLED":
                self.db.settle_stops(r["sell_coid"], "cancelled before a sell that filled")
                continue
            if status not in DONE_CANCELLED or _age(r) < min_age:
                continue   # the sell is still working, or too fresh to act on
            sym = r["symbol"]
            covered = sum(x["quantity"] for x in self.db.protective(sym)
                          if x["kind"] == "sl" and x["status"] == "active")
            gap = held.get(sym, 0) - covered
            # Settle this row before placing anything: at most one restore attempt per stop.
            self.db.set_protective(
                r["id"], "cancelled", "sell did not fill; stop restored" if gap > 0
                else "sell did not fill; shares already protected", sell_coid=None)
            n += 1
            if gap <= 0:
                continue
            qty, coid = min(gap, r["quantity"]), uuid.uuid4().hex
            try:
                await self.broker.place_stop(sym, qty, r["price"], coid)
                self.db.add_protective(r["proposal_id"], sym, "sl", coid, qty, r["price"],
                                       "active", "restored after a sell that did not fill")
                self.db.log("warn", f"The {sym} sell ended without filling; stop restored for "
                                    f"{qty:g} shares at {r['price']}")
            except BrokerError as e:
                self.db.log("error", f"Could not restore the stop for {qty:g} {sym} at "
                                     f"{r['price']}: {e}. Set one in Webull.")
        return n

    async def _compute_mae_mfe(self, symbol: str, entry_price: float,
                               entry_time: datetime, exit_time: datetime) -> tuple[float, float]:
        """
        Compute MAE (Maximum Adverse Excursion) and MFE (Maximum Favorable Excursion)
        from intraday bars during the trade hold period.

        Returns (mae_pct, mfe_pct) as percentages.
        """
        try:
            # Use 5-minute bars for reasonable granularity
            bars = await self.broker.intraday_bars(symbol, timespan="5m", count=390)
        except Exception:
            # Fallback: return simple entry/exit based values
            return 0.0, 0.0

        if not bars or entry_price <= 0:
            return 0.0, 0.0

        # Convert entry/exit times to UTC for comparison
        entry_ts = entry_time.timestamp()
        exit_ts = exit_time.timestamp()

        # Find intraday bars that fall within the trade hold period
        trade_bars = []
        for bar in bars:
            try:
                bar_time = bar["time"]
                if isinstance(bar_time, str):
                    bar_time = datetime.fromisoformat(bar_time.replace('Z', '+00:00'))
                if entry_ts <= bar_time.timestamp() <= exit_ts:
                    trade_bars.append(bar)
            except Exception:
                continue

        if not trade_bars:
            return 0.0, 0.0

        # For long trades: MAE = max drawdown from entry, MFE = max run-up from entry
        # MAE = min(0, (low - entry) / entry * 100)
        # MFE = max(0, (high - entry) / entry * 100)
        min_low = min(b["low"] for b in trade_bars)
        max_high = max(b["high"] for b in trade_bars)

        mae = min(0.0, (min_low - entry_price) / entry_price * 100)
        mfe = max(0.0, (max_high - entry_price) / entry_price * 100)

        return round(mae, 2), round(mfe, 2)

    # -- stop re-anchoring (fill landed at/below the planned stop) -----------------
    def _entry_atr(self, trade: dict) -> float | None:
        """ATR for a trade: the ledger row, then the latest stored signal for the symbol."""
        try:
            atr = trade.get("entry_atr")
            if atr and float(atr) > 0:
                return float(atr)
            for sg in self.db.signals(trade["symbol"], 1):
                if sg.get("atr") and float(sg["atr"]) > 0:
                    return float(sg["atr"])
        except Exception:  # noqa: BLE001
            log.debug("ATR lookup failed for %s", trade.get("symbol"), exc_info=True)
        return None

    def _reanchored_levels(self, trade: dict, fill: float) -> tuple[float, float]:
        """Stop/target anchored on the fill: ta.levels with the ATR; without any ATR, the stop
        sits MAX_SL_PCT below the fill (the widest allowed distance) and the target keeps the
        configured TP:SL ratio."""
        from . import ta
        sl, tp = ta.levels(fill, self._entry_atr(trade), self.s)
        if not sl:
            dist = fill * self.s.max_sl_pct / 100
            sl = round(fill - dist, 2)
            tp = round(fill + dist * (self.s.tp_atr_mult / self.s.sl_atr_mult), 2)
        return sl, tp

    async def reanchor_stop(self, trade: dict, fill_price: float | None,
                            stop: float | None = None) -> tuple[float | None, float | None, bool]:
        """Return (stop, target, changed) for a filled long entry. Only when the FILL is at or
        below the planned stop (that stop would trigger at once) are stop and target re-anchored
        from the fill and persisted on the ledger row (+ an alert). A fill above the stop with a
        price now below it is a normal stop-out: the stop is never widened. Never moves a stop
        upward. Never raises."""
        planned = float(stop if stop is not None else (trade.get("stop_price") or 0)) or None
        target = trade.get("target_price")
        try:
            sym = trade["symbol"]
            if not planned or not fill_price or trade.get("side") != "BUY":
                return planned, target, False
            fill = float(fill_price)
            if not fill <= planned:
                return planned, target, False
            sl, tp = self._reanchored_levels(trade, fill)
            if not sl or sl >= planned:                      # never re-anchor upward
                return planned, target, False
            if trade.get("proposal_id") is not None:
                self.db.update_trade_levels(trade["proposal_id"], sl, tp)
                self.db.set_kv(f"reanchored:{trade['proposal_id']}", {"stop": sl, "from": planned})
            self.db.log("warn", f"{sym}: fill ${fill:.2f} was below the planned stop ${planned:.2f}; "
                                f"stop re-anchored to ${sl:.2f} (target ${tp:.2f})", alert=True)
            try:
                live = await self.broker.last_price(sym)
            except Exception:  # noqa: BLE001
                live = None
            if live and live <= sl:
                self.db.log("warn", f"{sym}: price ${live:.2f} already below the re-anchored stop "
                                    f"${sl:.2f} - exit managed by the soft stop/at the open",
                            alert=True)
            return sl, tp, True
        except Exception:  # noqa: BLE001
            log.exception("could not re-anchor the stop for %s", trade.get("symbol"))
            return planned, target, False

    # -- exits instead of impossible stops (S7) ------------------------------------------
    async def stop_above_market(self, sym: str, stop: float) -> float | None:
        """In the CORE session, the live price when a sell stop at ``stop`` would sit at or above
        it (the broker may reject it, or it would trigger at once); else None."""
        try:
            if order_session() != "CORE":
                return None
            live = await self.broker.last_price(sym)
        except Exception:  # noqa: BLE001 - no live price: place the stop as usual
            return None
        return float(live) if live and float(stop) >= float(live) else None

    async def exit_position(self, sym: str, qty: float, ref: float, why: str,
                            session: str = "CORE") -> bool:
        """Sell the held shares now (MARKET in CORE when EXIT_ORDER_TYPE=market and the quantity
        is whole, else a marketable LIMIT 0.5% under ``ref``). At most once per EXIT_RETRY window
        per symbol, always passes the kill switch, alerts. Returns True when an order is out."""
        if qty <= 0 or not ref or ref <= 0:
            return False
        key = f"core_exit:{sym}"
        prev = self.db.get_kv(key)
        if isinstance(prev, dict) and time.time() - float(prev.get("at", 0)) < EXIT_RETRY_SECONDS:
            return False
        from .engine.util import _coid
        coid = _coid()
        market = float(qty) == int(qty) and exit_should_be_market(self.s, session)
        limit = max(0.01, round(float(ref) * 0.995, 2))
        self.db.set_kv(key, {"coid": coid, "at": time.time(), "why": why})
        row = self.db.add_protective(None, sym, "sl", coid, qty, limit, "unknown", f"exit: {why}")
        try:
            await self.broker.place_order(sym, "SELL", qty, limit, coid, trading_session=session,
                                          allow_when_killed=True,
                                          order_type="MARKET" if market else "LIMIT")
        except BrokerError as e:
            if e.transport:
                self.db.log("error", f"{sym}: exit sell got no reply ({e}). Check Webull.", alert=True)
                return True
            self.db.set_protective(row, "closed", f"exit rejected: {e}")
            self.db.set_kv(key, None)
            self.db.log("error", f"{sym}: exit sell rejected ({e}) - {why}. Sell it in Webull.",
                        alert=True)
            return False
        self.db.set_protective(row, "active", f"exit: {why}")
        kind = "MARKET" if market else f"LIMIT {limit:.2f}"
        self.db.log("warn", f"{sym}: EXIT {qty:g} sent ({kind}): {why}", alert=True)
        self.invalidate()
        return True

    def stale_unknown(self, row: dict, open_ids: set, hist_ids: set | None) -> bool:
        """B1: an 'unknown' protective row older than ~5 min whose client_order_id is neither
        working at the broker nor in its order history is not an armed stop."""
        if row.get("status") != "unknown" or _age(row) <= STALE_UNKNOWN_SECONDS:
            return False
        coid = row.get("client_order_id")
        return coid not in open_ids and coid not in (hist_ids or set())

    # -- stale-stop correction: two phases, per trade ------------------------------------
    def _stale_rows(self, t: dict, trades: list[dict]) -> list[dict]:
        pid, sym = t["proposal_id"], t["symbol"]
        rows = [x for x in self.db.protective(sym)
                if x["kind"] == "sl" and x["status"] in ("active", "unknown")]
        mine = [x for x in rows if x.get("proposal_id") == pid]
        if mine:
            return mine
        sole = [o for o in trades if o["symbol"] == sym]
        if len(sole) == 1 and len(rows) == 1 and rows[0].get("proposal_id") is None:
            return rows
        return []

    async def correct_stale_stops(self, held: dict) -> int:
        """A resting stop at/above the entry fill AND the live price (the ADI case: it would
        sell at the next open) is replaced by a re-anchored stop, in two phases:
          1. send the cancel and record kv ``reanchor_pending:<pid>`` (new stop/target);
          2. on a later pass, only once the old row is CANCELLED (by _sync / order history),
             place the re-anchored stop and persist the new levels. If the old stop FILLED
             instead, the marker is cleared and nothing is placed.
        Matched per trade (proposal id; an id-less row only for the symbol's sole trade). Not
        done in CORE. A cancel reply that is "not found / may have been filled / Permission
        denied" is not a confirmation. Never raises; never leaves a held position without a
        working stop or a pending re-place."""
        n = 0
        try:
            session = order_session()
            trades = [t for t in self.db.open_trades()
                      if t.get("side") == "BUY" and t.get("proposal_id") is not None]
            for t in trades:                                        # phase 2
                m = self.db.get_kv(f"reanchor_pending:{t['proposal_id']}")
                if m:
                    n += await self._finish_reanchor(t, m, held, session)
            if session == "CORE":
                return n
            for t in trades:                                        # phase 1
                pid, sym, fill = t["proposal_id"], t["symbol"], t.get("entry_fill_price")
                if (not fill or held.get(sym, 0) <= 0 or self.db.get_kv(f"reanchored:{pid}")
                        or self.db.get_kv(f"reanchor_pending:{pid}")):
                    continue
                rows = self._stale_rows(t, trades)
                if not rows:
                    continue
                row = max(rows, key=lambda x: float(x["price"] or 0))
                bad = float(row["price"] or 0)
                if bad < float(fill):
                    continue                                        # a valid (profit-locking) stop
                try:
                    live = await self.broker.last_price(sym)
                except Exception:  # noqa: BLE001
                    live = None
                if not live or bad < float(live):
                    continue                                        # not above the market
                sl, tp = self._reanchored_levels(t, float(fill))
                if not sl or sl >= bad:
                    continue
                tried = self.db.get_kv(f"reanchor_try:{pid}")
                if tried and time.time() - float(tried) < REANCHOR_RETRY_SECONDS:
                    continue
                self.db.set_kv(f"reanchor_try:{pid}", time.time())
                alert_key = f"stale_stop_alert:{pid}"
                try:
                    reply = str(await self.broker.cancel_order(row["client_order_id"]))
                    problem = reply if any(m in reply.lower() for m in UNCONFIRMED_CANCEL) else None
                except BrokerError as e:
                    problem = str(e)
                if problem:
                    if not self.db.get_kv(alert_key):
                        self.db.set_kv(alert_key, True)
                        self.db.log("error", f"{sym}: the resting stop ${bad:.2f} is above the fill "
                                             f"${float(fill):.2f} and the price, so it triggers at the "
                                             f"open, but the cancel was not confirmed ({problem}). "
                                             f"Replace it in Webull with ~${sl:.2f}.", alert=True)
                    continue
                self.db.set_kv(f"reanchor_pending:{pid}", {
                    "old_row": row["id"], "old_coid": row["client_order_id"], "sl": sl, "tp": tp,
                    "bad": bad, "qty": float(row["quantity"] or 0), "at": time.time()})
                self.db.log("warn", f"{sym}: stop ${bad:.2f} is above the fill ${float(fill):.2f} and "
                                    f"the price: cancel sent; the re-anchored stop ${sl:.2f} is "
                                    "placed once the broker confirms the cancel.", alert=True)
        except Exception:  # noqa: BLE001
            log.exception("stale-stop correction failed")
        return n

    def _finalize_reanchor(self, t: dict, m: dict, sym: str) -> None:
        pid = t["proposal_id"]
        self.db.update_trade_levels(pid, m["sl"], m["tp"])
        self.db.set_kv(f"reanchored:{pid}", {"stop": m["sl"], "from": m["bad"]})
        self.db.set_kv(f"reanchor_pending:{pid}", None)
        self.db.log("warn", f"{sym}: fill ${float(t.get('entry_fill_price') or 0):.2f} was below the "
                            f"planned stop ${m['bad']:.2f}; stop re-anchored to ${m['sl']:.2f} "
                            f"(target ${m['tp']:.2f})", alert=True)

    async def _finish_reanchor(self, t: dict, m: dict, held: dict, session: str | None) -> int:
        """Phase 2: place the re-anchored stop once the old stop is confirmed cancelled."""
        pid, sym = t["proposal_id"], t["symbol"]
        key = f"reanchor_pending:{pid}"
        try:
            if held.get(sym, 0) <= 0:
                self.db.set_kv(key, None)
                return 0
            old = next((x for x in self.db.protective(sym, open_only=False)
                        if x["id"] == m["old_row"]), None)
            if old is None:
                self.db.set_kv(key, None)
                return 0
            if old["status"] == "filled":
                self.db.set_kv(key, None)
                self.db.log("info", f"{sym}: the old stop filled; nothing to re-anchor.")
                return 0
            if old["status"] in ("active", "unknown", "waiting"):
                # cancel not confirmed yet: wait. If it stays unconfirmed for 10 min (the broker
                # no longer reports the order at all), alert once instead of guessing: placing
                # a second stop over a possibly-live one risks an oversell.
                if (time.time() - float(m.get("at") or time.time()) > 600
                        and not self.db.get_kv(f"reanchor_stuck_alert:{pid}")):
                    self.db.set_kv(f"reanchor_stuck_alert:{pid}", True)
                    self.db.log("error", f"{sym}: the cancel of stop ${m['bad']:.2f} has not been "
                                         "confirmed after 10 min. Check Webull: if the stop is gone, "
                                         f"place one at ~${m['sl']:.2f}.", alert=True)
                return 0
            others = [x for x in self.db.protective(sym) if x["kind"] == "sl"
                      and x["id"] != old["id"] and x["status"] in ("active", "unknown")]
            if others:                                     # something already covers the shares
                if any(abs(float(x["price"] or 0) - m["sl"]) < 0.011 for x in others):
                    self._finalize_reanchor(t, m, sym)
                else:
                    self.db.set_kv(key, None)
                return 0
            qty = min(float(m.get("qty") or 0), float(held[sym])) or float(held[sym])
            live = await self.stop_above_market(sym, m["sl"])
            if live:
                self.db.set_kv(key, None)
                await self.exit_position(sym, qty, live, f"re-anchored stop {m['sl']:.2f} >= price {live:.2f}")
                return 0
            from .engine.util import _coid
            coid = _coid()
            row = self.db.add_protective(pid, sym, "sl", coid, qty, m["sl"], "unknown",
                                         "re-anchored below the fill")
            try:
                await self.broker.place_stop(sym, qty, m["sl"], coid, allow_when_killed=True)
            except BrokerError as e:
                if e.transport:                            # may have been placed: keep the row
                    self._finalize_reanchor(t, m, sym)
                else:
                    self.db.set_protective(row, "closed", f"stop not placed: {e}")
                self.db.log("error", f"{sym}: re-anchored stop ${m['sl']:.2f} was not placed ({e}); "
                                     "the periodic check will retry.")
                return 0
            self.db.set_protective(row, "active", "re-anchored below the fill")
            self._finalize_reanchor(t, m, sym)
            return 1
        except Exception:  # noqa: BLE001
            log.exception("re-anchor phase 2 failed for %s", sym)
            return 0

    # -- trade ledger tracking ----------------------------------------------------
    async def _protect_filled_entry(self, trade: dict, filled_qty: float, positions: dict,
                                    working_sell: set, fill_price: float | None = None) -> None:
        """Place the protective GTC CORE stop as soon as an entry fill is recorded, instead of
        waiting for the periodic stop check. Idempotent: skipped when an sl row (any open state)
        or a working SELL already exists for the symbol. Sized to the filled quantity (capped at
        what is held). Limitation: a partial fill that is still working is only handled once the
        order ends; later fills are not resized by this path (the periodic check only verifies
        that a stop exists, it does not resize one). Never raises."""
        try:
            from .engine.util import _coid, _is_option_symbol
            sym = trade["symbol"]
            stop = trade.get("stop_price")
            if (not self.s.bracket_orders or trade.get("side") != "BUY" or not stop
                    or _is_option_symbol(sym)):
                return
            if self.s.core_enabled and sym.upper() == self.s.core_symbol.upper():
                return
            if (sym.upper() in working_sell
                    or any(x["kind"] == "sl" for x in self.db.protective(sym))):
                return
            held = positions.get(sym, 0.0)
            qty = min(float(filled_qty or 0), held) if held > 0 else 0.0
            if qty <= 0:
                return          # position not visible yet: the periodic check will place it
            fill = fill_price if fill_price else trade.get("entry_fill_price")
            stop, _tp, _changed = await self.reanchor_stop(trade, fill, float(stop))
            live = await self.stop_above_market(sym, float(stop))
            if live:        # S7: a sell stop at/above the market is not placeable: exit instead
                await self.exit_position(sym, qty, live, f"stop {float(stop):.2f} >= price {live:.2f}")
                return
            coid = _coid()
            note = "placed at entry fill"
            row = self.db.add_protective(trade["proposal_id"], sym, "sl", coid, qty, float(stop),
                                         "unknown", note)
            try:
                await self.broker.place_stop(sym, qty, float(stop), coid, allow_when_killed=True)
            except BrokerError as e:
                if not e.transport:
                    self.db.set_protective(row, "closed", f"stop not placed: {e}")
                self.db.log("error", f"{sym}: could not place the protective stop at {stop} "
                                     f"after the entry filled: {e}. The periodic check will retry.")
                return
            self.db.set_protective(row, "active", note)
            working_sell.add(sym.upper())
            self.db.log("info", f"{sym}: protective stop placed for {qty:g} at {stop} on entry fill")
        except Exception:  # noqa: BLE001 - protection bookkeeping must not break fill tracking
            log.exception("could not protect the filled entry for %s", trade.get("symbol"))

    async def track_trade_fills(self) -> int:
        """Track entry fills and exits for the trade ledger."""
        changed = 0
        # Check all open trades in the ledger
        open_trades = self.db.open_trades()
        if not open_trades:
            return 0

        # Get current orders from broker
        open_text = await self.broker.open_orders_text()
        hist_text = await self.broker.order_history_text()
        orders = {o["client_order_id"]: o for o in parse_orders(hist_text)}
        orders.update({o["client_order_id"]: o for o in parse_orders(open_text)})

        # Get current positions
        positions = await self.broker.positions()
        working_sell = {(o.get("symbol") or "").upper() for o in parse_orders(open_text)
                        if (o.get("side") or "").upper() == "SELL"
                        and ((o.get("status") or "").upper() in ACTIVE
                             or (o.get("status") or "").upper().startswith(("PENDING", "PARTIAL")))}

        for trade in open_trades:
            proposal_id = trade["proposal_id"]
            if not proposal_id:
                continue

            # Get the proposal to find the client_order_id
            proposal = self.db.get_proposal(proposal_id)
            if not proposal:
                continue

            coid = proposal.get("client_order_id")
            if not coid:
                continue

            # Entry order that ended without a full fill: record what filled, or close the row
            if trade["entry_fill_price"] is None:
                o = orders.get(coid)
                status = ((o or {}).get("status") or "").upper()
                if o and status in DONE_CANCELLED:
                    filled = o.get("filled_qty") or 0.0
                    if filled > 0:
                        # Partly filled, then cancelled/expired: keep the shares that did fill.
                        fill_price = o.get("avg_fill_price") or o.get("filled_price") or trade["limit_price"]
                        slippage = (fill_price - trade["limit_price"] if trade["side"] == "BUY"
                                    else trade["limit_price"] - fill_price)
                        self.db.update_trade_fill(proposal_id, fill_price,
                                                  o.get("filled_at") or o.get("placed_at") or _now(),
                                                  filled, o.get("commission") or 0.0, slippage)
                        self.db.log("info", f"Trade entry part-filled then {status.lower()}: "
                                            f"{trade['symbol']} {filled:g} of {trade['quantity_requested']:g} "
                                            f"@ {fill_price:.2f}")
                        await self._protect_filled_entry(trade, filled, positions, working_sell,
                                                         fill_price)
                    elif (self.db.get_kv(f"chase_cancel:{proposal_id}")
                          and not self.db.get_kv(f"chase:{proposal_id}")):
                        continue    # the entry chaser is mid cancel-and-resend: keep the row
                    else:
                        self.db.cancel_trade(proposal_id, f"entry_{status.lower()}")
                        self.db.update_proposal(
                            proposal_id, reason=f"Entry order {status.lower()} at the broker without filling")
                        self.db.log("info", f"Trade entry {status.lower()} unfilled: {trade['symbol']}")
                    changed += 1
                    continue
                if o is None and positions.get(trade["symbol"], 0.0) == 0:
                    # Not open and not in history. Old enough that a DAY order cannot still be
                    # working, and nothing is held, so it never became a position.
                    try:
                        age = _age({"created_at": trade["created_at"]})   # not datetime.*: a later local import shadows it here
                    except (TypeError, ValueError):
                        age = 0.0
                    if age > 36 * 3600:
                        self.db.cancel_trade(proposal_id, "entry_not_found")
                        self.db.update_proposal(
                            proposal_id, reason="Entry order not found at the broker after 36h; no position held")
                        self.db.log("warn", f"Trade entry for {trade['symbol']} not found at the broker "
                                            "after 36h and no position is held; closed as unfilled.")
                        changed += 1
                        continue

            # Check if entry filled
            if trade["entry_fill_price"] is None:
                o = orders.get(coid)
                if o and (o.get("status") or "").upper() == "FILLED":
                    # Prefer average fill price for accuracy (handles partial fills), fall back to filled_price
                    fill_price = o.get("avg_fill_price") or o.get("filled_price") or trade["limit_price"]
                    fill_time = o.get("filled_at") or o.get("placed_at") or _now()
                    # Use actual filled quantity from broker (handles partial fills)
                    filled_qty = int(o.get("filled_qty") or trade["quantity_requested"])
                    slippage = fill_price - trade["limit_price"] if trade["side"] == "BUY" else trade["limit_price"] - fill_price
                    commission = o.get("commission") or 0.0
                    self.db.update_trade_fill(proposal_id, fill_price, fill_time,
                                              filled_qty, commission, slippage)
                    self.db.log("info", f"Trade entry filled: {trade['symbol']} @ {fill_price:.2f} "
                                         f"(qty: {filled_qty}, slippage: {slippage:+.2f})")
                    await self._protect_filled_entry(trade, filled_qty, positions, working_sell,
                                                     fill_price)
                    changed += 1
                    continue

            # Check if position exited (for trades that have filled entry)
            if trade["entry_fill_price"] is not None and trade["exit_fill_price"] is None:
                # Check if position is still held
                held_qty = positions.get(trade["symbol"], 0.0)
                if held_qty == 0:
                    # Position closed - find exit fill in history
                    # Look for SELL order for this symbol
                    for o in orders.values():
                        if (o.get("symbol", "").upper() == trade["symbol"] and
                            (o.get("side") or "").upper() == "SELL" and
                            (o.get("status") or "").upper() == "FILLED"):
                            # Determine exit reason from protective orders
                            exit_reason = "manual"
                            exit_ref_price = None  # stop or target price for slippage calc
                            for po in self.db.protective(trade["symbol"]):
                                if po.get("proposal_id") == proposal_id and po["status"] == "filled":
                                    exit_reason = "stop" if po["kind"] == "sl" else "target"
                                    exit_ref_price = po["price"]
                                    break

                            # Prefer average fill price for accuracy, fall back to filled_price
                            fill_price = o.get("avg_fill_price") or o.get("filled_price") or 0.0
                            fill_time = o.get("filled_at") or _now()
                            # Use actual filled quantity from broker
                            filled_qty = int(o.get("filled_qty") or trade["quantity_filled"] or trade["quantity_requested"])
                            # Calculate exit slippage from stop/target reference price
                            slippage = 0.0
                            if exit_ref_price and fill_price:
                                slippage = exit_ref_price - fill_price if trade["side"] == "BUY" else fill_price - exit_ref_price
                            commission = o.get("commission") or 0.0

                            # Calculate P&L
                            entry_price = trade["entry_fill_price"]
                            qty = filled_qty
                            realized_pnl = (fill_price - entry_price) * qty - (trade["entry_commission"] or 0) - commission
                            realized_pnl_pct = (fill_price / entry_price - 1) * 100 if entry_price else 0

                            # Calculate hold time
                            from datetime import datetime
                            entry_time = datetime.fromisoformat(trade["entry_fill_time"].replace('Z', '+00:00'))
                            exit_time = datetime.fromisoformat(fill_time.replace('Z', '+00:00'))
                            hold_seconds = int((exit_time - entry_time).total_seconds())

                            # MAE/MFE - compute from intraday bars during the trade hold period
                            mae, mfe = await self._compute_mae_mfe(
                                trade["symbol"], entry_price, entry_time, exit_time
                            )

                            self.db.update_trade_exit(proposal_id, fill_price, fill_time,
                                                      exit_reason, commission, slippage)
                            self.db.update_trade_pnl(proposal_id, realized_pnl, realized_pnl_pct,
                                                     mae, mfe, hold_seconds)
                            self.db.log("info", f"Trade exited: {trade['symbol']} @ {fill_price:.2f} "
                                                 f"(qty: {qty}, {exit_reason}, P&L: ${realized_pnl:+.2f}, {realized_pnl_pct:+.2f}%)")

                            # Run deterministic autopsy with counterfactuals
                            closed_trade = self.db.get_trade(proposal_id)
                            if closed_trade:
                                causes = run_autopsy(closed_trade, broker=self.broker,
                                                     entry_time=entry_time, exit_time=exit_time)
                                llm_prompt = format_autopsy_for_llm(closed_trade, causes)
                                # Store autopsy - LLM narration will be filled in by review process
                                self.db.add_autopsy(closed_trade, [vars(c) for c in causes])
                                self.db.log("info", f"Autopsy completed for {trade['symbol']}: "
                                                     f"{closed_trade.get('realized_pnl_pct', 0):+.2f}% "
                                                     f"({len(causes)} causes found)")
                            changed += 1
                            break

        return changed

    # -- background loop ---------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="portfolio")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        last_closed_snap = 0.0
        while True:
            try:
                if self.broker.connected:
                    is_open = market_is_open()
                    if is_open or time.time() - last_closed_snap > 600:
                        snap = await self.snapshot(max_age=1)
                        if (snap["balance"].get("net_liq") is not None
                                and not snap["balance"].get("stale")
                                and not balance_is_empty(snap["balance"])):
                            self.db.add_equity(snap["balance"])
                        last_closed_snap = time.time()
                    await self.reconcile()
                    await self.track_trade_fills()
                    self.db.prune_equity((now_utc() - timedelta(days=35)).isoformat())
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("portfolio loop")
            await asyncio.sleep(self.s.snapshot_seconds)

    def equity_today(self) -> list[dict]:
        return self.db.equity(start_of_today_et_utc().isoformat(timespec="seconds"))
