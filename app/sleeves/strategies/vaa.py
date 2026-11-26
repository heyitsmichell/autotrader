"""VAA (G12 offensive, LQD/IEF/SHY defensive, B=4, top 2, 13612W) - mirrors rotation_lab.catalog.vaa."""
from __future__ import annotations

import math

import pandas as pd

G12 = ["SPY", "IWM", "QQQ", "VGK", "EWJ", "VWO", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD"]
DEFENSIVE = ["LQD", "IEF", "SHY"]
B = 4
TOPO = 2


def _rl(c: pd.Series, n: int) -> float:
    return c.iloc[-1] / c.iloc[-1 - n] - 1.0 if len(c) > n else float("nan")


def m13612w(c: pd.Series) -> float:
    return 12 * _rl(c, 21) + 4 * _rl(c, 63) + 2 * _rl(c, 126) + _rl(c, 252)


def targets(closes: pd.DataFrame) -> dict[str, float]:
    need = G12 + [s for s in DEFENSIVE if s not in G12]
    if closes is None or any(s not in closes.columns for s in need):
        return {}
    closes = closes.ffill(limit=1)
    mo = {s: m13612w(closes[s]) for s in need}
    if any(math.isnan(x) for x in mo.values()):
        return {}
    vo = {s: mo[s] for s in G12}
    vd = {s: mo[s] for s in DEFENSIVE}
    cf = min(1.0, sum(1 for x in vo.values() if x <= 0) / B)
    w = {max(vd, key=vd.get): cf}
    for s in sorted(vo, key=vo.get, reverse=True)[:TOPO]:
        w[s] = w.get(s, 0) + (1 - cf) / TOPO
    return {k: v for k, v in w.items() if v > 0}
