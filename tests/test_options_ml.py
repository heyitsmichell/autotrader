"""Options ML (learning mode): model, exploration, ranking, min_p, outcomes, retrain, defaults."""

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from app import indicators, options_ml
from app.config import load_settings
from app.ml_data.schema import FEATURE_COLS
from tests.test_option_entries import TODAY, eng, sig, with_settings  # noqa: F401  (eng is a fixture)


def synth(n=400, seed=1):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, len(options_ml.ALL_FEATURES)))
    y = (X[:, 0] + 0.3 * rng.normal(size=n) > 0).astype(float)
    return X, y


@pytest.fixture
def model_path(tmp_path, monkeypatch):
    p = tmp_path / "options_ml.json"
    monkeypatch.setattr(options_ml, "MODEL_PATH", p)
    options_ml._cache.clear()
    yield p
    options_ml._cache.clear()


def write_model(path, bias=0.0, feature=None, weight=0.0):
    n = len(options_ml.ALL_FEATURES)
    w = [bias] + [0.0] * n
    if feature:
        w[1 + options_ml.ALL_FEATURES.index(feature)] = weight
    options_ml.save_model({
        "version": "t", "trained_at": "2026-01-01T00:00:00+00:00", "features": options_ml.ALL_FEATURES,
        "weights": w, "medians": [0.0] * n, "mean": [0.0] * n, "std": [1.0] * n,
        "n_backtest": 1, "n_live": 0, "val_auc": 0.5, "baseline_win_rate": 0.4}, path)


def test_logreg_learns_and_auc():
    X, y = synth()
    beta, med, mean, std = options_ml._fit_scaled(X, y, None)
    sc = options_ml._sigmoid(beta[0] + options_ml._transform(X, med, mean, std) @ beta[1:])
    assert options_ml.auc_score(y, sc) > 0.9
    assert options_ml.auc_score(np.ones(5), np.arange(5.0)) is None


def test_train_predict_and_scaler_persistence(tmp_path, model_path):
    X, y = synth()
    X[::7, 3] = np.nan                                    # missing values are median-imputed
    nf = len(options_ml.FEATURES)
    rows = [{"features": dict(zip(options_ml.FEATURES, r[:nf])),
             "delta": r[-3], "dte": r[-2], "iv": r[-1], "ret": 1.0 if t else -1.0}
            for r, t in zip(X, y)]
    m = options_ml.train(rows, dataset=tmp_path / "none.csv.gz", path=model_path)
    assert m["n_live"] == 400 and m["n_backtest"] == 0
    on_disk = json.loads(model_path.read_text(encoding="utf-8"))
    assert on_disk["mean"] == m["mean"] and on_disk["std"] == m["std"]
    assert on_disk["features"] == options_ml.ALL_FEATURES
    p = options_ml.predict(rows[0]["features"], abs(rows[0]["delta"]), 30, 0.3, path=model_path)
    assert 0.0 < p < 1.0
    assert options_ml.predict({}, None, None, None, path=model_path) is not None   # all missing -> imputed


def test_no_model_predict_none(tmp_path):
    assert options_ml.predict({}, 0.7, 30, 0.3, path=tmp_path / "missing.json") is None


def test_retrain_skipped_without_data(tmp_path, model_path):
    s = load_settings()
    none = tmp_path / "none.csv.gz"
    assert options_ml.train([], dataset=none, path=model_path) is None
    assert not model_path.exists()
    assert options_ml.retrain_due(s, path=model_path, dataset=none, n_live=5) is False
    assert options_ml.retrain_due(s, path=model_path, dataset=none, n_live=25) is True


def test_explore_arms_deterministic_and_clamped():
    s = dataclasses.replace(load_settings(), options_ml_explore_pct=1.0)
    a = options_ml.explore_arm(5, "AAPL", "2026-11-20", s)
    assert a == options_ml.explore_arm(5, "AAPL", "2026-11-20", s)
    assert a[0] in options_ml.DELTA_ARMS and a[1] in options_ml.DTE_ARMS
    arms = {options_ml.explore_arm(i, "AAPL", "d", s) for i in range(200)}
    assert len(arms) > 3
    tight = dataclasses.replace(s, options_delta_min=0.6, options_delta_max=0.75,
                                options_min_dte=25, options_max_dte=35)
    for i in range(100):
        d, t = options_ml.explore_arm(i, "X", "d", tight)
        assert 0.6 <= d <= 0.75 and 25 <= t <= 35
    off = dataclasses.replace(s, options_ml_explore_pct=0.0)
    assert options_ml.explore_arm(1, "A", "d", off) is None


