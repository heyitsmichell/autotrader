"""Reader/writer for data/ml_dataset/trades.csv.gz."""
from __future__ import annotations

import contextlib
import os
import time
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path.with_name(path.name + ".lock")):
        if path.exists():
            old = pd.read_csv(path, low_memory=False)
            keys = set(zip(df["source"], df["variant"].fillna("")))
            keep = [(s, v) not in keys for s, v in zip(old["source"], old["variant"].fillna(""))]
            df = pd.concat([old[keep].reindex(columns=ALL_COLS), df], ignore_index=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        df.to_csv(tmp, index=False, compression="gzip")
        os.replace(tmp, path)
    return len(df)


@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout: float = 60.0, stale: float = 300.0):
    """Cross-platform lock file (O_EXCL create + retry); a lock older than `stale` seconds is broken."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not acquire {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def load(path: Path | None = None, holdout: int | None = None, source: str | None = None) -> pd.DataFrame:
    """Read the dataset; holdout=0 gives the trainable rows."""
    df = pd.read_csv(Path(path or DATASET_PATH), low_memory=False)
    if holdout is not None:
        df = df[df["holdout"] == holdout]
    if source is not None:
        df = df[df["source"] == source]
    return df.reset_index(drop=True)
