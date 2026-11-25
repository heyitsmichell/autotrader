"""Momentum-variant lab on the rotation_lab engine (costs, next-open execution, metrics, DSR reused).

All score functions take a close DataFrame and return a same-shaped DataFrame; row t uses data <= t only.
Weights built at rebalance close t are traded at open t+1 by engine.run (via catalog.evaluate).
"""
from __future__ import annotations
import math
from dataclasses import dataclass
import numpy as np
import pandas as pd
from .engine import rebal_flags, ANN
from .sweep import deflated_sharpe
from .catalog import Panel, evaluate, build_rebal, REGISTRY

SPY, CASH, DEF = "SPY", "BIL", "IEF"

# ------------------------------------------------------------------ rebalance calendars
def quarterly_flags(index: pd.DatetimeIndex) -> np.ndarray:
    """Last trading day of Feb/May/Aug/Nov."""
    m = rebal_flags(index, "monthly")
    return m & np.isin(index.month, (2, 5, 8, 11))


def flags_for(index, kind):
    return quarterly_flags(index) if kind == "quarterly" else rebal_flags(index, kind)

# ------------------------------------------------------------------ score functions (no look-ahead)
def _rank(df):  # cross-sectional pct rank within the row (same-date info only)
    return df.rank(axis=1, pct=True)

def mom_12_1(c): return c.shift(21) / c.shift(252) - 1
def mom_12_0(c): return c / c.shift(252) - 1
def mom_skip1w(c): return c.shift(5) / c.shift(252) - 1
def mom_12_7(c): return c.shift(147) / c.shift(252) - 1
def mom_6_1(c): return c.shift(21) / c.shift(126) - 1
def mom_6(c): return c / c.shift(126) - 1
def accel(c): return (c / c.shift(126) - 1) - (c.shift(126) / c.shift(252) - 1)

def rankavg(*dfs):
    ok = dfs[0].notna()
    for d in dfs[1:]:
        ok = ok & d.notna()
    rs = [_rank(d.where(ok)) for d in dfs]
    return sum(rs) / len(rs)

def mom_12_7_6_1(c): return rankavg(mom_12_7(c), mom_6_1(c))
def accel_12_1(c): return rankavg(accel(c), mom_12_1(c))
def vol252(c): return c.pct_change(fill_method=None).rolling(252, min_periods=252).std()
def mom_lowvol(c): return rankavg(mom_12_1(c), -vol252(c))

def fip_id(c):
    """Da-Gurun-Warachka information discreteness over the 12-1 window: sign(PRET)*(%neg - %pos). Low = smooth."""
    r = c.pct_change(fill_method=None).shift(21)
    pos = (r > 0).astype(float).where(r.notna()); neg = (r < 0).astype(float).where(r.notna())
    pp = pos.rolling(231, min_periods=231).mean(); pn = neg.rolling(231, min_periods=231).mean()
    return np.sign(mom_12_1(c)) * (pn - pp)

def seasonal(c, years=10, min_years=5):
    """Heston-Sadka: mean return of the upcoming calendar month over the prior <=10 years (>=5 required).
    Defined only on last-trading-day-of-month rows (NaN elsewhere)."""
    fl = rebal_flags(c.index, "monthly")
    rows = np.where(fl)[0]
    mc = c.iloc[rows]
    mret = mc.pct_change(fill_method=None)
    yr = mc.index.year.values; mo = mc.index.month.values
    out = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
    for i, t in enumerate(rows):
        tm = mo[i] % 12 + 1; ty = yr[i] + (1 if mo[i] == 12 else 0)
        sel = (mo == tm) & (yr >= ty - years) & (yr <= ty - 1)
        sub = mret.iloc[np.where(sel)[0]]
        out.iloc[t] = sub.mean().where(sub.count() >= min_years)
    return out

def clenow_score(c, win=90):
    """Annualised exp-regression slope over `win` days * R^2 (rows <= t only)."""
    y = np.log(c.where(c > 0))
    x = np.arange(win, dtype=float); xm = x.mean(); xv = ((x - xm) ** 2).mean()
    ym = y.rolling(win, min_periods=win).mean(); yv = y.rolling(win, min_periods=win).var(ddof=0)
    w = (x - xm)[::-1]
    yy = y.fillna(0.0).values
    cov = np.full(yy.shape, np.nan)
    for j in range(yy.shape[1]):
        cov[:, j] = np.convolve(yy[:, j], w, mode="full")[: yy.shape[0]] / win
    cov = pd.DataFrame(cov, index=y.index, columns=y.columns).where(ym.notna())
    slope = cov / xv
    r2 = (cov ** 2) / (xv * yv.where(yv > 1e-12))
    return (np.exp(slope * ANN) - 1) * r2.clip(0, 1)

