"""SleeveManager mixin: sleeve view builder."""
from __future__ import annotations

from ._common import *  # noqa: F401,F403


class ViewsMixin:
    # ------------------------------------------------------------------ API views
    def _sleeve_view(self, d: registry.SleeveDef, values: dict) -> dict:
        s = self.s
        st = self.db.sleeve_state(d.id) or {}
        v = values.get(d.id) or {}
        eq = v.get("equity")
        enabled = s.sleeves_enabled and (d.id == "swing" or d.id in self.enabled_ids())
        start = float(st.get("start_capital") or (s.sleeve_start_capital if d.id != "swing" else 0) or 0)
        series = [r for r in self.db.sleeve_equity_series(400) if r.get(d.id) is not None]
        if d.id == "swing" and not start:
            from .. import results
            start = results.swing_start(self.db) or 0.0
            if not start and series:
                start = float(series[0][d.id])
        prev = next((r[d.id] for r in reversed(series) if r["day"] < now_et().date().isoformat()), None)
        peak = _f(st.get("peak_equity")) or eq
        rets = []
        for a, b in zip(series, series[1:]):
            if a[d.id]:
                rets.append(b[d.id] / a[d.id] - 1)
        sharpe = vol = None
        if len(rets) >= 20:
            m = sum(rets) / len(rets)
            sd = (sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
            vol = sd * math.sqrt(252) * 100
            sharpe = (m / sd * math.sqrt(252)) if sd > 0 else None
        holds = self.db.sleeve_holdings(d.id) if d.id != "swing" else []
        status = "disabled" if not enabled else ("paused" if st.get("paused") else "active")
        reason = st.get("pause_reason") if st.get("paused") else None
        if d.id != "swing" and enabled and not st.get("paused"):
            if self.db.sleeve_orders(d.id, 5, OPEN_STATUSES):
                status, reason = "active", "orders working"
            elif not holds:
                status, reason = "waiting", "waiting for the first trade (CORE session, after " \
                                            f"{s.sleeve_rebalance_after_et} ET)"
        last_rb = st.get("last_rebalance")
        nxt = None
        if d.id in ("momentum", "vaa"):
            base = datetime.fromisoformat(last_rb).date() if last_rb else None
            nxt = (next_rebalance_day(base).isoformat() if base else now_et().date().isoformat())
        elif d.id == "putwrite":
            put = next((h for h in holds if h["kind"] == "option"), None)
            occ = parse_occ(put["symbol"]) if put else None
            nxt = occ["expiration"] if occ else now_et().date().isoformat()
        invested = v.get("invested")
        if d.id == "putwrite":      # what is committed is the cash securing the puts, not their mark
            invested = round(sum(abs(float(h["qty"])) * 100 * (parse_occ(h["symbol"]) or {}).get("strike", 0)
                                 for h in holds if h["kind"] == "option"), 2) or (invested if holds else 0.0)
        if d.id == "swing" and self.eng.portfolio is not None and self.eng.portfolio._cache:
            invested = sum(float(r.get("market_value") or 0) for r in self.eng.portfolio._cache.get("positions", []))
        return {
            "id": d.id, "name": d.name, "color_slot": d.color_slot, "description": d.description,
            "rebalance_rule": d.rebalance_rule, "expected": d.expected,
            "status": status, "status_reason": reason,
            "start_capital": start or None, "start_date": st.get("start_date"),
            "equity": eq, "cash": v.get("cash"), "invested": invested,
            "invested_pct": round(invested / eq * 100, 1) if eq and invested is not None else None,
            "day_pnl": round(eq - prev, 2) if eq is not None and prev else None,
            "day_pnl_pct": round((eq / prev - 1) * 100, 2) if eq is not None and prev else None,
            "total_return_pct": round((eq / start - 1) * 100, 2) if eq and start else None,
            "peak_equity": peak, "drawdown_pct": round((eq / peak - 1) * 100, 2) if eq and peak else None,
            "pause_dd_pct": s.sleeve_pause_dd_pct,
            "positions_n": (len(holds) if d.id != "swing" else
                            len((self.eng.portfolio._cache or {}).get("positions", []))
                            if self.eng.portfolio is not None else None),
            "last_rebalance": last_rb, "next_rebalance": nxt,
            "live": {"days": len(rets), "sharpe": round(sharpe, 2) if sharpe is not None else None,
                     "vol": round(vol, 1) if vol is not None else None},
        }

