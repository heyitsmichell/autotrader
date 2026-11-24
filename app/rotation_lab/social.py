"""Social-media strategy lab: panel loader, synthetic leveraged ETFs, rule builders, metrics, registry.

Timing: every rule builds target weights W[t] from data <= close[t]; app.rotation_lab.engine.run trades them at
open[t+1] with 5 bps + SEC fee + TAF. Research data only (<= 2025-11-23).
Synthetic leveraged series: daily L x of the underlying minus (L-1)*cash - expense; spliced to the REAL ETF
from its first listed day (real returns after, synthetic before). Pre-inception numbers are SYNTHETIC.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import ANN, run
from .sweep import deflated_sharpe  # noqa: F401  (re-exported for the runner)

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "data" / "bars_yahoo_ext"
SOC = EXT / "social"
LONG = EXT / "long"
END = pd.Timestamp("2025-11-23")
START = pd.Timestamp("2005-01-03")
REGISTRY = ROOT / "data" / "experiments" / "trials_registry.csv"
PRIOR_ROTATION_TRIALS = 304
SR_VAR = 1.728e-04  # daily-Sharpe variance across the prior rotation sweep (rotation REPORT)
BLOCKS3 = [(2006, 2008), (2009, 2011), (2012, 2014), (2015, 2017), (2018, 2020), (2021, 2023), (2024, 2025)]


# ------------------------------------------------------------------ data
def read_bars(sym: str) -> pd.DataFrame | None:
    for d in (SOC, LONG, EXT):
        p = d / f"{sym}.csv"
        if p.exists():
            x = pd.read_csv(p, parse_dates=["date"]).set_index("date")
            x = x[~x.index.duplicated()].sort_index()
            return x[x.index <= END]
    return None


def lev_returns(u_ret: pd.Series, cash_ret: pd.Series, lev: float, exp_yr: float = 0.01) -> pd.Series:
    """Daily-rebalanced leveraged return: lev*r_u - (lev-1)*r_cash - expense/252."""
    return lev * u_ret - (lev - 1.0) * cash_ret.reindex(u_ret.index).fillna(0.0) - exp_yr / ANN


def synth_lev_bars(u: pd.DataFrame, cash_ret: pd.Series, lev: float, exp_yr: float = 0.01) -> pd.DataFrame:
    """Synthetic open/close leveraged series from underlying bars. Overnight gap leg scaled by lev."""
    cc = lev_returns(u["close"].pct_change(), cash_ret, lev, exp_yr).fillna(0.0)
    close = 100.0 * (1 + cc).cumprod()
    gap = u["open"] / u["close"].shift(1) - 1.0
    open_ = close.shift(1) * (1 + lev * gap.fillna(0.0))
    open_.iloc[0] = close.iloc[0]
    return pd.DataFrame({"open": open_, "close": close, "high": np.maximum(open_, close), "low": np.minimum(open_, close)})


def splice(syn: pd.DataFrame, real: pd.DataFrame | None) -> pd.DataFrame:
    """Synthetic until real inception, real after (level-matched at the splice)."""
    if real is None or real.empty:
        return syn
    t0 = real.index[0]
    pre = syn[syn.index < t0]
    if pre.empty:
        return real[["open", "high", "low", "close"]]
    k = real["close"].iloc[0] / syn["close"].loc[t0] if t0 in syn.index else 1.0
    return pd.concat([pre * k, real[["open", "high", "low", "close"]]])


class Panel:
    """Aligned open/close/high/low frames on the SPY calendar, plus CASH (BIL; SHY proxy before 2007-05)."""

    def __init__(self, end: pd.Timestamp = END):
        spy = read_bars("SPY")
        self.index = spy.index[(spy.index >= START) & (spy.index <= end)]
        self.bars: dict[str, pd.DataFrame] = {}
        self.synthetic_until: dict[str, pd.Timestamp] = {}
        bil, shy = read_bars("BIL"), read_bars("SHY")
        c = bil["close"].pct_change()
        if shy is not None:
            c = c.combine_first(shy["close"].pct_change())
        self.cash_ret = c.reindex(self.index).fillna(0.0)
        cl = (1 + self.cash_ret).cumprod() * 100
        self.bars["CASH"] = pd.DataFrame({"open": cl, "close": cl, "high": cl, "low": cl})

    def add(self, sym: str):
        if sym in self.bars:
            return
        b = read_bars(sym)
        if b is None:
            raise KeyError(sym)
        self.bars[sym] = b.reindex(self.index)

    def add_lev(self, name: str, under: str, lev: float, exp_yr: float = 0.01, real: str | None = None):
        if name in self.bars:
            return
        self.add(under)
        syn = synth_lev_bars(self.bars[under].ffill(), self.cash_ret, lev, exp_yr)
        rl = read_bars(real or name)
        self.bars[name] = splice(syn, rl).reindex(self.index)
        self.synthetic_until[name] = rl.index[0] if rl is not None else self.index[-1]

    def close(self, s):
        return self.bars[s]["close"]

    def arrays(self, cols: list[str]):
        O = np.column_stack([self.bars[c]["open"].ffill().bfill().values for c in cols])
        C = np.column_stack([self.bars[c]["close"].ffill().bfill().values for c in cols])
        return O, C


# ------------------------------------------------------------------ indicators / state machines
def rsi_wilder(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def ibs(df: pd.DataFrame) -> pd.Series:
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return (df["close"] - df["low"]) / rng


def toggle(entry: np.ndarray, exit_: np.ndarray, max_hold: int | None = None) -> np.ndarray:
    """Position state after close t. Enter on `entry` when flat; leave on `exit_` (or after max_hold bars)."""
    T = len(entry)
    pos = np.zeros(T, bool)
    on, held = False, 0
    for t in range(T):
        if on:
            held += 1
            if exit_[t] or (max_hold is not None and held >= max_hold):
                on = False
        if not on and entry[t]:
            on, held = True, 0
        pos[t] = on
    return pos


def hysteresis(sig: pd.Series, sma: pd.Series, up: float, dn: float) -> np.ndarray:
    """Long when sig > sma*(1+up); stays long until sig < sma*(1-dn). Undefined SMA -> flat."""
    ent = (sig > sma * (1 + up)).fillna(False).values
    ex = (sig < sma * (1 - dn)).fillna(True).values
    return toggle(ent, ex)


def weights_from(panel: Panel, cols: list[str], parts: dict) -> pd.DataFrame:
    W = pd.DataFrame(0.0, index=panel.index, columns=cols)
    for a, w in parts.items():
        W[a] = np.asarray(w, float) if not np.isscalar(w) else float(w)
    W["CASH"] = 1.0 - W.drop(columns="CASH").sum(axis=1)
    return W


def rebal_dates(index: pd.DatetimeIndex, kind: str) -> np.ndarray:
    """True on the last trading day of the period (signal day; trade next open)."""
    if kind == "quarterly":
        key = index.year * 10 + (index.month - 1) // 3
    elif kind == "monthly":
        key = index.year * 100 + index.month
    elif kind == "annual":
        key = index.year
    else:
        raise ValueError(kind)
    key = np.asarray(key)
    f = np.zeros(len(index), bool)
    f[:-1] = key[:-1] != key[1:]
    return f


def simulate(panel: Panel, W: pd.DataFrame, flag: np.ndarray | None = None):
    """Execute weights via the rotation engine. flag=None -> trade whenever the target row changes."""
    cols = list(W.columns)
    assert cols[-1] == "CASH"
    O, C = panel.arrays(cols)
    Wv = W.values.astype(float)
    if flag is None:
        flag = np.zeros(len(Wv), bool)
        flag[1:] = np.abs(np.diff(Wv, axis=0)).sum(axis=1) > 1e-12
        flag[0] = Wv[0, -1] < 1 - 1e-12
    r, turn = run(O, C, Wv, flag)
    return pd.Series(r, index=panel.index), pd.Series(turn, index=panel.index)


# ------------------------------------------------------------------ metrics
def metrics(r: pd.Series, spy: pd.Series, turn: pd.Series | None = None, start=None, end=None) -> dict:
    r = r[(r.index >= (start or r.index[0])) & (r.index <= (end or r.index[-1]))]
    s = spy.reindex(r.index).fillna(0.0)
    yrs = len(r) / ANN
    eq = (1 + r).cumprod()
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    sd = r.std()
    sh = r.mean() / sd * math.sqrt(ANN) if sd > 0 else float("nan")
    dd = math.sqrt((np.minimum(r, 0) ** 2).mean()) * math.sqrt(ANN)
    mdd = float((eq / eq.cummax() - 1).min())
    ry = (1 + r).groupby(r.index.year).prod() - 1
    sy = (1 + s).groupby(s.index.year).prod() - 1
    blocks = []
    for a, b in BLOCKS3:
        m = (r.index.year >= a) & (r.index.year <= b)
        x = r[m]
        blocks.append(x.mean() / x.std() * math.sqrt(ANN) if m.sum() > 100 and x.std() > 0 else float("nan"))

    def sub_dd(y):
        x = r[r.index.year == y]
        if len(x) < 100:
            return float("nan"), float("nan")
        e = (1 + x).cumprod()
        return float(e.iloc[-1] - 1), float((e / e.cummax() - 1).min())

    o = dict(cagr=float(cagr), vol=float(sd * math.sqrt(ANN)), sharpe=float(sh),
             sortino=float(r.mean() * ANN / dd) if dd > 0 else float("nan"), maxdd=mdd,
             mar=float(cagr / abs(mdd)) if mdd else float("nan"), worst_year=float(ry.min()),
             pct_years_beat_spy=float((ry > sy.reindex(ry.index)).mean()), blocks=";".join(f"{v:.2f}" for v in blocks),
             n_blocks_pos=int(sum(v > 0 for v in blocks if v == v)), n_blocks=int(sum(v == v for v in blocks)),
             turnover_yr=float(turn.reindex(r.index).sum() / yrs) if turn is not None else float("nan"),
             start=str(r.index[0].date()), end=str(r.index[-1].date()))
    for y in (2008, 2020, 2022):
        o[f"ret{y}"], o[f"dd{y}"] = sub_dd(y)
    return o


def config_hash(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def registry_rows() -> int:
    if not REGISTRY.exists():
        return 0
    with open(REGISTRY, encoding="utf8") as f:
        return max(sum(1 for _ in f) - 1, 0)


def append_registry(rows: list[dict]):
    import csv
    cols = "ts,lab,strategy,config_hash,universe,start,end,cagr,sharpe,maxdd,turnover,notes".split(",")
    with open(REGISTRY, "a", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------ strategies (each returns W [, flag]; CASH last)
def w_static(P: Panel, alloc: dict, rebal: str):
    cols = list(alloc) + ["CASH"]
    W = weights_from(P, cols, dict(alloc))
    return W, rebal_dates(P.index, rebal) | (np.arange(len(P.index)) == 0)


def w_trend(P: Panel, sig: str, on: str, off: str, n: int, up: float = 0.0, dn: float = 0.0):
    """Hold `on` while sig is above its n-day SMA (hysteresis up/dn), else `off` (CASH or an ETF)."""
    c = P.close(sig).ffill()
    pos = hysteresis(c, c.rolling(n, min_periods=n).mean(), up, dn)
    cols = [on] + ([off] if off != "CASH" and off != on else []) + ["CASH"]
    parts = {on: pos.astype(float)}
    if off != "CASH":
        parts[off] = (~pos).astype(float)
    return weights_from(P, cols, parts)


def w_ftlt(P: Panel, hot: float, cold: float, defense: str, rsi_n: int = 10):
    """Composer-style 'TQQQ for the long term' (APPROXIMATION: public clones differ; exact thresholds unpublished).
    SPY > SMA200: TQQQ, parked in BIL when QQQ RSI(n) > hot. SPY <= SMA200: TQQQ only if TQQQ RSI(n) < cold,
    else `defense` (CASH or TLT)."""
    spy = P.close("SPY").ffill()
    up = (spy > spy.rolling(200, min_periods=200).mean()).fillna(False).values
    rq = rsi_wilder(P.close("QQQ").ffill(), rsi_n).values
    rt = rsi_wilder(P.close("TQQQ").ffill(), rsi_n).values
    long_ = np.where(up, ~(rq > hot), rt < cold) & ~np.isnan(rq)
    parts = {"TQQQ": long_.astype(float)}
    cols = ["TQQQ", "CASH"]
    if defense != "CASH":
        cols = ["TQQQ", defense, "CASH"]
        parts[defense] = ((~long_) & ~up).astype(float)
    return weights_from(P, cols, parts)


def w_9sig(P: Panel, target: float, init_w: float, bond: str = "IEF", throttle: float = 0.9):
    """Kelly '9Sig' (public approximation; spike-reset and 30-Down rules are paywalled, NOT implemented).
    Quarter-end: signal line L *= (1+target); TQQQ above L -> surplus into bond; below -> buy shortfall from bond,
    spending at most `throttle` of the bond balance."""
    t = P.close("TQQQ").ffill().pct_change().fillna(0).values
    b = P.close(bond).ffill().pct_change().fillna(0).values
    qe = rebal_dates(P.index, "quarterly")
    T = len(t)
    tv, bv = init_w, 1 - init_w
    last = tv  # book 'reset' form: line = TQQQ value after last quarter's trades x (1+target)
    wt = np.zeros(T)
    for i in range(T):
        tv *= 1 + t[i]
        bv *= 1 + b[i]
        if qe[i]:
            L = last * (1 + target)
            if tv > L:
                s = tv - L
                tv -= s
                bv += s
            elif tv < L:
                buy = min(L - tv, throttle * bv)
                tv += buy
                bv -= buy
            last = tv
        wt[i] = tv / (tv + bv)
    W = weights_from(P, ["TQQQ", bond, "CASH"], {"TQQQ": wt, bond: 1 - wt})
    return W, qe | (np.arange(T) == 0)


def w_single_toggle(P: Panel, asset: str, pos: np.ndarray, hedge: str = "CASH"):
    cols = [asset, "CASH"] if hedge == "CASH" else [asset, hedge, "CASH"]
    parts = {asset: pos.astype(float)}
    if hedge != "CASH":
        parts[hedge] = (~pos).astype(float)
    return weights_from(P, cols, parts)


def w_rsi2(P: Panel, asset: str, entry_th: float, exit_kind: str, sma_filter: int | None = 200, exit_n: int = 5):
    c = P.bars[asset].ffill()["close"]
    r2 = rsi_wilder(c, 2)
    ent = (r2 < entry_th).fillna(False).values
    if sma_filter:
        ent = ent & (c > c.rolling(sma_filter, min_periods=sma_filter).mean()).fillna(False).values
    ex = (c > c.rolling(exit_n).mean()).fillna(False).values if exit_kind == "sma5" else (r2 > 70).fillna(False).values
    return w_single_toggle(P, asset, toggle(ent, ex))


def w_ibs(P: Panel, asset: str, lo: float, hi: float, sma_filter: int | None = None):
    b = P.bars[asset].ffill()
    x = ibs(b)
    ent = (x < lo).fillna(False).values
    if sma_filter:
        ent = ent & (b["close"] > b["close"].rolling(sma_filter, min_periods=sma_filter).mean()).fillna(False).values
    return w_single_toggle(P, asset, toggle(ent, (x > hi).fillna(False).values))


def w_down_days(P: Panel, asset: str, k: int, hold: int = 1, sma_filter: int | None = None):
    c = P.close(asset).ffill()
    ent = (c.diff() < 0).rolling(k).sum().eq(k).fillna(False).values
    if sma_filter:
        ent = ent & (c > c.rolling(sma_filter, min_periods=sma_filter).mean()).fillna(False).values
    return w_single_toggle(P, asset, toggle(ent, np.zeros(len(c), bool), max_hold=hold))


def tom_window(index: pd.DatetimeIndex, last_n: int, first_n: int) -> np.ndarray:
    """True for days whose return is earned inside the turn-of-month window (last_n last + first_n first days)."""
    ym = index.year * 100 + index.month
    s = pd.Series(np.arange(len(index)), index=index)
    fwd = s.groupby(ym).cumcount().values + 1
    cnt = s.groupby(ym).transform("count").values
    return ((cnt - fwd) < last_n) | (fwd <= first_n)


def w_tom(P: Panel, asset: str, last_n: int, first_n: int, sma_filter: int | None = None):
    """W[t] earns day t+1's return, so the in-window flag is shifted by -1 (calendar rank is public)."""
    inwin = tom_window(P.index, last_n, first_n)
    pos = np.r_[inwin[1:], False]
    if sma_filter:
        c = P.close(asset).ffill()
        pos = pos & (c > c.rolling(sma_filter, min_periods=sma_filter).mean()).fillna(False).values
    return w_single_toggle(P, asset, pos)