SCORES = {"mom121": mom_12_1, "mom120": mom_12_0, "mom_skip1w": mom_skip1w, "mom127": mom_12_7,
          "mom127_61": mom_12_7_6_1, "accel": accel, "accel_121": accel_12_1, "mom6": mom_6,
          "mom_lowvol": mom_lowvol, "fip_id": fip_id, "seasonal": seasonal, "clenow": clenow_score}

# ------------------------------------------------------------------ selection helpers
def top_idx(s: np.ndarray, k: int) -> np.ndarray:
    fin = np.isfinite(s)
    k = min(k, int(fin.sum()))
    if k <= 0:
        return np.array([], int)
    ss = np.where(fin, s, -np.inf)
    ix = np.argpartition(-ss, k - 1)[:k]
    return ix[fin[ix]]


def fip_select(mom_row, id_row, n):
    """Top 2n by momentum, then the n with the lowest ID."""
    top = top_idx(mom_row, 2 * n)
    top = top[np.isfinite(id_row[top])]
    return top[np.argsort(id_row[top], kind="stable")[:n]]


def qm_select(mom_row, id_row, dec_frac=0.10, keep_frac=0.5):
    fin = np.isfinite(mom_row) & np.isfinite(id_row)
    nfin = int(fin.sum())
    k = max(2, int(round(nfin * dec_frac)))
    top = top_idx(np.where(fin, mom_row, np.nan), k)
    keep = max(1, int(round(len(top) * keep_frac)))
    return top[np.argsort(id_row[top], kind="stable")[:keep]]

# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class Cfg:
    name: str
    variant: str          # plain|volscale|dmom|breadth|fip|qm|clenow
    universe: str
    N: int
    score: str = "mom121"
    rebal: str = "monthly"
    cap: float = 1.0
    start: str = "2011-01-03"
    note: str = ""


def sma(c, n): return c.rolling(n, min_periods=n).mean()


class Ctx:
    def __init__(self, P: Panel, cols):
        self.P, self.cols = P, list(cols)
        self.c = P.c[self.cols]
        self.cache = {}

    def score(self, name):
        if name not in self.cache:
            self.cache[name] = SCORES[name](self.c).values
        return self.cache[name]


def bear_state(spy: pd.Series) -> np.ndarray:
    """Daniel-Moskowitz long-only adaptation: SPY 24m return < 0 AND 126d vol above trailing-5y median."""
    r24 = spy / spy.shift(504) - 1
    vol = spy.pct_change(fill_method=None).rolling(126, min_periods=126).std()
    med = vol.rolling(1260, min_periods=252).median()
    return ((r24 < 0) & (vol > med)).values


def breadth_low(c: pd.DataFrame, thr=0.4) -> np.ndarray:
    s = sma(c, 200)
    ab = (c > s); ok = s.notna()
    frac = ab.where(ok).sum(axis=1) / ok.sum(axis=1).replace(0, np.nan)
    return (frac < thr).values


def make_rowfn(cfg: Cfg, ctx: Ctx, P: Panel, r_unscaled: pd.Series | None = None):
    cols, N = ctx.cols, cfg.N
    quarterly = cfg.rebal == "quarterly"
    qf = quarterly_flags(P.idx) if quarterly else None
    gate = None
    if cfg.variant == "dmom":
        gate = bear_state(P.c[SPY])
    elif cfg.variant == "breadth":
        gate = breadth_low(ctx.c)
    if cfg.variant == "clenow":
        return _clenow_rowfn(cfg, ctx, P)

    def row(t):
        if quarterly and not qf[t]:
            return None
        if gate is not None and gate[t]:
            return {DEF: 1.0}
        if cfg.variant == "fip":
            ix = fip_select(ctx.score("mom121")[t], ctx.score("fip_id")[t], N)
        elif cfg.variant == "qm":
            ix = qm_select(ctx.score("mom121")[t], ctx.score("fip_id")[t])
        else:
            ix = top_idx(ctx.score(cfg.score)[t], N)
        if len(ix) == 0:
            return None
        n = len(ix) if cfg.variant == "qm" else N
        w = {cols[i]: 1.0 / n for i in ix}
        if cfg.variant == "volscale":
            if t < 127 or r_unscaled is None:
                return w
            x = r_unscaled.values[t - 126:t]          # returns known strictly before close t
            if np.isnan(x).any() or x.std() == 0:
                return w
            ex = min(cfg.cap, 0.12 / (x.std() * math.sqrt(ANN)))
            return {k: v * ex for k, v in w.items()}
        return w
    return row


