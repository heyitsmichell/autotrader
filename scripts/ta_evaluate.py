"""Evaluate the TA scoring model (v2; v1 was retired) on your cached daily bars, with no look-ahead.

    ./.venv/bin/python scripts/ta_evaluate.py [--dir data/bars] [--horizon 10] [--min-bars 120]

For every symbol and day it scores the setup with each model and measures the return over
the next HORIZON trading days. It prints, per model:
  * IC: rank correlation between score and forward return (higher is better; > 0.03 is
    useful for a daily signal, ~0 is noise, negative means the score points the wrong way)
  * mean forward return by score quintile (should rise from Q1 to Q5)
  * what happens above the live threshold (CONSENSUS_TA_MIN): trades, mean return, hit rate
Overlapping windows inflate sample size, so treat small IC differences as noise. Compare
models on the same bars and prefer the one that is better across most symbols, not one.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ta  # noqa: E402
from app.backtest.data import read_bars_csv  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.indicators import feature_series  # noqa: E402


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 30 or len(set(x)) < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return None if den == 0 else num / den


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "data" / "bars"))
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--min-bars", type=int, default=120)
    args = ap.parse_args()

    threshold = load_settings().consensus_ta_min
    files = sorted(Path(args.dir).glob("*.csv"))
    if not files:
        print(f"No CSV bars in {args.dir}. Run a backtest first (it caches bars there).")
        return 1

    models = ("v2",)   # v1 was retired (QA L14); kept as a tuple so the report layout is unchanged
    scores = {m: [] for m in models}
    fwd: list[float] = []
    per_symbol: dict[str, dict[str, float | None]] = {}
    for path in files:
        bars = read_bars_csv(path, path.stem)
        if len(bars) < args.min_bars + args.horizon:
            continue
        feats = feature_series(bars)
        sym_scores = {m: [] for m in models}
        sym_fwd: list[float] = []
        for i in range(60, len(bars) - args.horizon):       # 60 = SMA50 + ADX warm-up
            r = (bars[i + args.horizon]["close"] / bars[i]["close"] - 1) * 100
            sym_fwd.append(r)
            for m in models:
                sym_scores[m].append(ta.score(feats[i], m)[0])
        fwd += sym_fwd
        for m in models:
            scores[m] += sym_scores[m]
        per_symbol[path.stem] = {m: spearman(sym_scores[m], sym_fwd) for m in models}

    if not fwd:
        print("Not enough history. Need at least min-bars + horizon bars per symbol.")
        return 1
    print(f"{len(files)} files, {len(fwd)} symbol-days, horizon {args.horizon}d, threshold {threshold}\n")
    for m in models:
        sc = scores[m]
        ic = spearman(sc, fwd)
        print(f"== {m} ==  IC {'n/a' if ic is None else f'{ic:+.3f}'}   "
              f"score mean {statistics.fmean(sc):+.3f}  sd {statistics.pstdev(sc):.3f}")
        order = sorted(range(len(sc)), key=lambda i: sc[i])
        n = len(order)
        row = []
        for q in range(5):
            idx = order[q * n // 5:(q + 1) * n // 5]
            row.append(statistics.fmean(fwd[i] for i in idx))
        print("   mean fwd return by quintile (Q1 lowest score .. Q5 highest): "
              + "  ".join(f"{v:+.2f}%" for v in row))
        hit = [fwd[i] for i in range(n) if sc[i] >= threshold]
        if hit:
            print(f"   score >= {threshold}: {len(hit)} setups ({len(hit) / n:.0%} of days), "
                  f"mean {statistics.fmean(hit):+.2f}%, wins {sum(r > 0 for r in hit) / len(hit):.0%}")
        else:
            print(f"   score >= {threshold}: no setups")
    print("Base rate (all days): mean "
          f"{statistics.fmean(fwd):+.2f}%, wins {sum(r > 0 for r in fwd) / len(fwd):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())