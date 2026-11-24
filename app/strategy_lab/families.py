"""Published-strategy entry families as precomputed, causal signal tables.

Row t of every table uses bars <= t only; the engine evaluates the signal at the prior close and fills
at the next open (no look-ahead). `exit` columns are close-based native exits: the harness queues them
as a pending exit that fills at the NEXT open.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

NEW_FAMILIES = ("xsmom_m", "xsmom_w", "tsmom", "connors", "high52", "donchian55", "dualma", "nr7", "inside")
NATIVE_EXIT_FAMILIES = {"connors", "donchian55", "dualma"}
XS_TOP_N = 10
REGIMES = ("none", "spy200", "spy50")


def _rsi_wilder(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def regime_map(bars: dict, regime: str) -> dict:
    """date -> bool (True = long entries allowed). Uses SPY close vs its SMA at date t (causal)."""
    if regime == "none":
        return {}
    n = 200 if regime == "spy200" else 50
    c = bars["SPY"].close
    ok = (c > c.rolling(n).mean()) & c.rolling(n).mean().notna()
    return {pd.Timestamp(k).date(): bool(v) for k, v in ok.items()}


def family_table(fam: str, df: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol table (index = Timestamp) with columns hit (bool), score (float), exit (bool)."""
    c, h, l = df.close, df.high, df.low
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    exit_ = pd.Series(False, index=df.index)
    if fam == "tsmom":
        r126, r252 = c / c.shift(126) - 1, c / c.shift(252) - 1
        hit, score = (c > sma200) & (r126 > 0) & (r252 > 0), r126
    elif fam == "connors":
        rsi2 = _rsi_wilder(c, 2)
        hit, score = (rsi2 < 10) & (c > sma200), 100 - rsi2
        exit_ = c > c.rolling(5).mean()
    elif fam == "high52":
        hi = h.rolling(252).max()
        hit, score = c >= 0.95 * hi, c / hi
    elif fam == "donchian55":
        hi = h.shift(1).rolling(55).max()
        hit, score = c > hi, (c - hi) / _atr(df)
        exit_ = c < l.shift(1).rolling(20).min()
    elif fam == "dualma":
        s20 = c.rolling(20).mean()
        hit, score = (s20 > sma50) & (s20.shift(1) <= sma50.shift(1)), (s20 - sma50) / sma50
        exit_ = s20 < sma50
    elif fam in ("nr7", "inside"):
        rng = h - l
        if fam == "nr7":
            setup = rng <= rng.rolling(7).min()
        else:
            setup = (h < h.shift(1)) & (l > l.shift(1))
        trend = (c > sma50) & (sma50 > sma200)
        # setup bar was yesterday; today's close breaks its high; trend measured at today's close
        hit = setup.shift(1, fill_value=False) & (c > h.shift(1)) & trend
        score = (c - h.shift(1)) / _atr(df)
    else:
        raise KeyError(fam)
    ok = hit.fillna(False) & score.notna()
    return pd.DataFrame({"hit": ok, "score": score.fillna(0.0), "exit": exit_.fillna(False)})


def xsmom_tables(bars: dict, weekly: bool, top_n: int = XS_TOP_N) -> dict:
    """12-1 month momentum (close[t-21]/close[t-252]-1), top-N across the universe on rebalance days
    (first trading day of the month/week). Signal at close t of the LAST trading day of a period is
    what the next-open fill acts on, so rebalance days = last trading day of month/week."""
    syms = [s for s in bars if s != "SPY"]
    close = pd.DataFrame({s: bars[s].close for s in syms})
    mom = close.shift(21) / close.shift(252) - 1
    idx = close.index
    key = idx.to_period("W") if weekly else idx.to_period("M")
    last = pd.Series(key, index=idx)
    is_reb = pd.Series(last.values != last.shift(-1).values, index=idx)   # last bar of its period
    is_reb.iloc[-1] = False
    rank_ok = mom.rank(axis=1, ascending=False, method="first") <= top_n
    out = {}
    for s in syms:
        hit = rank_ok[s] & is_reb & mom[s].notna()
        out[s] = pd.DataFrame({"hit": hit, "score": mom[s].fillna(0.0),
                               "exit": pd.Series(False, index=idx)})
    return out


def build_tables(fam: str, bars: dict) -> dict:
    if fam in ("xsmom_m", "xsmom_w"):
        t = xsmom_tables(bars, weekly=(fam == "xsmom_w"))
    else:
        t = {s: family_table(fam, df) for s, df in bars.items() if s != "SPY"}
    return {s: {pd.Timestamp(k).date(): (bool(r.hit), float(r.score), bool(r.exit))
                for k, r in zip(d.index, d.itertuples())} for s, d in t.items()}


def chandelier_exit(bars: dict, mult: float = 3.0, n: int = 22) -> dict:
    out = {}
    for s, df in bars.items():
        x = df.close < df.close.rolling(n).max() - mult * _atr(df)
        out[s] = {pd.Timestamp(k).date(): bool(v) for k, v in x.items()}
    return out
