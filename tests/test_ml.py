import pytest
from app import ml  # noqa: E402

torch = pytest.importorskip("torch")


def _row(d, sym="A", y=1.0, fwd=1.0, ta=80, p=None):
    r = {"date": d, "symbol": sym, "x": [0.1] * len(ml.FEATURES), "y": y, "fwd": fwd, "ta": ta}
    return r


def test_et_day_uses_eastern_date():
    assert ml._et_day("2025-06-10T01:00:00+00:00") == "2025-06-09"   # after 8pm ET -> still the 9th
    assert ml._et_day("2025-06-10T15:00:00") == "2025-06-10"
    assert ml._et_day(None) == ""


def test_champion_comparison_requires_beating_on_both():
    rows = [_row("2025-01-01", y=1.0, fwd=2.0), _row("2025-01-02", y=0.0, fwd=-1.0)]
    win = ml._champion_comparison([{"rows": rows, "ps": [0.9, 0.1], "cps": [0.9, 0.9]}], 50, 0.5)
    assert win["beats"] is True and win["challenger_hit"] == 1.0 and win["champion_hit"] == 0.5
    lose = ml._champion_comparison([{"rows": rows, "ps": [0.9, 0.9], "cps": [0.9, 0.1]}], 50, 0.5)
    assert lose["beats"] is False
    assert ml._champion_comparison([{"rows": rows, "ps": [0.9, 0.9], "cps": None}], 50, 0.5) is None


def test_fit_purges_validation_and_normalises_on_train_only():
    import datetime as dt
    base = dt.date(2024, 1, 1)
    rows = []
    for i in range(100):
        d = (base + dt.timedelta(days=i)).isoformat()
        r = _row(d, y=float(i % 2))
        r["x"] = [float(i)] * len(ml.FEATURES)
        rows.append(r)
    model, mean, std = ml._fit(rows, epochs=2)
    # cut=85, validation starts at date idx 85; train keeps dates < idx 65 -> mean of 0..64 = 32
    assert abs(float(mean[0]) - 32.0) < 1e-4


def test_stop_gap_fills_at_open(test_settings):
    bars = []
    for i in range(160):
        px = 100.0 + (i % 3) * 0.1
        bars.append({"time": f"2025-01-{(i % 28) + 1:02d}", "open": px, "high": px + 1, "low": px - 1,
                     "close": px, "volume": 1_000_000})
    # distinct dates
    import datetime as dt
    for i, b in enumerate(bars):
        b["time"] = dt.datetime(2025, 1, 1, 14, 30, tzinfo=dt.timezone.utc) + dt.timedelta(days=i)
    gap = bars[120]
    gap.update(open=50.0, high=51.0, low=49.0, close=50.0)
    rows = ml.build_dataset({"A": bars}, None, test_settings)
    assert min(x["fwd"] for x in rows) < -20  # filled near the 50 open, not at the stop (~-5%)


