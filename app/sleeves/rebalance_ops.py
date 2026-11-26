"""SleeveManager mixin: monthly rebalance step (momentum/VAA)."""
from __future__ import annotations

from ._common import *  # noqa: F401,F403


class RebalanceMixin:
    # ------------------------------------------------------------------ momentum / VAA
    def _due_month(self, sid: str) -> bool:
        st = self.db.sleeve_state(sid) or {}
        last = st.get("last_rebalance")
        if not last:
            return True
        d = datetime.fromisoformat(last).date()
        today = now_et().date()
        return (d.year, d.month) != (today.year, today.month)

    async def _targets(self, sid: str) -> dict[str, float] | None:
        st = self.db.sleeve_state(sid) or {}
        meta = _meta(st)
        month = now_et().strftime("%Y-%m")
        if meta.get("targets_month") == month and meta.get("targets") is not None:
            return meta["targets"]
        if time.monotonic() < self._retry_after.get(sid, 0):
            return None
        from . import data
        if sid == "momentum":
            syms = data.sp500_symbols()
            closes = await asyncio.to_thread(data.daily_closes, syms)
            got = 0 if closes is None else len(getattr(closes, "columns", []))
            if syms and got < 0.9 * len(syms):
                # M18: partial download would rank a shrunken universe; don't cache the month.
                self._retry_after[sid] = time.monotonic() + 1800
                self.db.log("warn", f"Sleeve {sid}: downloaded {got}/{len(syms)} symbols (<90%); "
                                    "targets not cached, retrying in 30 min")
                return None
            tg = momentum.targets(closes, n=int(self.s.sleeve_momentum_n))
        else:
            closes = await asyncio.to_thread(data.daily_closes, VAA_SYMBOLS)
            tg = vaa.targets(closes)
        if not tg:
            self._retry_after[sid] = time.monotonic() + 1800
            self.db.log("warn", f"Sleeve {sid}: could not compute targets (price history missing); "
                                "retrying in 30 min")
            return None
        meta.update({"targets": tg, "targets_month": month, "passes": 0,
                     "targets_at": now_utc().isoformat(timespec="seconds")})
        self.db.upsert_sleeve_state(sid, meta=meta)
        return tg

    async def _rebalance_step(self, sid: str, values: dict, snap: dict) -> None:
        st = self.db.sleeve_state(sid) or {}
        if st.get("paused") or not self._due_month(sid) or self.eng.kill_switch:
            return
        tg = await self._targets(sid)
        if tg is None:
            return
        meta = _meta(self.db.sleeve_state(sid))
        holdings = {h["symbol"]: float(h["qty"]) for h in self.db.sleeve_holdings(sid) if h["kind"] == "stock"}
        prices = {}
        for sym in set(tg) | set(holdings):
            p = await self._price(sym, snap)
            if p:
                prices[sym] = p
        missing = rebalance.missing_prices(holdings, tg, prices)
        if missing:
            self.db.log("warn", f"Sleeve {sid}: no price for {', '.join(missing)}; "
                                "rebalance deferred, retrying next tick")
            return
        equity = values.get(sid, {}).get("equity") or self._cash(sid)
        trades = rebalance.diff(holdings, tg, equity * 0.995, prices, self.s.sleeve_min_trade_usd)
        if not trades or meta.get("passes", 0) >= MAX_PASSES_PER_MONTH:
            if trades:
                self.db.log("warn", f"Sleeve {sid}: rebalance closed after {MAX_PASSES_PER_MONTH} passes "
                                    f"with {len(trades)} trade(s) left", alert=True)
            self.db.upsert_sleeve_state(sid, last_rebalance=now_utc().isoformat(timespec="seconds"))
            top = ", ".join(f"{k} {v:.0%}" for k, v in sorted(tg.items(), key=lambda kv: -kv[1]))
            self.db.log("info", f"Sleeve {registry.get(sid).name}: rebalanced to " + top)
            self._event(sid, "rebalance", f"Rebalanced to {top}"[:200])
            return
        meta["passes"] = meta.get("passes", 0) + 1
        self.db.upsert_sleeve_state(sid, meta=meta)
        sells = [t for t in trades if t[1] == "SELL"]
        if sells:                               # sells first; buys in a later pass, from the cash raised
            for sym, side, qty in sells:
                await self._send_chunks(sid, sym, side, qty, prices[sym], "rebalance")
            return
        cash = self._cash(sid)
        need = sum(q * prices[s] * (1 + OFFSET_BPS / 1e4) for s, _, q in trades)
        scale = min(1.0, (cash * 0.995) / need) if need > 0 else 0.0
        for sym, side, qty in trades:
            q = int(math.floor(qty * scale))
            if q >= 1 and q * prices[sym] >= min(self.s.sleeve_min_trade_usd, cash):
                await self._send_chunks(sid, sym, side, q, prices[sym], "rebalance")

    async def _send_chunks(self, sid: str, sym: str, side: str, qty: int, price: float, why: str) -> None:
        s = self.s
        cap_usd = min([c for c in (s.max_order_usd, getattr(s, "max_order_notional", 0)) if c] or [0])
        unit = price * (1 + 2 * OFFSET_BPS / 1e4)
        if side == "BUY" and cap_usd and unit > cap_usd:
            # one share already breaks MAX_ORDER_USD: the guard would refuse every piece
            self.db.log("warn", f"Sleeve {sid}: cannot buy {sym} at ~${unit:,.2f}/share with MAX_ORDER_USD="
                                f"{cap_usd:,.0f}; raise it before enabling sleeves", alert=True)
            return
        whole = int(math.floor(qty + 1e-9))
        for q in rebalance.chunks(whole, unit, cap_usd, s.max_order_qty):
            await self._place_stock(sid, sym, side, int(q), price, why)

