"""Historical-simulation risk report: pure functions on holdings dicts and a closes DataFrame.

A holding is a dict: symbol, kind ("stock"|"option"), sleeve, qty, spot (stock price or the
option's underlying), underlying, and for options right ("C"/"P"), strike, dte (days),
iv, iv_source, mark, cost.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..sleeves import analytics
from .pricing import bs_price, greeks, implied_vol

LOOKBACK = 252
SQRT252 = math.sqrt(252)
MULT = 100
SHOCKS = [(-5, 0), (-10, 25), (-20, 50)]
MIN_LIVE_DAYS = 20


def _r(x, n=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else round(x, n) + 0.0


def mv(h: dict) -> float:
    if h["kind"] == "stock":
        return h["qty"] * h["spot"]
    return h["qty"] * MULT * float(h.get("mark") or 0.0)


def under(h: dict) -> str:
    return h["underlying"] if h["kind"] == "option" else h["symbol"]


def prepare(holdings: list[dict], closes: pd.DataFrame, warnings: list[str]) -> list[dict]:
    """Fill missing spot and IV from history; drop what cannot be priced (with warnings)."""
    out = []
    for h in holdings:
        h = dict(h)
        u = under(h)
        has = closes is not None and not closes.empty and u in closes.columns
        col = closes[u].dropna() if has else None
        if h.get("spot") is None and col is not None and len(col):
            h["spot"] = float(col.iloc[-1])
            if h["kind"] == "option":
                warnings.append(f"live price for {u} unavailable; used last close")
        if h.get("spot") is None:
            warnings.append(f"no price for {u}; {h['symbol']} left out")
            continue
        if h["kind"] == "option":
            T = max(h["dte"], 0) / 365.0
            imp = implied_vol(h["mark"], h["spot"], h["strike"], T, h["right"]) if h.get("mark") else None
            if imp:                          # distrust marks at/near intrinsic (BS put < intrinsic when deep ITM)
                tv = h["mark"] - bs_price(h["spot"], h["strike"], 0.0, 0.0, h["right"])
                ref = h.get("iv")
                if not ref and col is not None and len(col) > 5:
                    ref = float(col.pct_change().dropna().iloc[-30:].std() * SQRT252) or None
                if tv < max(0.05, 0.001 * h["spot"]) or (ref and imp > 3 * ref):
                    imp = None
            if imp:                          # price the grid off today's broker mark
                h["iv"], h["iv_source"] = imp, "implied_from_mark"
            elif not h.get("iv"):
                if col is not None and len(col) > 5:
                    rv = float(col.pct_change().dropna().iloc[-30:].std() * SQRT252)
                    if rv > 0 and not math.isnan(rv):
                        h["iv"], h["iv_source"] = rv * 1.1, "realized_est"
                        warnings.append(f"IV missing for {h['symbol']}; used realized vol")
                if not h.get("iv"):
                    warnings.append(f"no IV or history for {h['symbol']}; left out")
                    continue
            else:
                h.setdefault("iv_source", "snapshot")
            if h.get("mark") is None:
                h["mark"] = bs_price(h["spot"], h["strike"], T, h["iv"], h["right"])
        out.append(h)
    return out

def _ret_array(closes: pd.DataFrame, sym: str, idx, warned: set, warnings: list[str]) -> np.ndarray:
    """Daily returns of `sym` aligned to the SPY date index `idx` (missing days = 0)."""
    n = len(idx)
    if closes is not None and sym in closes.columns:
        r = closes[sym].pct_change(fill_method=None).reindex(idx)
        if r.notna().any():
            return r.fillna(0.0).to_numpy(float)
    if sym not in warned:
        warned.add(sym)
        warnings.append(f"no price history for {sym}; treated as flat in the simulation")
    return np.zeros(n)


def pnl_for_returns(h: dict, rets: np.ndarray, iv_mult: float = 1.0, dt_days: float = 1.0) -> np.ndarray:
    """Dollar P&L of one holding for each underlying return (1-day horizon by default)."""
    if h["kind"] == "stock":
        return h["qty"] * h["spot"] * rets
    S, K, right = h["spot"], h["strike"], h["right"]
    iv = h["iv"] * iv_mult
    t0 = max(h["dte"], 0) / 365.0
    t1 = max(h["dte"] - dt_days, 0) / 365.0
    base = bs_price(S, K, t0, h["iv"], right)
    return np.array([h["qty"] * MULT * (bs_price(S * (1 + x), K, t1, iv, right) - base) for x in rets])


def var_es(pnl: np.ndarray, level: float) -> tuple[float, float]:
    """Positive loss dollars: VaR at `level` (0.95) and expected shortfall."""
    if len(pnl) == 0:
        return 0.0, 0.0
    q = float(np.percentile(pnl, (1 - level) * 100))
    tail = pnl[pnl <= q]
    es = -float(tail.mean()) if len(tail) else -q
    return max(-q, 0.0) + 0.0, max(es, 0.0) + 0.0


def _beta(x: np.ndarray, spy: np.ndarray) -> float | None:
    if len(spy) < 2 or len(x) != len(spy):
        return None
    v = np.var(spy, ddof=1)
    return float(np.cov(x, spy)[0, 1] / v) if v > 0 else None


def _contrib(x: np.ndarray, total: np.ndarray) -> float | None:
    if len(total) < 2:
        return None
    v = np.var(total, ddof=1)
    return float(np.cov(x, total)[0, 1] / v * 100) if v > 0 else None


def _stats(pnl: np.ndarray, spy: np.ndarray, equity: float | None) -> dict:
    v95, e95 = var_es(pnl, 0.95)
    v99, e99 = var_es(pnl, 0.99)
    sd = float(np.std(pnl, ddof=1)) if len(pnl) > 1 else 0.0
    b = _beta(pnl, spy)
    ok = bool(equity and equity > 0)
    return {"var95": _r(v95, 0), "var99": _r(v99, 0), "es95": _r(e95, 0), "es99": _r(e99, 0),
            "var95_pct": _r(v95 / equity * 100, 2) if ok else None,
            "vol_ann_pct": _r(sd * SQRT252 / equity * 100, 2) if ok else None,
            "beta_spy": _r(b / equity, 2) if (b is not None and ok) else None}


def _delta_dollar(h: dict) -> float:
    if h["kind"] == "stock":
        return h["qty"] * h["spot"]
    g = greeks(h["spot"], h["strike"], max(h["dte"], 0) / 365.0, h["iv"], h["right"])
    return h["qty"] * MULT * g["delta"] * h["spot"]


def _corr(mat: np.ndarray) -> list[list]:
    n = mat.shape[1]
    with np.errstate(all="ignore"):
        c = np.corrcoef(mat, rowvar=False) if mat.shape[0] > 1 else np.full((n, n), np.nan)
    c = np.atleast_2d(c)
    return [[_r(c[i][j], 3) if i != j else 1 for j in range(n)] for i in range(n)]


def live_correlation(series: list[dict], ids: list[str]) -> tuple[list[list] | None, int]:
    rets = [dict(analytics.returns_from_equity([(r["day"], r.get(sid)) for r in series])) for sid in ids]
    days = sorted(set.intersection(*[set(r) for r in rets])) if rets else []
    if len(days) < MIN_LIVE_DAYS or len(ids) < 2:
        return None, len(days)
    return _corr(np.array([[r[d] for r in rets] for d in days])), len(days)


def drawdowns(series: list[dict], ids: list[str]) -> dict:
    out = {}
    for sid in ids:
        rets = analytics.returns_from_equity([(r["day"], r.get(sid)) for r in series])
        out[sid] = [{"peak": e["start"], "trough": e["trough"], "recovery": e["end"],
                     "depth_pct": _r(e["depth"] * 100, 2), "days": e["days"]}
                    for e in analytics.worst_drawdowns(rets, 3)]
    return out


def build_report(holdings: list[dict], closes: pd.DataFrame, sleeves: dict[str, dict],
                 equity: float | None, series: list[dict], warnings: list[str]) -> dict:
    """sleeves: {id: {"name", "equity"}} in display order. Returns the /api/risk body (minus as_of/cached)."""
    has_spy = closes is not None and "SPY" in closes.columns
    spy_all = closes["SPY"].pct_change(fill_method=None).dropna().iloc[-LOOKBACK:] if has_spy else pd.Series(dtype=float)
    n = len(spy_all)
    spy = spy_all.to_numpy(float)
    if n < 30:
        warnings.append("less than 30 days of SPY history; risk numbers are unreliable")
    if not equity:
        equity = sum(abs(mv(h)) for h in holdings) or None
        warnings.append("account equity unavailable; used holdings value")
    warned: set = set()
    cols = []
    for h in holdings:
        r = _ret_array(closes, under(h), spy_all.index, warned, warnings) if n else np.zeros(0)
        cols.append(pnl_for_returns(h, r))
    total = np.sum(cols, axis=0) if cols else np.zeros(n)
    sl_ids = [sid for sid in sleeves if any(h["sleeve"] == sid for h in holdings)]
    sl_pnl = {sid: np.sum([c for c, h in zip(cols, holdings) if h["sleeve"] == sid], axis=0) for sid in sl_ids}

    acc = {"equity": _r(equity, 0), **_stats(total, spy, equity)}
    dd = [_delta_dollar(h) for h in holdings]
    acc["gross_exposure"] = _r(sum(abs(x) for x in dd), 0)
    acc["net_exposure"] = _r(sum(dd), 0)
    acc["delta_dollars"] = _r(sum(dd), 0)

    sl_out = []
    for sid, info in sleeves.items():
        eq = info.get("equity")
        row = {"id": sid, "name": info.get("name"), "equity": _r(eq, 0)}
        if sid in sl_pnl and n:
            s = _stats(sl_pnl[sid], spy, eq)
            row.update({k: s[k] for k in ("var95", "es95", "var95_pct", "vol_ann_pct", "beta_spy")})
            row["contribution_pct"] = _r(_contrib(sl_pnl[sid], total), 1)
        else:
            row.update(var95=None, es95=None, var95_pct=None, vol_ann_pct=None, beta_spy=None,
                       contribution_pct=None)
        row["n_positions"] = sum(1 for h in holdings if h["sleeve"] == sid)
        sl_out.append(row)

    by_sym: dict[str, dict] = {}
    for h, c in zip(holdings, cols):
        e = by_sym.setdefault(h["symbol"], {"kind": h["kind"], "sleeves": [], "mv": 0.0, "exp": 0.0, "pnl": np.zeros(n)})
        if h["sleeve"] not in e["sleeves"]:
            e["sleeves"].append(h["sleeve"])
        e["mv"] += mv(h)
        e["exp"] += _delta_dollar(h)      # exposure base: stock value / option delta dollars
        e["pnl"] = e["pnl"] + c
    hold_out = []
    for sym, e in by_sym.items():
        den = abs(e["exp"]) or None
        sd = float(np.std(e["pnl"], ddof=1)) if n > 1 else 0.0
        b = _beta(e["pnl"], spy)
        hold_out.append({"symbol": sym, "kind": e["kind"], "sleeves": e["sleeves"], "market_value": _r(e["mv"], 0),
                         "weight_pct": _r(e["exp"] / equity * 100, 2) if equity and equity > 0 else None,
                         "vol_ann_pct": _r(sd * SQRT252 / den * 100, 1) if den else None,
                         "beta_spy": _r(b / den, 2) if (b is not None and den) else None,
                         "contribution_pct": _r(_contrib(e["pnl"], total), 1)})
    hold_out.sort(key=lambda x: -abs(x["contribution_pct"] or 0))
    top = sorted(by_sym.items(), key=lambda kv: -abs(kv[1]["exp"]))
    conc = {"top5_pct": _r(sum(abs(e["exp"]) for _, e in top[:5]) / equity * 100, 1) if equity and equity > 0 else None,
            "shared_symbols": [{"symbol": s, "sleeves": e["sleeves"], "market_value": _r(e["mv"], 0)}
                               for s, e in top if len(e["sleeves"]) > 1]}

    if len(sl_ids) >= 2 and n > 1:
        corr_h = _corr(np.array([sl_pnl[i] for i in sl_ids]).T)
    else:
        corr_h = [[1 if i == j else None for j in range(len(sl_ids))] for i in range(len(sl_ids))]
    live, live_days = live_correlation(series, sl_ids) if sl_ids else (None, 0)
    correlation = {"ids": sl_ids, "holdings": corr_h, "live": live, "live_days": live_days}

    return {"lookback_days": n, "warnings": warnings, "account": acc, "sleeves": sl_out, "holdings": hold_out[:15],
            "concentration": conc, "correlation": correlation,
            "stress": _stress(holdings, closes, spy_all, equity, warned, warnings),
            "drawdowns": drawdowns(series, list(sleeves))}


def _stress(holdings, closes, spy_all: pd.Series, equity, warned, warnings) -> list[dict]:
    spy = spy_all.to_numpy(float)
    betas: dict[str, float] = {}
    for h in holdings:
        u = under(h)
        if u in betas:
            continue
        b = None
        if closes is not None and u in closes.columns and closes[u].notna().sum() < 2:
            warnings.append(f"no usable price history for {u}; stress beta defaulted to 1.0")
        elif closes is not None and u in closes.columns and len(spy):
            b = _beta(_ret_array(closes, u, spy_all.index, warned, warnings), spy)
        betas[u] = 1.0 if b is None else b

    def run(name, kind, date, spy_pct, fn):
        by: dict[str, float] = {}
        for h in holdings:
            by[h["sleeve"]] = by.get(h["sleeve"], 0.0) + fn(h)
        tot = sum(by.values())
        return {"name": name, "kind": kind, "date": date, "spy_pct": spy_pct, "pnl": _r(tot, 0),
                "pnl_pct": _r(tot / equity * 100, 2) if equity and equity > 0 else None,
                "by_sleeve": {k: _r(v, 0) for k, v in by.items()}}

    out = []
    for shock, ivup in SHOCKS:
        def fn(h, shock=shock, ivup=ivup):
            return float(pnl_for_returns(h, np.array([betas[under(h)] * shock / 100.0]),
                                         1 + ivup / 100.0, 0.0)[0])
        out.append(run(f"Market {shock}%", "beta_shock", None, shock, fn))
    for day, ret in spy_all.nsmallest(5).items():
        def fn(h, day=day):
            u = under(h)
            if closes is None or u not in closes.columns:
                return 0.0
            x = closes[u].pct_change(fill_method=None).get(day)
            x = 0.0 if x is None or pd.isna(x) else float(x)
            return float(pnl_for_returns(h, np.array([x]))[0])
        out.append(run(f"Worst day {str(day)[:10]}", "historical", str(day)[:10], _r(ret * 100, 2), fn))
    return out
