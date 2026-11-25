"""Build app/sleeves/reference/*.csv.gz and bands.json (docs/SLEEVES_ARCHITECTURE.md section 8).

Rows after the locked holdout date (2025-11-21) are dropped right after loading.
Run from the project root:  python scripts/build_sleeve_reference.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "sleeves" / "reference"
HOLDOUT_END = "2025-11-21"
HORIZON, PATHS, BLOCK, SEED = 252, 2000, 21, 7
QUANTILES = {"p5": 5, "p25": 25, "p50": 50, "p75": 75, "p95": 95}


def _load(sleeve: str) -> pd.Series:
    d = ROOT / "data"
    if sleeve == "swing":
        df = pd.read_csv(d / "backtests/sleeves_2026-11-25/swing_live87_daily.csv")
        df = df[df["date"] <= HOLDOUT_END]
        s = pd.Series(df["equity"].values, index=df["date"].values).pct_change().dropna()
        return s
    if sleeve == "momentum":
        df = pd.read_csv(d / "experiments/momentum_variants/daily_returns.csv.gz",
                         usecols=["date", "app87|N10|plain_12-1"])
        col = "app87|N10|plain_12-1"
    elif sleeve == "putwrite":
        df = pd.read_csv(d / "experiments/options_research/daily_a_putwrite30_QQQ.csv.gz", usecols=["date", "ret"])
        col = "ret"
    else:
        df = pd.read_csv(d / "experiments/rotation/catalog_daily_returns.csv.gz", usecols=["date", "vaa_g12"])
        col = "vaa_g12"
    df = df[df["date"] <= HOLDOUT_END].dropna()
    return pd.Series(df[col].values, index=df["date"].values)


def _bands(r: np.ndarray) -> dict[str, list[float]]:
    rng = np.random.default_rng(SEED)
    n = len(r)
    nblk = -(-HORIZON // BLOCK)
    starts = rng.integers(0, n, size=(PATHS, nblk))
    idx = (starts[:, :, None] + np.arange(BLOCK)[None, None, :]) % n      # circular blocks
    paths = r[idx.reshape(PATHS, -1)[:, :HORIZON]]
    cum = np.cumprod(1.0 + paths, axis=1) - 1.0
    return {k: [round(float(v), 6) for v in np.percentile(cum, q, axis=0)] for k, q in QUANTILES.items()}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bands = {}
    for sid in ("swing", "momentum", "putwrite", "vaa"):
        s = _load(sid)
        assert s.index.max() <= HOLDOUT_END
        pd.DataFrame({"date": s.index, "ret": s.values.round(6)}).to_csv(
            OUT / f"{sid}.csv.gz", index=False, compression="gzip")
        window = f"{s.index.min()[:7]}..{s.index.max()[:7]} backtest"
        bands[sid] = {"window": window, **_bands(s.values.astype(float))}
        print(sid, len(s), window)
    (OUT / "bands.json").write_text(json.dumps(bands, separators=(",", ":")))


if __name__ == "__main__":
    main()
