"""SleeveManager mixin: snapshot, valuation, equity recording, drawdown checks."""
from __future__ import annotations

from ._common import *  # noqa: F401,F403


class ValuationMixin:
    # ------------------------------------------------------------------ valuation
    async def _snapshot(self) -> dict:
        text = await self.broker.positions_text(fresh=True)
        rows = parse_position_rows(text) if text and not text.startswith("No data") else []
        last = {r["symbol"]: r.get("last") for r in rows if r.get("last")}
        raw_qty = {r["symbol"]: float(r["qty"]) for r in rows}
        try:
            from ..engine.option_exits import parse_held_options
            opts = {h.symbol.upper(): h for h in parse_held_options(text or "")}
        except Exception as exc:  # noqa: BLE001
            log.warning("held-option parse failed: %s", exc)
            opts = {}
        net_liq = None
        cash = None
        if self.eng.portfolio is not None:
            try:
                bal = (await self.eng.portfolio.snapshot()).get("balance") or {}
                net_liq = _f(bal.get("net_liq"))
                cash = _f(bal.get("cash"))
            except Exception as exc:  # noqa: BLE001
                log.warning("portfolio snapshot net_liq failed: %s", exc)
                net_liq = None
        return {"last": last, "raw_qty": raw_qty, "options": opts, "net_liq": net_liq, "cash": cash}

    async def _price(self, sym: str, snap: dict) -> float | None:
        p = snap["last"].get(sym)
        if p:
            return float(p)
        try:
            p = await self.broker.last_price(sym)
        except BrokerError:
            p = None
        if p:
            snap["last"][sym] = float(p)
        return _f(p)

    def _value(self, snap: dict) -> dict[str, dict]:
        """{sleeve: {equity, cash, invested}} plus 'total'. Swing = net liq minus the others."""
        out: dict[str, dict] = {}
        others = 0.0
        for sid in registry.NON_SWING:
            st = self.db.sleeve_state(sid)
            if not st:
                continue
            cash, invested, unmarked = float(st.get("cash") or 0.0), 0.0, False
            for h in self.db.sleeve_holdings(sid):
                q = float(h["qty"])
                if h["kind"] == "option":
                    held = snap["options"].get(h["symbol"].upper())
                    mark = _f(getattr(held, "mark", None)) if held else None
                    if mark is None:
                        unmarked = True   # estimate with cost for the swing remainder, but don't record it
                    invested += q * 100 * (mark if mark is not None else float(h.get("avg_cost") or 0))
                else:
                    px = snap["last"].get(h["symbol"]) or h.get("avg_cost") or 0.0
                    invested += q * float(px)
            # sold lots leave holdings when the order is sent but cash arrives on fill: count the
            # still-unfilled proceeds so equity (and the drawdown check) doesn't dip mid-rebalance
            pending = 0.0
            for o in self.db.sleeve_orders(sid, 50, OPEN_STATUSES):
                if str(o.get("side")).upper() != "SELL" or not o.get("limit_price"):
                    continue
                if o.get("kind") == "option" and o.get("position_intent") != "SELL_TO_CLOSE":
                    continue                  # a sell-to-open adds a short on fill, nothing left holdings
                rem = max(0.0, float(o.get("qty") or 0) - float(o.get("filled_qty") or 0))
                pending += rem * float(o["limit_price"]) * (100 if o.get("kind") == "option" else 1)
            out[sid] = {"equity": round(cash + invested + pending, 2), "cash": round(cash, 2),
                        "invested": round(invested, 2), "unmarked": unmarked}
            others += cash + invested + pending
        nl = snap.get("net_liq")
        if nl is not None:
            out["swing"] = {"equity": round(nl - others, 2), "cash": None, "invested": None}
            out["total"] = {"equity": round(nl, 2), "cash": None, "invested": None}
        return out

    def _record(self, values: dict, snap: dict) -> None:
        if not is_trading_day(now_et().date()):
            return                                   # no equity rows on weekends/holidays
        day = now_et().date().isoformat()
        for sid, v in values.items():
            if v.get("unmarked"):
                log.warning("sleeve %s: held option has no mark; skipping today's equity row", sid)
                continue
            self.db.record_sleeve_equity(day, sid, v["equity"], v.get("cash"), v.get("invested"))
        spy = snap["last"].get("SPY")
        if spy:
            self.db.record_sleeve_equity(day, "spy", float(spy))

    async def _drawdown_checks(self, values: dict) -> None:
        limit = float(self.s.sleeve_pause_dd_pct)
        for sid in ("swing", *registry.NON_SWING):
            v = values.get(sid)
            st = self.db.sleeve_state(sid)
            if not v or not st:
                continue
            eq = v["equity"]
            prev = self._last_eq.get(sid)
            self._last_eq[sid] = eq
            peak = float(st.get("peak_equity") or eq)
            # a new peak only from a believable reading (no >15% jump in one minute), so one bad
            # high balance read cannot set a peak that later looks like a drawdown
            if eq > peak and (prev is None or eq <= prev * 1.15):
                peak = eq
            if peak != st.get("peak_equity"):
                self.db.upsert_sleeve_state(sid, peak_equity=peak)
            dd = (eq / peak - 1) * 100 if peak > 0 else 0.0
            # one bad balance read must not pause a sleeve (the swing figure is net liq minus the
            # others): the breach has to hold for DD_CONFIRM_TICKS consecutive ticks (~minutes)
            hits = self._dd_hits.get(sid, 0) + 1 if dd <= limit else 0
            self._dd_hits[sid] = hits
            if hits >= DD_CONFIRM_TICKS and not st.get("paused"):
                await self.set_paused(sid, True, f"drawdown {dd:.1f}% reached the {limit:.0f}% pause line")

    def others_day_pnl(self) -> float:
        """Today's P&L of the non-swing sleeves (0 when sleeves are off or not valued yet)."""
        if not self.s.sleeves_enabled or not self.last_values:
            return 0.0
        today = now_et().date().isoformat()
        rows = [r for r in self.db.sleeve_equity_series(10) if r["day"] < today]
        prev = rows[-1] if rows else {}
        total = 0.0
        for sid in registry.NON_SWING:
            now_eq = (self.last_values.get(sid) or {}).get("equity")
            if now_eq is not None and prev.get(sid) is not None:
                total += now_eq - float(prev[sid])
        return round(total, 2)

