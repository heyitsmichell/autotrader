"""Leaderboard CSV (one row per variant per run) + ranking + promotion rule (reported only, never applied)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

LB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "experiments" / "leaderboard.csv"
COLS = ["timestamp", "variant", "config_hash", "scope", "n_trials", "trades", "exp_R", "se", "penalized",
        "all_positive", "fold_exp_R", "fold_trades", "max_dd", "seconds"]


def to_row(r: dict, ts: str | None = None) -> dict:
    folds = r.get("folds") or []
    return {"timestamp": ts or datetime.now().isoformat(timespec="seconds"), "variant": r["variant"],
            "config_hash": r["config_hash"], "scope": r["scope"], "n_trials": r.get("n_trials"),
            "trades": r["trades"], "exp_R": round(r["exp_R"], 5), "se": round(r.get("se", 0.0), 5),
            "penalized": round(r.get("penalized", 0.0), 5), "all_positive": r.get("all_positive", ""),
            "fold_exp_R": json.dumps([round(f["exp_R"], 4) for f in folds]),
            "fold_trades": json.dumps([f["trades"] for f in folds]),
            "max_dd": round(r.get("max_dd", 0.0), 4), "seconds": r.get("seconds", "")}


def append(results: list[dict], path: Path = LB_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([to_row(r) for r in results], columns=COLS)
    df.to_csv(path, mode="a", header=not path.exists(), index=False)


def load(path: Path = LB_PATH) -> pd.DataFrame:
    return pd.read_csv(path) if Path(path).exists() else pd.DataFrame(columns=COLS)


def rank(df: pd.DataFrame, scope: str = "research") -> pd.DataFrame:
    """Latest row per (variant, config_hash) for the scope, sorted by penalized score."""
    d = df[df.scope == scope].sort_values("timestamp").drop_duplicates(["variant", "config_hash"], keep="last")
    d = d.sort_values("penalized", ascending=False).reset_index(drop=True)
    d.index += 1
    return d


def promotable(ch: dict, champ: dict, ch_holdout: float | None = None, champ_holdout: float | None = None):
    """ch/champ: research rows (penalized, fold_exp_R list). Holdout args are holdout expectancies (R);
    enforced only when the challenger's holdout was run. Returns (bool, reasons)."""
    why = []
    if not ch["penalized"] > champ["penalized"]:
        why.append("penalized score does not beat champion")
    folds = ch["fold_exp_R"]
    if not folds or not all(x > 0 for x in folds):
        why.append("not positive in every research fold")
    if ch_holdout is not None:
        if champ_holdout is None:
            why.append("champion holdout not run")
        elif not ch_holdout > champ_holdout:
            why.append("does not beat champion on holdout")
    return (not why), why


def table(d: pd.DataFrame, champion: str, top: int = 10) -> str:
    lines = [f"{'#':>2} {'variant':<24}{'trades':>7}{'expR':>8}{'penal':>8}  folds   maxDD"]
    for i, r in d.head(top).iterrows():
        f = json.loads(r.fold_exp_R) if isinstance(r.fold_exp_R, str) else []
        signs = "".join("+" if x > 0 else "-" for x in f)
        star = "*" if r.variant == champion else " "
        lines.append(f"{i:>2} {r.variant:<23}{star}{int(r.trades):>6}{r.exp_R:>+8.3f}{r.penalized:>+8.3f}  "
                     f"{signs:<7} {r.max_dd*100:5.1f}%")
    return "\n".join(lines)
