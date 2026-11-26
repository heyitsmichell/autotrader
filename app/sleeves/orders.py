"""SleeveManager mixin: stock order placement, sync, fills."""
from __future__ import annotations

from ._common import *  # noqa: F401,F403


class OrdersMixin:
    # ------------------------------------------------------------------ orders
    async def _place_stock(self, sid: str, sym: str, side: str, qty: int, ref: float, why: str) -> int:
        """Send one marketable limit (already chunked). Returns the sleeve order id."""
        q = await self._quote(sym)
        bid, ask = (q.bid, q.ask) if q else (None, None)
        if side == "BUY":
            px = (ask or ref) * (1 + OFFSET_BPS / 1e4)
        else:
            px = (bid or ref) * (1 - OFFSET_BPS / 1e4)
        px = round(px, 2)
        coid = registry.coid_for(sid)
        oid = self.db.add_sleeve_order(sid, sym, "stock", side, qty, px, coid, reason=why)
        if side == "SELL":          # shares leave the sleeve when the sell is sent (see module doc)
            self._adjust_holding(sid, sym, -qty, None)
        try:
            await self.broker.place_order(sym, side, qty, px, coid, trading_session="CORE")
        except BrokerError as e:
            if e.transport:
                self.db.update_sleeve_order(oid, status="working", reason=f"{why}; no reply: {e}")
            else:
                self.db.update_sleeve_order(oid, status="rejected", reason=f"{why}; rejected: {e}")
                if side == "SELL":
                    self._adjust_holding(sid, sym, qty, None)
                self.db.log("warn", f"Sleeve {sid}: {side} {qty} {sym} rejected: {e}")
            return oid
        self.db.update_sleeve_order(oid, status="working")
        self.db.log("info", f"Sleeve {registry.get(sid).name}: {side} {qty} {sym} @ {px:.2f} ({why})")
        return oid

    async def _quote(self, sym: str):
        try:
            return await self.broker.quote(sym)
        except BrokerError:
            return None

    def _adjust_holding(self, sid: str, sym: str, dq: float, fill_px: float | None,
                        kind: str = "stock") -> None:
        cur = next((h for h in self.db.sleeve_holdings(sid) if h["symbol"] == sym.upper()), None)
        q0 = float(cur["qty"]) if cur else 0.0
        c0 = _f(cur.get("avg_cost")) if cur else None
        q1 = q0 + dq
        cost = c0
        if fill_px is not None and abs(q1) > abs(q0):        # adding to the position: blend the cost
            cost = ((abs(q0) * (c0 or fill_px)) + abs(dq) * fill_px) / abs(q1)
        self.db.set_sleeve_holding(sid, sym, kind, round(q1, 6), round(cost, 4) if cost else cost)

    async def sync_orders(self) -> None:
        """Settle working sleeve orders from the broker's open orders and history."""
        rows = self.db.sleeve_orders(None, 200, OPEN_STATUSES)
        if not rows:
            return
        try:
            seen = {o["client_order_id"]: o for o in parse_orders(await self.broker.order_history_text())}
            seen.update({o["client_order_id"]: o for o in parse_orders(await self.broker.open_orders_text())})
        except BrokerError as e:
            log.debug("sleeve sync: %s", e)
            return
        for r in rows:
            o = seen.get(r["client_order_id"])
            age = (now_utc() - datetime.fromisoformat(r["ts"])).total_seconds()
            if o is None:
                if age > GIVE_UP_SECONDS:
                    self._finish(r, "failed", "never seen at the broker")
                continue
            st = (o.get("status") or "").upper()
            filled = float(o.get("filled_qty") or 0)
            px = _f(o.get("avg_fill_price")) or _f(o.get("filled_price"))
            new = filled - float(r.get("filled_qty") or 0)
            if new > 1e-9 and not px:
                log.warning("sleeve sync: %s filled %s but no fill price reported; retrying next sync",
                            r["client_order_id"], new)
                continue                         # never book at the limit price
            if new > 1e-9 and px:
                self._apply_fill(r, new, px)
                self.db.update_sleeve_order(r["id"], filled_qty=filled, avg_fill_price=px)
                r = {**r, "filled_qty": filled}
            if st == "FILLED" or filled >= float(r["qty"]) - 1e-9:
                self.db.update_sleeve_order(r["id"], status="filled")
            elif st in ACTIVE or st.startswith(("PENDING", "PARTIAL", "WORKING", "SUBMITTED", "NEW")):
                if age > CANCEL_AFTER_SECONDS:
                    try:
                        await self.broker.cancel_order(r["client_order_id"])
                    except BrokerError as e:
                        log.debug("sleeve cancel %s: %s", r["client_order_id"], e)
                if filled > 0:
                    self.db.update_sleeve_order(r["id"], status="partial")
            else:                                # cancelled / rejected / expired / failed
                self._finish(r, "cancelled" if "CANCEL" in st else st.lower() or "failed",
                             f"broker status {st}")

    def _finish(self, r: dict, status: str, why: str) -> None:
        rest = float(r["qty"]) - float(r.get("filled_qty") or 0)
        if rest > 1e-9 and r["side"] == "SELL" and r["kind"] == "stock":
            self._adjust_holding(r["sleeve"], r["symbol"], rest, None)   # unsold shares come back
        self.db.update_sleeve_order(r["id"], status=status, reason=f"{r.get('reason') or ''}; {why}".strip("; "))

    def _apply_fill(self, r: dict, q: float, px: float) -> None:
        self._apply_fill_trade(r, q, px)
        # the broker order detail carries no fee field, so the fee is an estimate
        if r["kind"] == "option":
            fee = abs(q) * OPTION_FEE_PER_CONTRACT
        elif r["side"] == "SELL":
            fee = abs(q) * px * STOCK_SELL_FEE_RATE
        else:
            fee = 0.0
        if fee > 0:
            self._add_cash(r["sleeve"], -round(fee, 4))

    def _apply_fill_trade(self, r: dict, q: float, px: float) -> None:
        sid, sym = r["sleeve"], r["symbol"]
        if r["kind"] == "option":
            intent = (r.get("position_intent") or "").upper()
            if intent == "SELL_TO_OPEN":
                self._adjust_holding(sid, sym, -q, px, kind="option")
                self._add_cash(sid, q * 100 * px)
            else:                                   # BUY_TO_CLOSE
                self._adjust_holding(sid, sym, q, None, kind="option")
                self._add_cash(sid, -q * 100 * px)
            return
        if r["side"] == "BUY":
            self._adjust_holding(sid, sym, q, px)
            self._add_cash(sid, -q * px)
        else:                                       # shares were removed when the sell was sent
            self._add_cash(sid, q * px)

