"""Options what-if grid: spot move x IV shift x horizon, Black-Scholes repricing (read-only)."""
from __future__ import annotations

from .pricing import RATE, bs_price, greeks

SPOT_PCT = [-20 + 2.5 * i for i in range(13)]
IV_SHIFT_PCT = [-30, 0, 50, 100]
HORIZONS = [{"label": "Today", "days": 0}, {"label": "+7 days", "days": 7}, {"label": "At expiry", "days": None}]
MULT = 100


def _rz(x, n=2):
    return round(x, n) + 0.0          # + 0.0 turns -0.0 into 0.0


def _t(dte: float, days: int | None) -> float:
    """Years left after `days` more days; None = expiry (zero)."""
    if days is None:
        return 0.0
    return max(dte - days, 0.0) / 365.0


def position_grid(p: dict) -> list[list[list[float]]]:
    """[horizon][iv][spot] dollar P&L vs the model value today. p needs spot, strike, right, dte, iv, qty."""
    S, K, right, iv, qty = p["spot"], p["strike"], p["right"], p["iv"], p["qty"]
    base = bs_price(S, K, _t(p["dte"], 0), iv, right)
    out = []
    for h in HORIZONS:
        t = _t(p["dte"], h["days"])
        rows = []
        for ivs in IV_SHIFT_PCT:
            sig = iv * (1 + ivs / 100.0)
            rows.append([_rz(qty * MULT * (bs_price(S * (1 + sp / 100.0), K, t, sig, right) - base), 2)
                         for sp in SPOT_PCT])
        out.append(rows)
    return out


def total_grid(grids: list[list[list[list[float]]]]) -> list[list[list[float]]]:
    out = [[[0.0] * len(SPOT_PCT) for _ in IV_SHIFT_PCT] for _ in HORIZONS]
    for g in grids:
        for h in range(len(HORIZONS)):
            for i in range(len(IV_SHIFT_PCT)):
                for s in range(len(SPOT_PCT)):
                    out[h][i][s] += g[h][i][s]
    return [[[_rz(v, 2) for v in row] for row in hz] for hz in out]


def position_stats(p: dict, sleeve_cash: float | None) -> dict:
    """Dollar Greeks, breakeven, max loss and assignment cash for one position."""
    S, K, right, iv, qty = p["spot"], p["strike"], p["right"], p["iv"], p["qty"]
    g = greeks(S, K, p["dte"] / 365.0, iv, right)
    n = qty * MULT
    prem = p.get("cost") if p.get("cost") else p.get("mark")
    be = mx = None
    if prem is not None:
        be = _rz(K - prem if right == "P" else K + prem, 2)
        if qty > 0:
            mx = _rz(prem * n, 2)
        elif right == "P":
            mx = _rz((K - prem) * abs(n), 2)       # short call: unlimited (None)
    return {"delta_dollars": _rz(n * g["delta"] * S, 2),
            "gamma_dollars_1pct": _rz(n * g["gamma"] * S * S * 0.01, 2),
            "vega_dollars": _rz(n * g["vega"] * 0.01, 2),
            "theta_dollars": _rz(n * g["theta"] / 365.0, 2),
            "breakeven": be, "max_loss": mx,
            "assignment_cash": _rz(K * MULT * abs(qty), 2) if (right == "P" and qty < 0) else None,
            "sleeve_cash": sleeve_cash}
