"""Published-strategy catalog on the rotation_lab engine (docs/STRATEGY_CATALOG.md has sources/rules).

Timing: every signal uses data <= close[t]; target weights W[t] are traded at open[t+1] (engine.run).
Only data <= 2025-11-23 is ever loaded (locked holdout). Cash = BIL (remainder of weights).
Long-only, no leverage (cash account): long/short factor legs are implemented as their long leg only.
"""
from __future__ import annotations
import hashlib, json, math, time
from pathlib import Path
import numpy as np
import pandas as pd
from .engine import run, rebal_flags, SLIP, SEC_FEE, TAF_PER_SHARE, ANN
from .sweep import (ROOT, EXT, RESEARCH_END, EVAL_START, read_csv, metrics, deflated_sharpe, Data)

LONG = EXT / "long"
EXP = ROOT / "data" / "experiments"
REGISTRY = EXP / "trials_registry.csv"
REG_COLS = "ts,lab,strategy,config_hash,universe,start,end,cagr,sharpe,maxdd,turnover,notes".split(",")
PRIOR_TRIALS = 304

# ----------------------------------------------------------------------------- panel
class Panel:
    def __init__(self, o: pd.DataFrame, c: pd.DataFrame):
        self.idx = o.index
        self.o = o
        self.c = c.ffill(limit=5)
        self.T = len(self.idx)

    def has(self, syms):
        return all(s in self.c.columns and self.c[s].notna().any() for s in syms)


def load_etfs(syms) -> Panel:
    raw = {}
    for s in dict.fromkeys(list(syms) + ["SPY", "BIL"]):
        p = LONG / f"{s}.csv"
        p = p if p.exists() else EXT / f"{s}.csv"
        if p.exists():
            raw[s] = read_csv(p)
    idx = raw["SPY"].index
    idx = idx[idx <= RESEARCH_END]
    o = pd.DataFrame({s: d["open"].reindex(idx) for s, d in raw.items()})
    c = pd.DataFrame({s: d["close"].reindex(idx) for s, d in raw.items()})
    return Panel(o, c)


def stock_panel() -> Panel:
    d = Data()
    cols = d.univ["sp500"] + [x for x in ("SPY", "BIL") if x not in d.univ["sp500"]]
    return Panel(d.open[cols], d.close[cols])

# ----------------------------------------------------------------------------- evaluation
def evaluate(P: Panel, W: pd.DataFrame, flag: np.ndarray):
    cols = [c for c in W.columns if c != "BIL"]
    cash = P.o["BIL"].values
    O = np.column_stack([P.o[cols].values, cash]) if cols else cash[:, None]
    C = np.column_stack([P.c[cols].values, P.c["BIL"].values]) if cols else P.c["BIL"].values[:, None]
    Wm = np.column_stack([W[cols].values, 1.0 - W[cols].values.sum(axis=1)]) if cols else np.ones((P.T, 1))
    r, turn = run(O, C, Wm, flag)
    return pd.Series(r, index=P.idx), pd.Series(turn, index=P.idx)


def build_rebal(P: Panel, rowfn, freq="monthly"):
    """rowfn(t)->dict sym->weight (remainder cash) or None (no signal yet). Rebalance at period end.
    M17: on None the PREVIOUS holdings are kept (cash only before the first signal); docstring fixed
    rather than behaviour, so published research results are unchanged."""
    fl = rebal_flags(P.idx, freq)
    recs = {int(t): rowfn(int(t)) for t in np.where(fl)[0]}
    cols = sorted({k for d in recs.values() if d for k in d if k != "BIL"})
    W = np.zeros((P.T, len(cols))); flag = np.zeros(P.T, bool); cur = np.zeros(len(cols))
    pos = {c: i for i, c in enumerate(cols)}
    for t in range(P.T):
        if t in recs and recs[t] is not None:
            cur = np.zeros(len(cols))
            for k, v in recs[t].items():
                if k != "BIL":
                    cur[pos[k]] = v
            flag[t] = True
        W[t] = cur
    return pd.DataFrame(W, index=P.idx, columns=cols), flag


def build_daily(P: Panel, Wdf: pd.DataFrame):
    W = Wdf.fillna(0.0)
    d = W.diff().abs().sum(axis=1).values > 1e-9
    d[0] = False
    return W, d

# ----------------------------------------------------------------------------- feature helpers
def rl(c, n):  # n-day return
    return c / c.shift(n) - 1.0


def m13612w(c):
    return 12 * rl(c, 21) + 4 * rl(c, 63) + 2 * rl(c, 126) + rl(c, 252)


def m13612(c):
    return (rl(c, 21) + rl(c, 63) + rl(c, 126) + rl(c, 252)) / 4.0


def sma12_rel(c):  # Keller BAA: p0 / SMA(13 month-ends incl. today) - 1 ~ 13*21d... daily approximation
    return c / c.rolling(252, min_periods=252).mean() - 1.0


