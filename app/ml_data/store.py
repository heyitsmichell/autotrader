"""Reader/writer for data/ml_dataset/trades.csv.gz."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .features import BarStore, features_at, spy_regime_at
from .schema import ALL_COLS, is_holdout

ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_PATH = ROOT / "data" / "ml_dataset" / "trades.csv.gz"


def normalize_rows(source: str, rows, store: BarStore | None = None, add_features: bool = True) -> pd.DataFrame:
    """rows: dicts using schema names. Fills source, holdout, signal-day features and SPY regime (unless the
    row already carries f_/spy_ values). Returns a frame with exactly ALL_COLS."""
    out = []
    for r in rows:
        r = dict(r)
        r["source"] = source
        if r.get("holdout") in (None, ""):
            r["holdout"] = is_holdout(r.get("entry_date"))
        sd = r.get("signal_date")
        if add_features and sd and not any(str(k).startswith("f_") for k in r):
            f = features_at(r["symbol"], sd, store)
            if f:
                r.update({"f_" + k: v for k, v in f.items() if k != "date"})
        if add_features and sd and not any(str(k).startswith("spy_") for k in r):
            r.update(spy_regime_at(sd, store))
        out.append(r)
    return pd.DataFrame(out).reindex(columns=ALL_COLS)


def append_trades(source: str, rows, path: Path | None = None, store: BarStore | None = None,
                  add_features: bool = True) -> int:
    """Append rows; earlier rows with the same (source, variant) are replaced so it stays idempotent.
    Returns the total row count of the file."""
    path = Path(path or DATASET_PATH)
    df = normalize_rows(source, rows, store, add_features)
    if df.empty:
        return 0
    if path.exists():
        old = pd.read_csv(path, low_memory=False)
        keys = set(zip(df["source"], df["variant"].fillna("")))
        keep = [(s, v) not in keys for s, v in zip(old["source"], old["variant"].fillna(""))]
        df = pd.concat([old[keep].reindex(columns=ALL_COLS), df], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")
    return len(df)


def load(path: Path | None = None, holdout: int | None = None, source: str | None = None) -> pd.DataFrame:
    """Read the dataset; holdout=0 gives the trainable rows."""
    df = pd.read_csv(Path(path or DATASET_PATH), low_memory=False)
    if holdout is not None:
        df = df[df["holdout"] == holdout]
    if source is not None:
        df = df[df["source"] == source]
    return df.reset_index(drop=True)
