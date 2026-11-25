import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.ml_data import (ALL_COLS, BarStore, append_trades, features_at, is_holdout, load, spy_regime_at)  # noqa: E402


def _write(d: Path, sym: str, n=320, bump_after=None):
    d.mkdir(parents=True, exist_ok=True)
    rows, day, px = [], date(2024, 1, 2), 100.0
    while len(rows) < n:
        if day.weekday() < 5:
            px *= 1.001 + 0.004 * ((len(rows) * 7) % 5 - 2) / 2
            if bump_after and day.isoformat() > bump_after:
                px *= 1.5
            rows.append([day.isoformat(), px, px * 1.01, px * 0.99, px, 1e6 + len(rows)])
        day += timedelta(days=1)
    with open(d / f"{sym}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "open", "high", "low", "close", "volume"]); w.writerows(rows)
    return rows[260][0]


def test_no_lookahead(tmp_path):
    day = _write(tmp_path / "a", "XYZ"); _write(tmp_path / "a", "SPY")
    _write(tmp_path / "b", "XYZ", bump_after=day); _write(tmp_path / "b", "SPY", bump_after=day)
    fa = features_at("XYZ", day, BarStore([tmp_path / "a"]))
    fb = features_at("XYZ", day, BarStore([tmp_path / "b"]))
    assert fa and fa == fb
    assert fa == features_at("XYZ", day, BarStore([tmp_path / "b"]), truncate=True)
    assert spy_regime_at(day, BarStore([tmp_path / "a"])) == spy_regime_at(day, BarStore([tmp_path / "b"]))
    assert features_at("XYZ", "2030-01-01", BarStore([tmp_path / "a"])) is None


def test_schema_and_holdout(tmp_path):
    day = _write(tmp_path / "a", "XYZ"); _write(tmp_path / "a", "SPY")
    st, p = BarStore([tmp_path / "a"]), tmp_path / "t.csv.gz"
    rows = [{"symbol": "XYZ", "variant": "v", "signal_date": day, "entry_date": "2025-11-21"},
            {"symbol": "XYZ", "variant": "v", "signal_date": day, "entry_date": "2025-11-24"}]
    append_trades("unit", rows, path=p, store=st)
    append_trades("unit", rows, path=p, store=st)   # idempotent
    d = load(p)
    assert list(d.columns) == ALL_COLS and len(d) == 2
    assert d["holdout"].tolist() == [0, 1]
    assert d["f_rsi14"].notna().all() and d["spy_trend"].notna().all()
    assert is_holdout("2025-11-23") == 0 and is_holdout("2025-11-24") == 1
    assert len(load(p, holdout=0)) == 1


def test_registry_backfill_idempotent(tmp_path):
    import backfill_registry as B
    reg, lb, wide = tmp_path / "r.csv", tmp_path / "lb.csv", tmp_path / "w.json"
    pd.DataFrame(columns=B.COLS).to_csv(reg, index=False)
    pd.DataFrame([{"name": "a", "kind": "sweep", "universe": "u", "cagr": .1, "sharpe": 1.0, "maxdd": -.2,
                   "turnover_yr": 3, "n_days": 10, "dsr": .5},
                  {"name": "b", "kind": "benchmark", "universe": "u", "cagr": 0, "sharpe": 0, "maxdd": 0,
                   "turnover_yr": 0, "n_days": 1, "dsr": 0}]).to_csv(lb, index=False)
    import json
    wide.write_text(json.dumps({"n_trials": 2, "window": ["2022-01-01", "2025-01-01"],
                                "all": [{"name": "xsmom_w|none|d|p10|noml", "trades": 5, "expR": .1, "pen": .0,
                                         "cagr": .1, "sharpe": 1, "max_dd": .1}]}))
    r1 = B.backfill(reg, lb, wide, tmp_path / "bak.csv")
    r2 = B.backfill(reg, lb, wide, tmp_path / "bak.csv")
    assert r1["appended"] == 2 and r1["unrecoverable"] == 1
    assert r2["appended"] == 0 and len(pd.read_csv(reg)) == 2
