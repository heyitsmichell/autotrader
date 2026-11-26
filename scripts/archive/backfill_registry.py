"""Backfill trials_registry.csv with the rotation-sweep rows and wide-sweep trials that were never logged.

Idempotent: skips rows whose (lab, strategy) or config_hash already exist. One append write at the end.
Usage: python scripts/archive/backfill_registry.py [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
EXP = ROOT / "data" / "experiments"
COLS = ["ts", "lab", "strategy", "config_hash", "universe", "start", "end", "cagr", "sharpe", "maxdd", "turnover", "notes"]


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:10]


def rotation_rows(lb_path: Path, ts: str) -> list[dict]:
    lb = pd.read_csv(lb_path)
    lb = lb[lb["kind"] != "benchmark"]
    out = []
    for r in lb.itertuples():
        if r.kind == "catalog":
            continue  # already logged as lab=rotation_catalog
        out.append({"ts": ts, "lab": "rotation_sweep", "strategy": r.name, "config_hash": _h("rotation_sweep|" + str(r.name)),
                    "universe": r.universe, "start": "2011-01-03", "end": "2025-11-23",
                    "cagr": round(float(r.cagr), 4), "sharpe": round(float(r.sharpe), 3),
                    "maxdd": round(-abs(float(r.maxdd)), 4), "turnover": round(float(r.turnover_yr), 2),
                    "notes": f"backfilled rotation {r.kind} row; n_days={r.n_days}; dsr={float(r.dsr):.3f}"})
    return out


def wide_rows(path: Path, ts: str) -> tuple[list[dict], int]:
    j = json.loads(path.read_text())
    items = j.get("all") or j.get("top") or []
    w0, w1 = j.get("window", ["", ""])
    out = []
    for t in items:
        name = t["name"]
        try:
            from app.strategy_lab import variants as V
            e, reg, ex, mp, ml = name.split("|")
            h = V.make(e, ex, reg, int(mp[1:]), ml == "ml").config_hash()
        except Exception:
            h = _h("wide_sweep|" + name)
        out.append({"ts": ts, "lab": "wide_sweep", "strategy": name, "config_hash": h, "universe": "bars_yahoo_tech_semis",
                    "start": w0, "end": w1, "cagr": round(t.get("cagr") or 0, 4), "sharpe": round(t.get("sharpe") or 0, 3),
                    "maxdd": round(-abs(t.get("max_dd") or 0), 4), "turnover": t.get("trades", ""),
                    "notes": f"backfilled wide sweep; expR={t.get('expR', 0):.3f} pen={t.get('pen', 0):.3f} trades={t.get('trades')}; research folds only"})
    return out, int(j.get("n_trials", len(items))) - len(items)


def backfill(registry: Path, lb: Path, wide: Path, backup: Path | None, dry: bool = False) -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    before = pd.read_csv(registry)
    seen = {(a, b) for a, b in zip(before["lab"], before["strategy"])}
    hashes = set(before["config_hash"].astype(str))
    rows = rotation_rows(lb, ts)
    wr, unrec = wide_rows(wide, ts)
    new, skipped, seen_new = [], 0, set()
    for r in rows + wr:
        k = (r["lab"], r["strategy"])
        if k in seen or k in seen_new or (r["lab"] == "wide_sweep" and r["config_hash"] in hashes) or r["config_hash"] in seen_new:
            skipped += 1
            continue
        seen_new.add(k); seen_new.add(r["config_hash"]); new.append(r)
    if new and not dry:
        if backup and not backup.exists():
            shutil.copy2(registry, backup)
        with open(registry, "a", newline="") as f:  # single append write
            w = csv.DictWriter(f, fieldnames=COLS)
            buf = []
            for r in new:
                buf.append(r)
            w.writerows(buf)
    return {"before": len(before), "appended": len(new), "skipped": skipped, "unrecoverable": unrec,
            "after": len(before) + (0 if dry else len(new))}


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(backfill(EXP / "trials_registry.csv", EXP / "rotation" / "leaderboard.csv",
                   EXP / "search_20261125_012249.json", EXP / "trials_registry.backup_2026-11-25.csv", a.dry_run))
