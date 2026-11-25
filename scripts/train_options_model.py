#!/usr/bin/env python3
"""Train the options ML (data/models/options_ml.json) and print a short report.

Data: modelled call trades in data/ml_dataset/trades.csv.gz (source=options_entry) plus any live
option outcomes in data/trader.db (table option_outcomes, read-only), weighted OPTIONS_ML_LIVE_WEIGHT.
Validation: train before 2025-01-01, validate on 2025; the saved model is refit on everything.
AUC near 0.50 means no skill; that is recorded as it is.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import options_ml  # noqa: E402
from app.config import DATA_DIR  # noqa: E402


def live_rows(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM option_outcomes").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        sel = json.loads(r["selection_json"] or "{}")
        if r["ret"] is None:
            continue
        out.append({"proposal_id": r["proposal_id"], "features": sel.get("features") or {},
                    "delta": sel.get("delta"), "dte": sel.get("dte"), "iv": sel.get("iv"),
                    "ret": float(r["ret"]), "closed_at": r["closed_at"]})
    return out


def main() -> int:
    from app.config import load_settings
    weight = load_settings().options_ml_live_weight
    live = live_rows(DATA_DIR / "trader.db")
    m = options_ml.train(live, live_weight=weight)
    if m is None:
        print("Not enough data to train (no dataset and fewer than "
              f"{options_ml.MIN_LIVE_ONLY_ROWS} live outcomes).")
        return 0
    print(options_ml.report_line(m))
    print(f"  validation rows {m['n_val']}, features {len(m['features'])}, version {m['version']}")
    print(f"  saved {options_ml.MODEL_PATH}")
    w = sorted(zip(m["features"], m["weights"][1:]), key=lambda t: -abs(t[1]))[:5]
    print("  largest standardized weights: " + ", ".join(f"{k} {v:+.3f}" for k, v in w))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