def test_holdout_truncates_bars(test_settings):
    import datetime as dt
    bars = []
    for i in range(300):
        px = 100.0 + (i % 5)
        t = dt.datetime(2025, 6, 1, 14, 30, tzinfo=dt.timezone.utc) + dt.timedelta(days=i)
        bars.append({"time": t, "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 1_000_000})
    rows = ml.build_dataset({"A": bars}, None, test_settings, holdout_cutoff="2025-11-24")
    assert rows and max(r["date"] for r in rows) < "2025-11-24"
    # label window must end before the cutoff: last labelled day is >= 20 bars before the last kept bar
    kept_last = max(b["time"].date().isoformat() for b in bars if b["time"].date().isoformat() < "2025-11-24")
    assert max(r["date"] for r in rows) < kept_last


class _FakeDB:
    def __init__(self, trades):
        self.trades, self.logs = trades, []

    def closed_trades(self, limit=0):
        return self.trades

    def log(self, level, msg, alert=False):
        self.logs.append((level, msg, alert))


def _trade(pid, entry, exit_):
    return {"proposal_id": pid, "symbol": "A", "exit_reason": "target", "entry_fill_time": entry,
            "exit_fill_time": exit_, "entry_features": dict(zip(ml.FEATURES, [0.5] * len(ml.FEATURES))),
            "realized_pnl_pct": 3.0, "entry_ta_score": 70, "hold_seconds": 3600}


def test_ledger_holdout_drop_and_same_day_trades_kept(test_settings):
    db = _FakeDB([
        _trade(1, "2025-03-03T15:00:00+00:00", "2025-03-05T15:00:00+00:00"),
        _trade(2, "2025-03-03T16:00:00+00:00", "2025-03-06T15:00:00+00:00"),   # same symbol+day, kept
        _trade(3, "2025-11-20T15:00:00+00:00", "2025-11-25T15:00:00+00:00"),   # exit in holdout, dropped
        _trade(4, "2025-12-01T15:00:00+00:00", "2025-12-02T15:00:00+00:00"),   # entry in holdout, dropped
    ])
    rows = ml.build_dataset({}, db, test_settings, holdout_cutoff="2025-11-24")
    assert sorted(r["proposal_id"] for r in rows) == [1, 2]
    assert any("dropped" in m and "2 ledger" in m for _, m, _ in db.logs)


class _S:
    ml_retrain_days = 7
    broker_mode = "mock"


def test_champion_age_alert_once_per_day():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()

    class DB(_FakeDB):
        def champion_model(self):
            return {"id": 5, "created_at": old}

    f = ml.MLFilter.__new__(ml.MLFilter)
    f.s, f.db = _S(), DB([])
    assert f.check_champion_age() is True
    assert f.check_champion_age() is True
    alerts = [m for m in f.db.logs if m[2]]
    assert len(alerts) == 1 and alerts[0][0] == "warn"


def test_promote_with_bad_checkpoint_leaves_db_unchanged():
    class DB(_FakeDB):
        promoted = False

        def champion_model(self):
            return None

        def get_model_by_id(self, mid):
            return {"id": mid, "status": "challenger", "path": "does-not-exist.pt", "broker_mode": "mock"}

        def promote_challenger(self, *a):
            DB.promoted = True

    f = ml.MLFilter.__new__(ml.MLFilter)
    f.s, f.db = _S(), DB([])
    f.model = object()
    with pytest.raises(ValueError):
        f.promote_challenger(9)
    assert DB.promoted is False
    assert any(l[0] == "error" for l in f.db.logs)


def test_load_champion_failure_clears_model():
    class DB(_FakeDB):
        def champion_model(self):
            return {"id": 3, "path": "missing.pt", "broker_mode": "mock"}

        def active_model(self):
            return None

    f = ml.MLFilter.__new__(ml.MLFilter)
    f.s, f.db = _S(), DB([])
    f.model, f.mean, f.std, f.model_id, f.last_error = object(), 1, 1, 2, None
    f._load_champion()
    assert f.model is None and f.model_id is None and f.last_error


def test_champion_comparison_scores_only_rows_after_train_end():
    rows = [_row("2025-01-01", y=0.0, fwd=-1.0), _row("2025-03-01", y=1.0, fwd=2.0)]
    rec = [{"rows": rows, "ps": [0.9, 0.9], "cps": [0.9, 0.1]}]
    # in-sample row (Jan) ignored: champion's only OOS row is Mar where it abstains -> challenger wins
    cmp_ = ml._champion_comparison(rec, 50, 0.5, after="2025-02-01")
    assert cmp_["beats"] is True and cmp_["challenger_hit"] == 1.0 and cmp_["champion_hit"] == 0.0


def test_champion_comparison_skipped_without_oos_rows():
    rows = [_row("2025-01-01")]
    cmp_ = ml._champion_comparison([{"rows": rows, "ps": [0.9], "cps": [0.9]}], 50, 0.5, after="2025-06-01")
    assert cmp_["skipped"] is True and cmp_["beats"] is True and "skipped" in cmp_["note"]


def test_row_train_end_legacy_and_new():
    assert ml._row_train_end({"params": '{"train_end": "2025-05-01"}'}) == "2025-05-01"
    assert ml._row_train_end({"params": '{"features": []}'}) is None
    assert ml._row_train_end(None) is None