def _clenow_rowfn(cfg, ctx, P, cap_mult=2.0):
    cols, N = ctx.cols, cfg.N
    c = ctx.c
    sc = ctx.score("clenow")
    cv = c.values
    ma100 = sma(c, 100).values
    gap_ok = (c.pct_change(fill_method=None).abs().rolling(90, min_periods=60).max() <= 0.15).values
    atr = c.diff().abs().rolling(20, min_periods=20).mean().values   # close-to-close ATR proxy (no H/L loaded)
    spy = P.c[SPY]
    spy_ok = (spy > sma(spy, 200)).values
    state = {"held": []}

    def row(t):
        ok = np.isfinite(sc[t]) & (sc[t] > 0) & (cv[t] > ma100[t]) & gap_ok[t] & np.isfinite(atr[t]) & (atr[t] > 0)
        nvalid = int(np.isfinite(sc[t]).sum())
        order = np.argsort(-np.where(ok, sc[t], -np.inf), kind="stable")
        top_cut = max(1, int(0.2 * max(nvalid, 1)))
        rank = np.empty(len(order), int); rank[order] = np.arange(len(order))
        keep = [i for i in state["held"] if ok[i] and rank[i] < top_cut]
        if spy_ok[t]:
            for i in order:
                if len(keep) >= N or not ok[i]:
                    break
                if int(i) not in keep:
                    keep.append(int(i))
        state["held"] = keep
        if not keep:
            return {}
        raw = np.array([cv[t, i] / atr[t, i] for i in keep])
        w = np.minimum(raw / raw.sum(), cap_mult / N)   # cap single name at 2/N (rest cash)
        return {cols[i]: float(x) for i, x in zip(keep, w)}
    return row


def run_cfg(cfg: Cfg, ctx: Ctx, P: Panel, r_unscaled=None):
    W, flag = build_rebal(P, make_rowfn(cfg, ctx, P, r_unscaled), "weekly" if cfg.rebal == "weekly" else "monthly")
    return evaluate(P, W, flag)


def run_benchmark(P: Panel, kind: str, cols):
    def row(t):
        if kind == "spy":
            return {SPY: 1.0}
        av = [s for s in cols if np.isfinite(P.c[s].values[t])]
        return {s: 1.0 / len(av) for s in av} if av else None
    W, flag = build_rebal(P, row, "monthly")
    return evaluate(P, W, flag)

# ------------------------------------------------------------------ stats
def n_registry_rows(path=REGISTRY) -> int:
    try:
        return max(sum(1 for _ in open(path, encoding="utf-8")) - 1, 0)
    except FileNotFoundError:
        return 0


def dsr(r: pd.Series, n_registry: int, n_mine: int, sr_var: float) -> float:
    """Deflated Sharpe with the COMBINED trial count (existing registry rows + this lab's trials)."""
    return deflated_sharpe(r, n_registry + n_mine, sr_var)


def block_boot_dsharpe(a: pd.Series, b: pd.Series, n=2000, block=21, seed=11):
    """Paired circular block bootstrap of Sharpe(a)-Sharpe(b) (annualised). Returns (point, lo, hi)."""
    d = pd.concat([a, b], axis=1).dropna().values
    T = len(d)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(T / block))
    starts = rng.integers(0, T, size=(n, nb))
    ix = ((starts[:, :, None] + np.arange(block)[None, None, :]) % T).reshape(n, -1)[:, :T]
    A, B = d[:, 0][ix], d[:, 1][ix]

    def sr(x):
        sd = x.std(1)
        return x.mean(1) / np.where(sd > 0, sd, np.nan) * math.sqrt(ANN)
    diff = sr(A) - sr(B)
    pt = (d[:, 0].mean() / d[:, 0].std() - d[:, 1].mean() / d[:, 1].std()) * math.sqrt(ANN)
    return float(pt), float(np.nanpercentile(diff, 2.5)), float(np.nanpercentile(diff, 97.5))


def extra_metrics(r: pd.Series) -> dict:
    mo = (1 + r).groupby([r.index.year, r.index.month]).prod() - 1
    yr = (1 + r).groupby(r.index.year).prod() - 1
    out = {"worst_month": float(mo.min())}
    out["ret_2020_03"] = float(mo.get((2020, 3), float("nan")))
    out["ret_2022"] = float(yr.get(2022, float("nan")))
    for y, v in yr.items():
        out[f"y{y}"] = float(v)
    return out
