"""Pure target-weight functions for the shadow strategies (no I/O)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..sleeves.strategies import vaa
from .registry import VAA_G4_DEFENSE, VAA_G4_OFFENSE


def _top(score: pd.Series, n: int) -> dict[str, float]:
    score = score.replace([np.inf, -np.inf], np.nan).dropna()
    if n <= 0 or len(score) < n:
        return {}
    top = score.sort_values(ascending=False, kind="stable").index[:n]
    return {str(s): 1.0 / n for s in top}


def mom_skip1w_targets(closes: pd.DataFrame, n: int = 10) -> dict[str, float]:
    """score = close[t-5] / close[t-252] - 1 (t = last row)."""
    if closes is None or len(closes) < 253:
        return {}
    score = closes.iloc[-1 - 5] / closes.iloc[-1 - 252] - 1
    return _top(score, n)


def overnight_scores(opens: pd.DataFrame, closes: pd.DataFrame, window: int = 252,
                     asof: int | None = None) -> pd.Series:
    """Sum of log(open[t] / close[t-1]) over the last ``window`` rows up to and including row ``asof``
    (default the last row). Rows after ``asof`` are never read."""
    if opens is None or closes is None or opens.empty or closes.empty:
        return pd.Series(dtype=float)
    end = len(closes) if asof is None else asof + 1
    cols = [x for x in closes.columns if x in opens.columns]
    o, c = opens[cols].iloc[:end], closes[cols].iloc[:end]
    ratio = o / c.shift(1)
    lr = np.log(ratio.where(ratio > 0)).tail(window)
    if len(lr) < window:
        return pd.Series(dtype=float)
    return lr.sum(min_count=window)


def overnight_mom_targets(opens: pd.DataFrame, closes: pd.DataFrame, n: int = 10,
                          asof: int | None = None) -> dict[str, float]:
    return _top(overnight_scores(opens, closes, asof=asof), n)


def vaa_g4_targets(closes: pd.DataFrame) -> dict[str, float]:
    """VAA, B=1, top 1: cf = min(1, offensive funds with 13612W <= 0 / B) goes to the best defensive
    fund, the rest to the best offensive fund."""
    need = VAA_G4_OFFENSE + VAA_G4_DEFENSE
    if closes is None or any(s not in closes.columns for s in need):
        return {}
    mo = {s: vaa.m13612w(closes[s].dropna()) for s in need}
    if any(math.isnan(x) for x in mo.values()):
        return {}
    vo = {s: mo[s] for s in VAA_G4_OFFENSE}
    vd = {s: mo[s] for s in VAA_G4_DEFENSE}
    cf = min(1.0, sum(1 for x in vo.values() if x <= 0) / 1)
    w = {max(vd, key=vd.get): cf}
    best = max(vo, key=vo.get)
    w[best] = w.get(best, 0) + (1 - cf)
    return {k: v for k, v in w.items() if v > 0}


AMIHUD_WINDOW = 90
AMIHUD_TOP_N = 20
AMIHUD_QUALITY_PCT = 0.5
AMIHUD_REBALANCE_DAYS = 21


def amihud_quality_targets(closes: pd.DataFrame, volumes: pd.DataFrame, n: int = AMIHUD_TOP_N,
                           window: int = AMIHUD_WINDOW, q: float = AMIHUD_QUALITY_PCT) -> dict[str, float]:
    """Amihud = mean(|ret| / (close*volume)) over ``window`` rows (last row = t); eligible = 12m-return
    cross-sectional percentile >= q; top ``n`` by Amihud, equal weight. Uses rows up to the last only."""
    if closes is None or volumes is None or len(closes) < 253:
        return {}
    cols = [c for c in closes.columns if c in volumes.columns]
    c, v = closes[cols], volumes[cols].reindex(closes.index)
    dv = (c * v).replace(0, np.nan)
    illiq = (c.pct_change(fill_method=None).abs() / dv).iloc[-window:]
    if len(illiq) < window:
        return {}
    score = illiq.mean(skipna=False)
    ret252 = c.iloc[-1] / c.iloc[-1 - 252] - 1
    elig = ret252.rank(pct=True) >= q
    return _top(score[elig.reindex(score.index).fillna(False)], n)


def compute(shadow_id: str, ohlc: dict, n: int = 10) -> dict[str, float]:
    if shadow_id == "mom_skip1w":
        return mom_skip1w_targets(ohlc.get("close"), n)
    if shadow_id == "overnight_mom":
        return overnight_mom_targets(ohlc.get("open"), ohlc.get("close"), n)
    if shadow_id == "vaa_g4":
        return vaa_g4_targets(ohlc.get("close"))
    if shadow_id == "amihud_quality":
        return amihud_quality_targets(ohlc.get("close"), ohlc.get("volume"), n)
    return {}