def w_vix_ratio(P: Panel, asset: str, th: float, off: str = "CASH", kind: str = "vix3m"):
    """Hold asset while VIX/VIX3M < th (contango), else `off`. kind='vix9d': VIX9D/VIX < th."""
    v = read_bars("VIX")["close"].reindex(P.index).ffill()
    if kind == "vix3m":
        ratio = v / read_bars("VIX3M")["close"].reindex(P.index).ffill()
    else:
        ratio = read_bars("VIX9D")["close"].reindex(P.index).ffill() / v
    return w_single_toggle(P, asset, (ratio < th).fillna(False).values, off)


def w_vix_lev(P: Panel, lo: float = 15.0, hi: float = 30.0, lmax: float = 1.5):
    """'MAX effect with VIX-based leverage scaling' (QuantConnect research): leverage lmax at VIX<=lo -> 1x at VIX>=hi.
    SPY + SSO (2x): w_SSO = L-1, rounded to 0.1 to limit churn."""
    v = read_bars("VIX")["close"].reindex(P.index).ffill()
    L = (lmax - (lmax - 1) * ((v - lo) / (hi - lo)).clip(0, 1)).fillna(1.0)
    wsso = (L - 1).round(1).values
    return weights_from(P, ["SPY", "SSO", "CASH"], {"SSO": wsso, "SPY": 1 - wsso})