def monthend_table(P: Panel, syms):
    """Month-end closes (strict last trading day of month) -> used for exact monthly SMA rules."""
    fl = rebal_flags(P.idx, "monthly")
    ix = np.where(fl)[0]
    return ix, pd.DataFrame(P.c[syms].values[ix], index=ix, columns=syms)


def fixed(P, weights, freq="monthly"):
    return build_rebal(P, lambda t: dict(weights), freq)


def _g(df, t, s):
    v = df[s].values[t]
    return v

# ----------------------------------------------------------------------------- TAA strategies
GTAA5 = ["SPY", "EFA", "IEF", "DBC", "VNQ"]
F13 = ["VTI", "IJR", "EFA", "EEM", "VNQ", "DBC", "GLD", "TLT", "IEF", "TIP", "LQD", "HYG", "AGG"]


def faber(P, assets=GTAA5, sma=10, topn=0, w_ret=(1, 3, 6, 12)):
    ix, M = monthend_table(P, assets)
    S = M.rolling(sma, min_periods=sma).mean()
    R = sum(M / M.shift(k) - 1 for k in w_ret) / len(w_ret)
    pos = {t: i for i, t in enumerate(ix)}

    def row(t):
        i = pos[t]
        if S.iloc[i].isna().any() or R.iloc[i].isna().any():
            return None
        ok = M.iloc[i] > S.iloc[i]
        if topn:
            sel = R.iloc[i].sort_values(ascending=False).index[:topn]
            return {s: 1.0 / topn for s in sel if ok[s]}
        return {s: 1.0 / len(assets) for s in assets if ok[s]}
    return build_rebal(P, row)


def gem(P, us="SPY", intl="EFA", bond="AGG", lb=252, extra=None):
    c = P.c
    cand = [us, intl] + ([extra] if extra else [])
    r = {s: rl(c[s], lb) for s in cand + ["BIL"]}

    def row(t):
        v = {s: r[s].values[t] for s in r}
        if any(np.isnan(x) for x in v.values()):
            return None
        if v[us] > v["BIL"]:
            return {max(cand, key=lambda s: v[s]): 1.0}
        return {bond: 1.0}
    return build_rebal(P, row)


def adm(P, a="SPY", b="EFA", defn="TLT"):
    c = P.c
    sc = {s: (rl(c[s], 21) + rl(c[s], 63) + rl(c[s], 126)) / 3 for s in (a, b)}

    def row(t):
        v = {s: sc[s].values[t] for s in sc}
        if any(np.isnan(x) for x in v.values()):
            return None
        best = max(v, key=v.get)
        return {best: 1.0} if v[best] > 0 else {defn: 1.0}
    return build_rebal(P, row)


def vaa(P, off, deff, B, topo, mom="13612w"):
    c = P.c
    f = m13612w if mom == "13612w" else sma12_rel
    mo = {s: f(c[s]) for s in off + deff}

    def row(t):
        vo = {s: mo[s].values[t] for s in off}; vd = {s: mo[s].values[t] for s in deff}
        if any(np.isnan(x) for x in list(vo.values()) + list(vd.values())):
            return None
        b = sum(1 for x in vo.values() if x <= 0)
        cf = min(1.0, b / B)
        w = {}
        bd = max(vd, key=vd.get)
        w[bd] = cf
        for s in sorted(vo, key=vo.get, reverse=True)[:topo]:
            w[s] = w.get(s, 0) + (1 - cf) / topo
        return w
    return build_rebal(P, row)


