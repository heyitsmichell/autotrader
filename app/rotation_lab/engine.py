"""Vectorised-signal / loop-execution daily rotation backtester.

Timing: signals use data <= close[t]; the resulting target weights W[t] are traded at open[t+1];
day k's return runs open[k] -> open[k+1] (last day: open -> close). No look-ahead.
Columns: M symbols + one trailing risk-off column (own price series).
"""
from __future__ import annotations
from dataclasses import dataclass, replace
import numpy as np
import pandas as pd

SLIP = 0.0005          # 5 bps per side
SEC_FEE = 0.0000278    # on sells
TAF_PER_SHARE = 0.000166
ANN = 252


@dataclass(frozen=True)
class Spec:
    name: str = ""
    universe: str = "sector"
    score: str = "mom121"
    topn: int = 5
    rebal: str = "monthly"        # weekly | monthly | daily
    regime: str = "none"          # none | 200 | 100  (SPY > SMA)
    riskoff: str = "BIL"          # BIL | SPY | TLT | IEF
    absmom: bool = False          # 63d>0 & close>SMA100 eligibility
    voltarget: float = 0.0        # 0=off else annual vol cap
    weighting: str = "equal"      # equal | invvol
    daily_exit: bool = False      # between rebalances, drop names failing absmom
    regime_flag_only: bool = False


def features(close: pd.DataFrame) -> dict[str, np.ndarray]:
    """All features at close t use only data <= t. Insufficient history -> NaN (symbol excluded)."""
    c = close
    ret1 = c.pct_change(fill_method=None)
    vol = ret1.rolling(63, min_periods=63).std() * np.sqrt(ANN)
    mom121 = c.shift(21) / c.shift(252) - 1.0
    r63, r126 = c / c.shift(63) - 1, c / c.shift(126) - 1
    yr = pd.Series(c.index.year, index=c.index)
    last_of_year = (yr != yr.shift(-1)).values
    ye = c.where(pd.Series(last_of_year, index=c.index), np.nan).ffill().shift(1)
    ytd = c / ye - 1.0
    # ytd at first rows of a year uses prev-year-end; require same-year freshness handled by ffill limit
    sma100 = c.rolling(100, min_periods=100).mean()
    absok = (r63 > 0) & (c > sma100)
    f = {"mom121": mom121, "mom_vol": mom121 / vol, "m36": (r63 + r126) / 2, "r126": r126,
         "r63": r63, "ytd": ytd, "vol": vol}
    out = {k: v.values.astype(float) for k, v in f.items()}
    out["absok"] = absok.values & ~np.isnan(r63.values) & ~np.isnan(sma100.values)
    return out


def regime_series(spy_close: pd.Series, kind: str) -> np.ndarray:
    if kind == "none":
        return np.ones(len(spy_close), bool)
    n = int(kind)
    sma = spy_close.rolling(n, min_periods=n).mean()
    return (spy_close > sma).values & sma.notna().values


def rebal_flags(index: pd.DatetimeIndex, kind: str) -> np.ndarray:
    if kind == "daily":
        return np.ones(len(index), bool)
    if kind == "weekly":
        iso = index.isocalendar()
        key = (iso["year"].astype(int) * 100 + iso["week"].astype(int)).values
    else:
        key = index.year * 100 + index.month
    key = np.asarray(key)
    f = np.zeros(len(index), bool)
    f[:-1] = key[:-1] != key[1:]
    return f