def test_live_features_align_with_dataset_columns():
    bars = [{"time": datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i), "open": 100 + i * .1,
             "high": 101 + i * .1, "low": 99 + i * .1, "close": 100 + i * .1, "volume": 1000 + i}
            for i in range(300)]
    live = indicators.features(bars)
    cols = {c[2:] for c in FEATURE_COLS}
    assert set(options_ml.FEATURES) <= cols
    assert set(options_ml.FEATURES) <= set(live)


async def test_default_off_unchanged(eng):
    await eng.propose_option_entries(1, [sig()])
    sel = eng.captured[0]["selection"]
    assert "ml_version" not in sel and "explore" not in sel and sel["ml_p"] == 0.6
    assert "ML" not in eng.captured[0]["rationale"]


async def test_selection_fields_and_rationale(eng, model_path):
    write_model(model_path, bias=0.0)
    with_settings(eng, options_ml_enabled=True, options_ml_explore_pct=0.0)
    await eng.propose_option_entries(1, [sig(features={"rsi14": 55.0})])
    p = eng.captured[0]
    sel = p["selection"]
    assert sel["ml_p"] == 0.5 and sel["explore"] is False and sel["arm"] is None
    assert sel["ml_version"] == "t@2026-01-01" and sel["features"] == {"rsi14": 55.0}
    assert "ML 0.50" in p["rationale"] and "explore" not in p["rationale"]


async def test_explore_uses_arm_and_marks(eng, model_path):
    write_model(model_path)
    with_settings(eng, options_ml_enabled=True, options_ml_explore_pct=1.0)
    await eng.propose_option_entries(3, [sig()])
    p = eng.captured[0]
    sel = p["selection"]
    assert sel["explore"] is True and sel["arm"] and "· explore" in p["rationale"]


async def test_ranking_by_p_desc(eng, model_path):
    write_model(model_path, feature="rsi14", weight=1.0)
    with_settings(eng, options_ml_enabled=True, options_ml_explore_pct=0.0)
    cands = [sig("MSFT", features={"rsi14": -2.0}), sig("AAPL", features={"rsi14": 2.0})]
    await eng.propose_option_entries(1, cands)
    assert [p["underlying"] for p in eng.captured] == ["AAPL", "MSFT"]


async def test_min_p_skips_but_not_when_exploring(eng, model_path):
    write_model(model_path, bias=-3.0)                       # p ~ 0.05
    with_settings(eng, options_ml_enabled=True, options_ml_explore_pct=0.0, options_ml_min_p=0.3)
    assert await eng.propose_option_entries(1, [sig()]) == 0
    text = " | ".join(r[0] for r in eng.db.conn.execute("SELECT message FROM events").fetchall())
    assert "options ML P(win)" in text
    with_settings(eng, options_ml_explore_pct=1.0)
    assert await eng.propose_option_entries(2, [sig()]) == 1


def _buy(db, sym, limit, sel, status="placed", exp=None):
    inst = {"option_type": "CALL", "underlying": "AAPL", "selection": sel, "expiration_date": exp}
    return db.add_proposal(1, {"symbol": sym, "side": "BUY", "quantity": 1, "limit_price": limit},
                           status, "", limit, asset_class="option", position_intent="BUY_TO_OPEN",
                           expiration_date=exp, instrument_json=json.dumps(inst))


def test_outcome_recording_and_live_rows(test_engine):
    db = test_engine.db
    sym = "AAPL261218C00100000"
    sel = {"delta": 0.7, "dte": 30, "iv": 0.3, "ml_p": 0.54, "ml_version": "t", "explore": True,
           "features": {"rsi14": 60.0}}
    bid = _buy(db, sym, 2.0, sel)
    db.add_proposal(1, {"symbol": sym, "side": "SELL", "quantity": 1, "limit_price": 3.0},
                    "placed", "option exit: premium_stop", 3.0, asset_class="option",
                    position_intent="SELL_TO_CLOSE")
    assert options_ml.record_closed_outcomes(db, {sym}, date(2026, 11, 20)) == 0     # still held
    assert options_ml.record_closed_outcomes(db, set(), date(2026, 11, 20)) == 1
    assert options_ml.record_closed_outcomes(db, set(), date(2026, 11, 20)) == 0     # once only
    r = db.option_outcomes()[0]
    assert r["proposal_id"] == bid and r["ret"] == pytest.approx(0.5)
    assert r["exit_reason"] == "premium_stop" and r["explore"] == 1 and r["entry_premium"] == 2.0
    live = options_ml.live_training_rows(db)
    assert live[0]["features"] == {"rsi14": 60.0} and live[0]["ret"] == pytest.approx(0.5)


def test_expired_unsold_needs_a_fill(test_engine):
    db = test_engine.db
    _buy(db, "AAPL261101C00100000", 2.0, {"delta": 0.7}, exp="2026-11-01")
    assert options_ml.record_closed_outcomes(db, set(), date(2026, 11, 20)) == 0     # never filled
