"""12-1 momentum: top-n by close[-22]/close[-253]-1 (matches rotation_lab mom_12_1)."""
from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger("app.sleeves")


def targets(closes: pd.DataFrame, n: int = 10) -> dict[str, float]:
    if closes is None or closes.empty:
        return {}
    closes = closes.ffill(limit=1)
    score = (closes.shift(21) / closes.shift(252) - 1).iloc[-1]
    dropped = sorted(score[score.isna()].index)
    if dropped:
        log.info("momentum: excluded %d symbol(s) with missing data: %s", len(dropped), ", ".join(map(str, dropped[:20])))
    score = score.dropna()
    if n <= 0 or len(score) < n:
        return {}
    top = score.sort_values(ascending=False, kind="stable").index[:n]
    return {str(s): 1.0 / n for s in top}