def build_targets(score: np.ndarray, elig: np.ndarray, vol: np.ndarray, regime: np.ndarray,
                  reb: np.ndarray, spec: Spec, absok: np.ndarray):
    """score/elig/vol/absok: T x M. Returns W (T x M+1, last col = risk-off), trade flag (T,)."""
    T, M = score.shape
    N = spec.topn
    W = np.zeros((T, M + 1))
    flag = np.zeros(T, bool)
    cur = np.zeros(M + 1); cur[M] = 1.0
    cursel: tuple = ()
    prev_reg = True
    for t in range(T):
        new = cur
        sel_changed = False
        if not regime[t]:
            if cur[M] < 1.0:
                new = np.zeros(M + 1); new[M] = 1.0; cursel = ()
        else:
            want = reb[t] or not prev_reg
            s = np.where(elig[t], score[t], -np.inf)
            fin = np.isfinite(s)
            sel = ()
            if want or spec.rebal == "daily":
                k = min(N, int(fin.sum()))
                if k > 0:
                    ix = np.argpartition(-s, k - 1)[:k]
                    ix = ix[fin[ix]]
                    sel = tuple(sorted(ix.tolist()))
                if want or sel != cursel:
                    new = np.zeros(M + 1)
                    if sel:
                        ia = np.array(sel)
                        if spec.weighting == "invvol":
                            v = vol[t, ia]; v = np.where(np.isfinite(v) & (v > 0), v, np.nanmedian(v) if np.isfinite(v).any() else 1.0)
                            w = (1 / v); w = w / w.sum() * (len(ia) / N)
                            w = np.minimum(w, 2.0 / N)
                        else:
                            w = np.full(len(ia), 1.0 / N)
                        new[ia] = w
                    new[M] = 1.0 - new[:M].sum()
                    cursel = sel
            elif spec.daily_exit:
                held = np.where(cur[:M] > 0)[0]
                bad = [i for i in held if not absok[t, i]]
                if bad:
                    new = cur.copy(); new[bad] = 0.0; new[M] = 1.0 - new[:M].sum()
                    cursel = tuple(i for i in cursel if i not in bad)
        prev_reg = bool(regime[t])
        if new is not cur and not np.array_equal(new, cur):
            flag[t] = True
            cur = new
        W[t] = cur
    return W, flag


def run(O: np.ndarray, C: np.ndarray, W: np.ndarray, flag: np.ndarray, costs: bool = True):
    """O,C: T x (M+1) open/close incl. risk-off col. Returns daily returns, traded notional/day."""
    T, M1 = O.shape
    w = np.zeros(M1); w[-1] = 1.0
    rets = np.zeros(T); turn = np.zeros(T)
    with np.errstate(all="ignore"):
        R = np.empty((T, M1))
        R[:-1] = O[1:] / O[:-1] - 1.0
        R[-1] = C[-1] / O[-1] - 1.0
    R = np.nan_to_num(R, nan=0.0, posinf=0.0, neginf=0.0)
    tafr = np.where(np.isfinite(O) & (O > 0), TAF_PER_SHARE / np.where(O > 0, O, 1), 0.0)
    for k in range(T):
        cost = 0.0
        if k >= 1 and flag[k - 1]:
            d = W[k - 1] - w
            buy = d[d > 0].sum(); selln = -d[d < 0]
            if costs:
                cost = SLIP * (buy + selln.sum()) + SEC_FEE * selln.sum() + (tafr[k][d < 0] * selln).sum()
            turn[k] = np.abs(d).sum()
            w = W[k - 1].copy()
        g = float(w @ R[k])
        rets[k] = (1 - cost) * (1 + g) - 1
        w = w * (1 + R[k]) / (1 + g)
    return rets, turn


def apply_voltarget(W, flag, rets_unscaled, cap, step=0.1):
    """Scale risky sleeve by min(1, cap/vol63) using returns known at close t (lagged), rest -> risk-off."""
    T = len(rets_unscaled)
    r = pd.Series(rets_unscaled)
    vol = (r.rolling(63, min_periods=20).std() * np.sqrt(ANN)).shift(1).values  # uses r[..t-1]
    expo = np.where(np.isfinite(vol) & (vol > 0), np.minimum(1.0, cap / np.where(vol > 0, vol, 1)), 1.0)
    expo = np.round(expo / step) * step
    expo = np.clip(expo, step, 1.0)
    W2 = W.copy()
    W2[:, :-1] = W[:, :-1] * expo[:, None]
    W2[:, -1] = 1.0 - W2[:, :-1].sum(axis=1)
    chg = np.zeros(T, bool); chg[1:] = expo[1:] != expo[:-1]
    return W2, flag | chg