def w_letf_mom(P: Panel, names: list[str], look: int, rebal: str = "monthly"):
    """Top-1 of leveraged ETFs by `look`-day return if > 0 else BIL (community pattern, no single canonical post)."""
    R = pd.DataFrame({n: P.close(n).ffill().pct_change(look) for n in names}).values
    reb = rebal_dates(P.index, rebal)
    cur, sel = None, np.full(len(P.index), -1)
    for t in range(len(P.index)):
        if reb[t] or cur is None:
            row = R[t]
            cur = int(np.nanargmax(row)) if np.isfinite(row).any() and np.nanmax(row) > 0 else -1
        sel[t] = cur
    return weights_from(P, names + ["CASH"], {n: (sel == i).astype(float) for i, n in enumerate(names)})


def w_sector_top(P: Panel, secs: list[str], n: int, look: int, skip: int, filt: bool):
    """Top-n sector SPDRs by return over `look` days (skipping `skip`), equal weight, monthly; SPY<SMA200 -> BIL."""
    C = pd.DataFrame({s: P.close(s).ffill() for s in secs})
    sc = (C.shift(skip) / C.shift(look) - 1.0).values
    spy = P.close("SPY").ffill()
    ok = (spy > spy.rolling(200, min_periods=200).mean()).fillna(False).values if filt else np.ones(len(spy), bool)
    reb = rebal_dates(P.index, "monthly")
    W = np.zeros((len(C), len(secs)))
    cur = np.zeros(len(secs))
    for t in range(len(C)):
        if reb[t]:
            row = sc[t]
            cur = np.zeros(len(secs))
            if np.isfinite(row).sum() >= n:
                cur[np.argsort(-np.where(np.isfinite(row), row, -9))[:n]] = 1.0 / n
        W[t] = cur * ok[t]
    return weights_from(P, secs + ["CASH"], {s: W[:, i] for i, s in enumerate(secs)})
