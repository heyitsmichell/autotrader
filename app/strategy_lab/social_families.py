"""Swing-style social rules on a stock universe (Connors RSI(2) basket, IBS basket), executed with the rotation
engine (signal at close t -> fill open t+1, 5 bps + SEC + TAF). Research data only (Data caps at 2025-11-23).

These are the basket forms of the posted single-index rules: up to `slots` positions of 1/slots each, entries ranked by
the most oversold reading, exits on the posted rule. Survivorship-biased universe (today's S&P 500). The existing
strategy_lab 'connors' family (per-name harness) is not touched.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.rotation_lab.engine import run
from app.rotation_lab.sweep import Data


def _rsi_df(c: pd.DataFrame, n: int) -> pd.DataFrame:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _hilo(d: Data, syms: list[str]):
    """High/low panels aligned to d.index (read from the raw ext CSVs)."""
    from app.rotation_lab.sweep import EXT, read_csv
    hi, lo = {}, {}
    for s in syms:
        x = read_csv(EXT / f"{s}.csv")
        hi[s] = x["high"].reindex(d.index)
        lo[s] = x["low"].reindex(d.index)
    return pd.DataFrame(hi), pd.DataFrame(lo)


def basket_weights(score: np.ndarray, entry_ok: np.ndarray, exit_ok: np.ndarray, slots: int, max_hold: int | None):
    """score: T x M (lower = more oversold, NaN = ineligible). Returns W (T x M+1, last col cash) and trade flag."""
    T, M = score.shape
    W = np.zeros((T, M + 1))
    flag = np.zeros(T, bool)
    held = np.zeros(M, bool)
    age = np.zeros(M, int)
    prev = np.zeros(M, bool)
    for t in range(T):
        age[held] += 1
        drop = held & (exit_ok[t] | ((age >= max_hold) if max_hold else False))
        held &= ~drop
        free = slots - int(held.sum())
        if free > 0:
            cand = np.where(entry_ok[t] & ~held & np.isfinite(score[t]))[0]
            if len(cand):
                pick = cand[np.argsort(score[t, cand])[:free]]
                held[pick] = True
                age[pick] = 0
        W[t, :M] = held / slots
        W[t, M] = 1.0 - W[t, :M].sum()
        flag[t] = bool((held != prev).any())
        prev = held.copy()
    return W, flag


def run_basket(d: Data, universe: str, kind: str, th: float, slots: int = 10, max_hold: int | None = 10, ro: str = "BIL"):
    """kind: 'rsi2' (buy RSI(2)<th above SMA200, exit close>SMA5) or 'ibs' (buy IBS<th above SMA200, exit IBS>0.8)."""
    ix, O, C = d.sub(universe, ro)
    syms = d.univ[universe]
    close = d.close[syms]
    sma200 = close.rolling(200, min_periods=200).mean()
    trend = (close > sma200).values
    if kind == "rsi2":
        sc = _rsi_df(close, 2)
        score = sc.values
        entry = (sc < th).values & trend
        exit_ = (close > close.rolling(5).mean()).fillna(False).values
    elif kind == "ibs":
        hi, lo = _hilo(d, syms)
        x = ((close - lo) / (hi - lo).replace(0, np.nan))
        score = x.values
        entry = (x < th).values & trend
        exit_ = (x > 0.8).fillna(False).values
    else:
        raise ValueError(kind)
    W, flag = basket_weights(score, entry, exit_, slots, max_hold)
    r, turn = run(O, C, W, flag)
    return pd.Series(r, index=d.index), pd.Series(turn, index=d.index)
