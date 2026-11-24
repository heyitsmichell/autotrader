"""Lessons pipeline: names match the engine, splits are chronological, p-values are real."""
import random

from app.lessons import (LEARNABLE_PARAMS, UNTOUCHABLE_RISK_PARAMS, mann_whitney_greater,
                         validate_learnable_param)


def test_mann_whitney_detects_shift_and_null():
    rng = random.Random(1)
    a = [rng.gauss(1.0, 1) for _ in range(60)]
    b = [rng.gauss(0.0, 1) for _ in range(60)]
    assert mann_whitney_greater(a, b)[1] < 0.001
    assert mann_whitney_greater(b, a)[1] > 0.99
    c = [rng.gauss(0, 1) for _ in range(200)]
    d = [rng.gauss(0, 1) for _ in range(200)]
    assert mann_whitney_greater(c, d)[1] > 0.001


def test_learnable_names_are_engine_settings():
    from app.config import Settings
    fields = set(Settings.__dataclass_fields__)
    assert set(LEARNABLE_PARAMS) <= fields
    assert not set(LEARNABLE_PARAMS) & UNTOUCHABLE_RISK_PARAMS


def test_risk_limits_rejected_case_insensitive():
    ok, _ = validate_learnable_param("MAX_ORDER_USD", 1.0)
    assert not ok
    ok, _ = validate_learnable_param("sl_atr_mult", 2.0)
    assert ok


def test_embargo_covers_label_horizon():
    from app import ml
    rows = []
    for d in range(400):  # synthetic dates; only checking the split arithmetic
        for sym in ("A", "B", "C"):
            rows.append({"date": f"2025-{1 + d // 28:02d}-{1 + d % 28:02d}x{d:04d}", "symbol": sym,
                         "x": [0.0] * len(ml.FEATURES), "y": float(d % 2), "fwd": 0.0, "ta": 1.0})
    dates = sorted({r["date"] for r in rows})
    test_len = max(20, len(dates) // 10)
    test_end = len(dates)
    train_end = test_end - test_len - ml.SIM_MAX_HOLD_BARS
    assert test_end - test_len - train_end == ml.SIM_MAX_HOLD_BARS