G12 = ["SPY", "IWM", "QQQ", "VGK", "EWJ", "VWO", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD"]


def daa(P, off=G12, deff=("SHY", "IEF", "LQD"), canary=("VWO", "BND"), topo=6, B=None):
    c = P.c
    B = B or len(canary)
    mo = {s: m13612w(c[s]) for s in set(off) | set(deff) | set(canary)}

    def row(t):
        allv = {s: mo[s].values[t] for s in mo}
        if any(np.isnan(x) for x in allv.values()):
            return None
        b = sum(1 for s in canary if allv[s] <= 0)
        cf = min(1.0, b / B)
        bd = max(deff, key=lambda s: allv[s])
        w = {}
        if allv[bd] > 0:
            w[bd] = cf
        for s in sorted(off, key=lambda s: allv[s], reverse=True)[:topo]:
            w[s] = w.get(s, 0) + (1 - cf) / topo
        return w
    return build_rebal(P, row)


def baa(P, off=("SPY", "QQQ", "IWM", "VEA", "VWO", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD", "IEF"),
        deff=("TLT", "IEF", "LQD", "TIP", "BND", "DBC", "BIL"), canary=("SPY", "VEA", "VWO", "BND"), topo=6, topd=3):
    c = P.c
    syms = set(off) | set(deff) | set(canary)
    m1 = {s: m13612w(c[s]) for s in canary}
    m2 = {s: sma12_rel(c[s]) for s in syms}

    def row(t):
        if any(np.isnan(m1[s].values[t]) for s in canary) or any(np.isnan(m2[s].values[t]) for s in syms):
            return None
        if all(m1[s].values[t] > 0 for s in canary):
            return {s: 1.0 / topo for s in sorted(off, key=lambda s: m2[s].values[t], reverse=True)[:topo]}
        w = {}
        bil = m2["BIL"].values[t]
        for s in sorted(deff, key=lambda s: m2[s].values[t], reverse=True)[:topd]:
            tgt = s if m2[s].values[t] >= bil else "BIL"
            w[tgt] = w.get(tgt, 0) + 1.0 / topd
        return w
    return build_rebal(P, row)


def haa(P, off=("SPY", "IWM", "VEA", "VWO", "DBC", "VNQ", "TLT", "IEF"), canary="TIP", topo=4):
    c = P.c
    mo = {s: m13612(c[s]) for s in set(off) | {canary, "IEF", "BIL"}}

    def row(t):
        v = {s: mo[s].values[t] for s in mo}
        if any(np.isnan(x) for x in v.values()):
            return None
        if v[canary] > 0:
            w = {}
            for s in sorted(off, key=lambda s: v[s], reverse=True)[:topo]:
                tgt = s if v[s] > 0 else ("IEF" if v["IEF"] > v["BIL"] else "BIL")
                w[tgt] = w.get(tgt, 0) + 1.0 / topo
            return w
        return {"IEF": 1.0} if v["IEF"] > v["BIL"] else {"BIL": 1.0}
    return build_rebal(P, row)


PAA12 = ["SPY", "QQQ", "IWM", "VGK", "EWJ", "EEM", "VNQ", "DBC", "GLD", "HYG", "LQD", "TLT"]


def paa(P, a=2, top=6, off=PAA12):
    c = P.c
    mo = {s: sma12_rel(c[s]) for s in off + ["IEF", "BIL"]}
    N = len(off); n1 = a * N / 4

    def row(t):
        v = {s: mo[s].values[t] for s in mo}
        if any(np.isnan(x) for x in v.values()):
            return None
        n = sum(1 for s in off if v[s] > 0)
        bf = 1.0 if n <= n1 else (N - n) / (N - n1)
        bf = min(1.0, max(0.0, bf))
        w = {("IEF" if v["IEF"] > v["BIL"] else "BIL"): bf}
        for s in sorted(off, key=lambda s: v[s], reverse=True)[:top]:
            w[s] = w.get(s, 0) + (1 - bf) / top
        return w
    return build_rebal(P, row)


def trend_overlay(P, legs=(("SPY", .6), ("IEF", .4)), sma=10):
    ix, M = monthend_table(P, [s for s, _ in legs]); S = M.rolling(sma, min_periods=sma).mean()
    pos = {t: i for i, t in enumerate(ix)}

    def row(t):
        i = pos[t]
        if S.iloc[i].isna().any():
            return None
        return {s: w for s, w in legs if M.iloc[i][s] > S.iloc[i][s]}
    return build_rebal(P, row)


def risk_parity(P, assets=("SPY", "TLT", "GLD", "DBC"), lb=252):
    vol = P.c[list(assets)].pct_change(fill_method=None).rolling(lb, min_periods=lb).std()

    def row(t):
        v = vol.values[t]
        if np.isnan(v).any():
            return None
        w = 1.0 / v
        return dict(zip(assets, w / w.sum()))
    return build_rebal(P, row)

# ----------------------------------------------------------------------------- calendar / SPY timing
def _mask_weights(P, mask, asset, off=None):
    """mask[k] True = hold day k (open[k]->open[k+1] return). W[t] set for day t+1."""
    m = np.zeros(P.T, bool); m[:-1] = mask[1:]
    cols = {asset: m.astype(float)}
    if off:
        cols[off] = (~m).astype(float)
    return build_daily(P, pd.DataFrame(cols, index=P.idx))


def _month_pos(idx):
    s = pd.Series(np.arange(len(idx)), index=idx)
    key = idx.year * 100 + idx.month
    g = s.groupby(key)
    first = (s - g.transform("min") + 1).values
    last = (g.transform("max") - s + 1).values
    return first, last


def tom(P, a=1, b=3, asset="SPY"):
    f, l = _month_pos(P.idx)
    return _mask_weights(P, (l <= a) | (f <= b), asset)


def preholiday(P, asset="SPY", after=False):
    idx = P.idx
    miss = pd.bdate_range(idx[0], idx[-1]).difference(idx)
    pos = idx.searchsorted(miss)  # position of the day after the holiday
    m = np.zeros(len(idx), bool)
    for p in pos:
        if 0 < p < len(idx):
            m[p - 1] = True
            if after:
                m[p] = True
    return _mask_weights(P, m, asset)


def halloween(P, asset="SPY", off=None, start_m=11, end_m=4):
    mo = P.idx.month.values
    mask = (mo >= start_m) | (mo <= end_m) if start_m > end_m else (mo >= start_m) & (mo <= end_m)
    return _mask_weights(P, mask, asset, off)


def overnight_intraday(P, asset="SPY", kind="overnight"):
    """Round trip every day (2 trades/day). Returns series directly. Costs: slip both sides, SEC on sells, TAF."""
    o, c = P.o[asset].values, P.c[asset].values
    r = np.zeros(P.T); turn = np.zeros(P.T)
    taf = TAF_PER_SHARE / np.where(o > 0, o, 1)
    for k in range(1, P.T):
        if kind == "overnight":
            g = o[k] / c[k - 1] - 1; sell_px_taf = taf[k]
        else:
            g = c[k] / o[k] - 1; sell_px_taf = TAF_PER_SHARE / max(c[k], 1)
        cost = 2 * SLIP + SEC_FEE + sell_px_taf
        r[k] = (1 - cost) * (1 + g) - 1 if np.isfinite(g) else 0.0
        turn[k] = 2.0
    return pd.Series(r, index=P.idx), pd.Series(turn, index=P.idx)


def volmanaged(P, asset="SPY", target=0.12, window=21, cap=1.0):
    rv = P.c[asset].pct_change(fill_method=None).rolling(window, min_periods=window).std() * math.sqrt(ANN)

    def row(t):
        v = rv.values[t]
        return None if np.isnan(v) else {asset: min(cap, target / v)}
    return build_rebal(P, row)


def dual_ma(P, asset="SPY", fast=50, slow=200, off=None):
    c = P.c[asset]
    sig = (c.rolling(fast, min_periods=fast).mean() > c.rolling(slow, min_periods=slow).mean()).astype(float)
    sig[c.rolling(slow, min_periods=slow).mean().isna()] = 0.0
    cols = {asset: sig}
    if off:
        cols[off] = 1 - sig
    return build_daily(P, pd.DataFrame(cols))


def _donch_state(c, n_in, n_out):
    v = c.values; T = len(v); st = np.zeros(T); on = 0.0
    hi = pd.Series(v).shift(1).rolling(n_in, min_periods=n_in).max().values
    lo = pd.Series(v).shift(1).rolling(n_out, min_periods=n_out).min().values
    for t in range(T):
        if on == 0.0 and np.isfinite(hi[t]) and v[t] > hi[t]:
            on = 1.0
        elif on == 1.0 and np.isfinite(lo[t]) and v[t] < lo[t]:
            on = 0.0
        st[t] = on
    return st


def donchian(P, assets=("SPY",), n_in=55, n_out=20):
    cols = {s: _donch_state(P.c[s], n_in, n_out) / len(assets) for s in assets}
    return build_daily(P, pd.DataFrame(cols, index=P.idx))


TREND8 = ["SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]


def vol_trend(P, assets=TREND8, sma=200, tv=0.10, lb=63, gross=1.0):
    c = P.c[list(assets)]
    sm = c.rolling(sma, min_periods=sma).mean()
    vol = c.pct_change(fill_method=None).rolling(lb, min_periods=lb).std() * math.sqrt(ANN)

    def row(t):
        v = vol.values[t]; up = (c.values[t] > sm.values[t]) & ~np.isnan(sm.values[t])
        if np.isnan(v).any():
            return None
        w = np.where(up, tv / v / len(assets), 0.0)
        if w.sum() > gross:
            w = w / w.sum() * gross
        return dict(zip(assets, w))
    return build_rebal(P, row)


def tsmom(P, assets=TREND8, lb=252, tv=0.10, longshort=False):
    c = P.c[list(assets)]
    vol = c.pct_change(fill_method=None).ewm(com=60, min_periods=60).std() * math.sqrt(ANN)
    ret = rl(c, lb)

    def row(t):
        v, r = vol.values[t], ret.values[t]
        if np.isnan(v).any() or np.isnan(r).any():
            return None
        w = np.where(r > 0, tv / v / len(assets), 0.0)
        if w.sum() > 1:
            w = w / w.sum()
        return dict(zip(assets, w))
    return build_rebal(P, row)

# ----------------------------------------------------------------------------- stock cross-sectional (long leg only)
def _topk(P, S: pd.DataFrame, frac=0.1, k=None, regime=None, bottom=False, freq="monthly"):
    A = S.values
    spy_sma = P.c["SPY"].rolling(regime, min_periods=regime).mean().values if regime else None
    spy = P.c["SPY"].values

    def row(t):
        a = A[t]
        fin = np.isfinite(a)
        if fin.sum() < 50:
            return None
        if regime and not (spy[t] > spy_sma[t]):
            return {}
        kk = k or max(1, int(fin.sum() * frac))
        a2 = np.where(fin, a, -np.inf if not bottom else np.inf)
        ix = np.argsort(a2)[:kk] if bottom else np.argsort(-a2)[:kk]
        return {S.columns[i]: 1.0 / kk for i in ix}
    return build_rebal(P, row, freq)


def _stocks(P):
    return [c for c in P.c.columns if c not in ("SPY", "BIL")]


def s_mom(P, lb=252, skip=21, **kw):
    c = P.c[_stocks(P)]
    return _topk(P, c.shift(skip) / c.shift(lb) - 1, **kw)


def s_rev(P, lb=21, freq="monthly", **kw):
    c = P.c[_stocks(P)]
    return _topk(P, c / c.shift(lb) - 1, bottom=True, freq=freq, **kw)


def s_lowvol(P, lb=252, **kw):
    c = P.c[_stocks(P)]
    return _topk(P, c.pct_change(fill_method=None).rolling(lb, min_periods=lb).std(), bottom=True, **kw)


def _beta(P, lb):
    r = P.c[_stocks(P)].pct_change(fill_method=None); m = P.c["SPY"].pct_change(fill_method=None)
    return r.rolling(lb, min_periods=lb).cov(m).div(m.rolling(lb, min_periods=lb).var(), axis=0), r, m


def s_lowbeta(P, lb=252, **kw):
    b, _, _ = _beta(P, lb)
    return _topk(P, b, bottom=True, **kw)


def s_52wh(P, lb=252, **kw):
    c = P.c[_stocks(P)]
    return _topk(P, c / c.rolling(lb, min_periods=lb).max(), **kw)


def s_resmom(P, lb=252, skip=21, scale=True, **kw):
    b, r, m = _beta(P, lb)
    n = lb - skip
    S = r.rolling(n, min_periods=n).sum().shift(skip) - b.mul(m.rolling(n, min_periods=n).sum().shift(skip), axis=0)
    if scale:
        corr = r.rolling(lb, min_periods=lb).corr(m)
        S = S / (r.rolling(lb, min_periods=lb).std() * np.sqrt((1 - corr ** 2).clip(lower=1e-4)))
    return _topk(P, S, **kw)

# ----------------------------------------------------------------------------- catalog table
def _A(**k):
    return k

SRC = {
    "faber": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461",
    "gem": "https://www.optimalmomentum.com/ (Antonacci, Dual Momentum Investing 2014)",
}
# id, group, fn, universe label, base kwargs, variants (<=7)
CATALOG = [
    ("faber_gtaa5", "taa", faber, "SPY,EFA,IEF,DBC,VNQ", {}, [_A(sma=6), _A(sma=8), _A(sma=12), _A(assets=GTAA5 + ["GLD", "EEM"]), _A(assets=["SPY", "EFA", "TLT", "GLD", "VNQ"])]),
    ("faber_top6of13", "taa", faber, "13 ETFs", dict(assets=F13, topn=6), [_A(topn=4), _A(topn=8), _A(sma=8), _A(sma=12), _A(w_ret=(3, 6, 12)), _A(w_ret=(6,))]),
    ("gem_antonacci", "taa", gem, "SPY,EFA,AGG", {}, [_A(lb=126), _A(lb=189), _A(intl="VEA"), _A(bond="IEF"), _A(bond="TLT"), _A(extra="EEM"), _A(extra="GLD")]),
    ("accel_dual_mom", "taa", adm, "SPY,EFA,TLT", {}, [_A(defn="IEF"), _A(defn="AGG"), _A(defn="BIL"), _A(b="EEM"), _A(a="QQQ")]),
    ("vaa_g4", "taa", vaa, "SPY,EFA,EEM,AGG|LQD,IEF,SHY", dict(off=["SPY", "EFA", "EEM", "AGG"], deff=["LQD", "IEF", "SHY"], B=1, topo=1),
     [_A(topo=2, B=2), _A(mom="sma12"), _A(deff=["IEF", "SHY"]), _A(off=["SPY", "VEA", "VWO", "AGG"]), _A(B=2)]),
    ("vaa_g12", "taa", vaa, "12 ETFs|LQD,IEF,SHY", dict(off=G12, deff=["LQD", "IEF", "SHY"], B=4, topo=2),
     [_A(topo=3), _A(topo=5), _A(B=3), _A(B=6), _A(mom="sma12"), _A(topo=1, B=2)]),
    ("daa_g12", "taa", daa, "G12|canary VWO,BND", {}, [_A(topo=3), _A(topo=4), _A(topo=8), _A(canary=("VWO", "BND", "SPY")), _A(deff=("SHY", "IEF")), _A(B=1)]),
    ("baa_g12", "taa", baa, "BAA-G12", {}, [_A(topo=4), _A(topo=8), _A(topd=1), _A(topd=2), _A(canary=("SPY", "VEA", "VWO", "BND", "TLT"))]),
    ("baa_g4", "taa", baa, "QQQ,VEA,VWO,AGG", dict(off=("QQQ", "VEA", "VWO", "AGG"), topo=1), [_A(topo=2), _A(topd=1), _A(topd=2), _A(canary=("SPY", "VWO", "BND"))]),
    ("haa", "taa", haa, "HAA", {}, [_A(topo=3), _A(topo=5), _A(canary="BND"), _A(canary="AGG"), _A(off=("SPY", "IWM", "VEA", "VWO", "DBC", "VNQ", "TLT", "IEF", "GLD"))]),
    ("paa", "taa", paa, "PAA12", {}, [_A(a=0), _A(a=1), _A(top=4), _A(top=3, a=2), _A(top=6, a=1)]),
    ("permanent_portfolio", "static", fixed, "SPY,TLT,GLD,BIL", dict(weights={"SPY": .25, "TLT": .25, "GLD": .25}), [_A(freq="weekly"), _A(weights={"SPY": .25, "IEF": .25, "GLD": .25}), _A(weights={"VTI": .25, "TLT": .25, "GLD": .25})]),
    ("all_weather", "static", fixed, "VTI,TLT,IEF,GLD,DBC", dict(weights={"VTI": .3, "TLT": .4, "IEF": .15, "GLD": .075, "DBC": .075}), [_A(weights={"VTI": .3, "TLT": .4, "IEF": .15, "GLD": .15}), _A(weights={"VTI": .3, "TLT": .55, "GLD": .075, "DBC": .075}), _A(weights={"VTI": .4, "TLT": .3, "IEF": .15, "GLD": .075, "DBC": .075})]),
    ("risk_parity", "static", risk_parity, "SPY,TLT,GLD,DBC", {}, [_A(lb=126), _A(lb=63), _A(assets=("SPY", "TLT", "GLD")), _A(assets=("SPY", "TLT", "GLD", "DBC", "VNQ")), _A(assets=("SPY", "EFA", "TLT", "IEF", "GLD", "DBC"))]),
    ("golden_butterfly", "static", fixed, "VTI,VBR,TLT,SHY,GLD", dict(weights={"VTI": .2, "VBR": .2, "TLT": .2, "SHY": .2, "GLD": .2}), [_A(weights={"SPY": .2, "IJR": .2, "TLT": .2, "SHY": .2, "GLD": .2}), _A(weights={"VTI": .2, "VBR": .2, "TLT": .2, "IEF": .2, "GLD": .2})]),
    ("sixty40_trend", "taa", trend_overlay, "SPY,IEF", {}, [_A(sma=8), _A(sma=12), _A(legs=(("SPY", .5), ("IEF", .5))), _A(legs=(("SPY", .6), ("TLT", .4))), _A(legs=(("SPY", .7), ("IEF", .3)))]),
    # --- calendar / timing on SPY
    ("turn_of_month", "calendar", tom, "SPY", {}, [_A(a=2, b=3), _A(a=1, b=4), _A(a=3, b=3), _A(a=1, b=2), _A(asset="QQQ"), _A(asset="IWM")]),
    ("window_dressing_eom", "calendar", tom, "SPY", dict(a=3, b=0), [_A(a=2, b=0), _A(a=4, b=0), _A(a=5, b=0), _A(a=3, b=0, asset="QQQ")]),
    ("pre_holiday", "calendar", preholiday, "SPY", {}, [_A(after=True), _A(asset="QQQ"), _A(asset="IWM")]),
    ("overnight_spy", "calendar", overnight_intraday, "SPY", dict(kind="overnight"), [_A(asset="QQQ"), _A(asset="IWM")]),
    ("intraday_spy", "calendar", overnight_intraday, "SPY", dict(kind="intraday"), [_A(asset="QQQ")]),
    ("halloween_sell_in_may", "calendar", halloween, "SPY", {}, [_A(off="IEF"), _A(off="TLT"), _A(asset="QQQ"), _A(asset="EFA"), _A(start_m=10, end_m=5), _A(start_m=12, end_m=3)]),
    ("vol_managed_spy", "trend", volmanaged, "SPY", {}, [_A(target=0.10), _A(target=0.15), _A(target=0.18), _A(window=63), _A(window=10), _A(asset="QQQ")]),
    # --- trend / CTA
    ("dual_ma_50_200", "trend", dual_ma, "SPY", {}, [_A(fast=20, slow=100), _A(fast=50, slow=100), _A(fast=100, slow=200), _A(off="IEF"), _A(off="TLT"), _A(asset="QQQ"), _A(fast=10, slow=200)]),
    ("donchian_55_20", "trend", donchian, "SPY", {}, [_A(n_in=20, n_out=10), _A(n_in=100, n_out=50), _A(n_in=55, n_out=55), _A(assets=tuple(TREND8)), _A(assets=tuple(TREND8), n_in=20, n_out=10), _A(assets=tuple(TREND8), n_in=100, n_out=50)]),
    ("vol_targeted_trend_etf", "trend", vol_trend, "8 ETFs", {}, [_A(tv=0.07), _A(tv=0.15), _A(sma=100), _A(sma=150), _A(lb=21), _A(gross=0.6)]),
    ("tsmom_moskowitz", "trend", tsmom, "8 ETFs", {}, [_A(lb=63), _A(lb=126), _A(tv=0.07), _A(tv=0.15), _A(assets=["SPY", "EFA", "EEM", "TLT", "IEF", "GLD"])]),
    # --- stock cross-sectional (survivorship-biased universe)
    ("mom_12_1", "stock", s_mom, "sp500(today)", {}, [_A(frac=0.2), _A(k=20), _A(lb=126), _A(lb=189), _A(skip=0), _A(regime=200), _A(frac=0.05)]),
    ("st_reversal_1m", "stock", s_rev, "sp500(today)", {}, [_A(frac=0.2), _A(lb=5, freq="weekly"), _A(lb=10, freq="weekly"), _A(regime=200), _A(k=20)]),
    ("low_vol", "stock", s_lowvol, "sp500(today)", dict(frac=0.2), [_A(frac=0.1), _A(lb=126), _A(lb=63), _A(regime=200), _A(k=30)]),
    ("low_beta_bab_long", "stock", s_lowbeta, "sp500(today)", dict(frac=0.2), [_A(frac=0.1), _A(lb=126), _A(lb=63), _A(regime=200), _A(k=30)]),
    ("high_52w", "stock", s_52wh, "sp500(today)", dict(frac=0.3), [_A(frac=0.1), _A(frac=0.2), _A(lb=126), _A(regime=200), _A(k=20)]),
    ("residual_mom", "stock", s_resmom, "sp500(today)", dict(frac=0.1), [_A(frac=0.2), _A(scale=False), _A(lb=189), _A(regime=200), _A(k=20)]),
]
# metals / currencies set (separate table)
def mf_trend(P, assets, sma=10):
    return faber(P, assets=list(assets), sma=sma)


def mf_dualmom(P, cand=("SPY", "EFA", "GLD"), lb=252, bond="AGG"):
    c = P.c; r = {s: rl(c[s], lb) for s in list(cand) + ["BIL"]}

    def row(t):
        v = {s: r[s].values[t] for s in r}
        if any(np.isnan(x) for x in v.values()):
            return None
        b = max(cand, key=lambda s: v[s])
        return {b: 1.0} if v[b] > v["BIL"] else {bond: 1.0}
    return build_rebal(P, row)


def mf_gsr(P, sma=12, trend=10, gl="GLD", sl="SLV"):
    """Gold/silver ratio switch: ratio below its SMA (silver outperforming) -> SLV else GLD; held only if above own trend SMA."""
    ix, M = monthend_table(P, [gl, sl]); ratio = M[gl] / M[sl]; rs = ratio.rolling(sma, min_periods=sma).mean()
    S = M.rolling(trend, min_periods=trend).mean(); pos = {t: i for i, t in enumerate(ix)}

    def row(t):
        i = pos[t]
        if np.isnan(rs.iloc[i]) or S.iloc[i].isna().any():
            return None
        s = sl if ratio.iloc[i] < rs.iloc[i] else gl
        return {s: 1.0} if M.iloc[i][s] > S.iloc[i][s] else {}
    return build_rebal(P, row)


def mf_rank(P, cand=("GLD", "SLV", "UUP", "GDX"), lb=126, top=1):
    c = P.c; r = {s: rl(c[s], lb) for s in cand}

    def row(t):
        v = {s: r[s].values[t] for s in cand}
        if any(np.isnan(x) for x in v.values()):
            return None
        sel = [s for s in sorted(v, key=v.get, reverse=True)[:top] if v[s] > 0]
        return {s: 1.0 / top for s in sel}
    return build_rebal(P, row)


FX6 = ("FXE", "FXY", "FXB", "FXA", "FXC", "FXF")
METALS_FX = [
    ("mf_gld_sma10", "metals_fx", mf_trend, "GLD", dict(assets=("GLD",)), [_A(sma=6), _A(sma=8), _A(sma=12), _A(assets=("SLV",)), _A(assets=("GDX",))]),
    ("mf_gld_slv_uup_trend", "metals_fx", mf_trend, "GLD,SLV,UUP", dict(assets=("GLD", "SLV", "UUP")), [_A(sma=6), _A(sma=8), _A(sma=12), _A(assets=("GLD", "SLV")), _A(assets=("GLD", "SLV", "GDX"))]),
    ("mf_dualmom_gld", "metals_fx", mf_dualmom, "SPY,EFA,GLD|AGG", {}, [_A(lb=126), _A(lb=189), _A(cand=("SPY", "EFA", "GLD", "SLV")), _A(cand=("SPY", "EFA", "GLD", "UUP")), _A(bond="IEF"), _A(cand=("SPY", "GLD"))]),
    ("mf_gold_silver_ratio", "metals_fx", mf_gsr, "GLD,SLV", {}, [_A(sma=6), _A(sma=9), _A(sma=18), _A(trend=8), _A(trend=12)]),
    ("mf_fx_trend_6", "metals_fx", mf_trend, "FXE,FXY,FXB,FXA,FXC,FXF", dict(assets=FX6), [_A(sma=6), _A(sma=8), _A(sma=12), _A(assets=("FXE", "FXY", "FXB", "FXA"))]),
    ("mf_metals_fx_momrank", "metals_fx", mf_rank, "GLD,SLV,UUP,GDX", {}, [_A(lb=63), _A(lb=252), _A(top=2), _A(cand=("GLD", "SLV", "UUP", "FXE", "FXY")), _A(cand=("GLD", "SLV"))]),
]

# ----------------------------------------------------------------------------- runner
def cfg_hash(strategy, kw):
    return hashlib.md5((strategy + json.dumps(kw, sort_keys=True, default=str)).encode()).hexdigest()[:10]


def registry_rows():
    if not REGISTRY.exists():
        return pd.DataFrame(columns=REG_COLS)
    return pd.read_csv(REGISTRY, on_bad_lines="skip")


def append_registry(rows: list[dict]):
    EXP.mkdir(parents=True, exist_ok=True)
    new = not REGISTRY.exists() or REGISTRY.stat().st_size == 0
    have = set(zip(registry_rows().get("lab", []), registry_rows().get("strategy", []), registry_rows().get("config_hash", [])))
    with open(REGISTRY, "a", newline="", encoding="utf8") as f:
        import csv
        w = csv.DictWriter(f, fieldnames=REG_COLS)
        if new:
            w.writeheader()
        for r in rows:
            if ("rotation_catalog", r["strategy"], r["config_hash"]) not in have:
                w.writerow(r)


def total_trials():
    g = registry_rows().drop_duplicates(subset=["lab", "strategy", "config_hash"])
    return len(g) + PRIOR_TRIALS


def run_catalog(out_dir: Path):
    t0 = time.time(); out_dir.mkdir(parents=True, exist_ok=True)
    need = set()
    import re
    for e in CATALOG + METALS_FX:
        need |= set(re.findall(r"[A-Z]{3,5}", e[3]))
    allsyms = ["SPY", "EFA", "EEM", "VEA", "VWO", "AGG", "BND", "LQD", "IEF", "SHY", "TLT", "TIP", "HYG", "GLD", "DBC", "VNQ", "VTI", "VBR", "IJR", "QQQ", "IWM", "VGK", "EWJ",
               "SLV", "UUP", "GDX", "FXE", "FXY", "FXB", "FXA", "FXC", "FXF", "BIL"]
    P = load_etfs(allsyms); PS = stock_panel()
    spy_r, spy_t = evaluate(P, pd.DataFrame({"SPY": np.r_[1.0, np.ones(P.T - 1)]}, index=P.idx), np.r_[True, np.zeros(P.T - 1, bool)])
    bench = {"SPY_buyhold": (spy_r, spy_t), "60_40_SPY_IEF": evaluate(P, *build_rebal(P, lambda t: {"SPY": .6, "IEF": .4}))}
    rows, rets = [], {}
    for k, (r, t) in bench.items():
        rows.append(dict(name=k, group="benchmark", published=True, **metrics(r, t, spy_r))); rets[k] = r
    skipped = []
    for sid, grp, fn, univ, base, variants in CATALOG + METALS_FX:
        panel = PS if grp == "stock" else P
        for vi, v in enumerate([{}] + variants):
            kw = {**base, **v}
            name = sid if vi == 0 else f"{sid}#v{vi}"
            try:
                res = fn(panel, **kw)
                if isinstance(res[0], pd.Series):
                    r, t = res
                elif isinstance(res[0], pd.DataFrame):
                    r, t = evaluate(panel, *res)
                else:
                    raise ValueError("bad result")
            except Exception as ex:  # missing symbol etc
                skipped.append((name, repr(ex)[:90])); continue
            m = metrics(r, t, spy_r)
            rows.append(dict(name=name, strategy=sid, group=grp, published=(vi == 0), universe=univ, params=json.dumps(v, default=str),
                             config_hash=cfg_hash(sid, kw), **m)); rets[name] = r
        print(sid, "done", f"{time.time() - t0:.0f}s", flush=True)
    df = pd.DataFrame(rows)
    ev = lambda k: rets[k][rets[k].index >= EVAL_START]
    trial = df[df.group != "benchmark"]
    reg = [dict(ts=pd.Timestamp.now().isoformat(timespec="seconds"), lab="rotation_catalog", strategy=r["name"], config_hash=r["config_hash"],
                universe=r["universe"], start=str(EVAL_START.date()), end=str(RESEARCH_END.date()), cagr=round(r["cagr"], 4), sharpe=round(r["sharpe"], 3),
                maxdd=round(r["maxdd"], 4), turnover=round(r["turnover_yr"], 2), notes=("published " if r["published"] else "variant ") + r["group"])
           for _, r in trial.iterrows()]
    append_registry(reg)
    N = total_trials()
    srs = np.array([ev(k).mean() / ev(k).std() for k in trial.name])
    srvar = max(float(np.nanvar(srs)), 1.728e-4)
    df["dsr"] = [deflated_sharpe(ev(k), N, srvar) if k in set(trial.name) else np.nan for k in df.name]
    df["n_trials_total"] = N
    df.sort_values("dsr", ascending=False).to_csv(out_dir / "catalog_leaderboard.csv", index=False)
    pd.DataFrame(rets).to_csv(out_dir / "catalog_daily_returns.csv.gz")
    # append to main rotation leaderboard (columns aligned)
    lb = out_dir / "leaderboard.csv"
    if lb.exists():
        old = pd.read_csv(lb)
        old = old[old.get("kind", "") != "catalog"]
        add = df[df.group != "benchmark"].assign(kind="catalog")
        pd.concat([old, add[[c for c in add.columns if c in old.columns or c in ("group", "published", "strategy", "params", "config_hash", "n_trials_total")]]], ignore_index=True).to_csv(lb, index=False)
    return df, N, srvar, skipped, time.time() - t0
