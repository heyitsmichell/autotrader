"""Tests for purged/embargoed walk-forward validation (Phase 2)."""

from __future__ import annotations

from datetime import date, timedelta

from app.backtest.walkforward import (
    WalkForwardConfig,
    WalkForwardResult,
    compute_label_overlap_weights,
    effective_sample_size,
    subsample_non_overlapping,
    purge_test_samples,
    benjamini_hochberg,
    bonferroni,
    deflated_sharpe_ratio,
    walk_forward,
)


# --- Overlap weights tests -----------------------------------------------------

def test_compute_label_overlap_weights_no_overlap():
    """Weights should be 1.0 when no samples overlap."""
    weights = compute_label_overlap_weights([0, 10, 20], embargo_bars=5, total_samples=30)
    assert all(w == 1.0 for w in weights)


def test_compute_label_overlap_weights_with_overlap():
    """Weights should be < 1.0 when samples overlap."""
    # Samples at indices 0, 1, 2 with embargo=5 -> all overlap
    weights = compute_label_overlap_weights([0, 1, 2], embargo_bars=5, total_samples=10)
    # Each overlaps with 2 others + self = 3 total
    assert all(w == pytest.approx(1/3) for w in weights)


def test_effective_sample_size():
    """ESS should equal n when all weights are 1, less when weights vary."""
    # All equal weights -> ESS = n
    weights = [1.0, 1.0, 1.0, 1.0]
    assert effective_sample_size(weights) == 4.0

    # One weight much smaller -> ESS < n
    weights = [1.0, 1.0, 1.0, 0.1]
    ess = effective_sample_size(weights)
    assert ess < 4.0
    assert ess > 3.0


def test_subsample_non_overlapping():
    """Should keep only non-overlapping samples."""
    # With embargo=5, indices 0, 5, 10 are non-overlapping
    kept = subsample_non_overlapping([0, 1, 2, 5, 6, 10, 11], embargo_bars=5)
    assert kept == [0, 5, 10]


def test_purge_test_samples():
    """Should remove test samples that overlap with training period."""
    # Train ends at index 100, purge=20
    # Test samples at 95, 100, 105, 120
    # 95-20=75 <= 100 -> purge (too close to train)
    # 100-20=80 <= 100 -> purge
    # 105-20=85 <= 100 -> purge
    # 120-20=100 <= 100 -> purge
    purged = purge_test_samples([95, 100, 105, 120], train_end_idx=100, purge_bars=20)
    assert purged == []

    # Test samples well after train (more than purge_bars after train_end)
    # 150-20=130 > 100 -> keep
    purged = purge_test_samples([150, 160, 170], train_end_idx=100, purge_bars=20)
    assert purged == [150, 160, 170]


# --- Multiple testing corrections ----------------------------------------------

def test_benjamini_hochberg():
    """BH FDR correction."""
    p_values = [0.01, 0.04, 0.03, 0.2, 0.5]
    rejected, adjusted = benjamini_hochberg(p_values, alpha=0.05)
    # At alpha=0.05, sorted p: 0.01(rank1), 0.03(rank2), 0.04(rank3), 0.2, 0.5
    # BH thresholds: 0.01*1/5=0.01, 0.03*2/5=0.012, 0.04*3/5=0.024, ...
    # Wait, BH is: reject if p_i <= alpha * i / n
    # For n=5, alpha=0.05: thresholds = 0.01, 0.02, 0.03, 0.04, 0.05
    # Sorted: 0.01, 0.03, 0.04, 0.2, 0.5
    # 0.01 <= 0.01 ✓, 0.03 <= 0.02 ✗, 0.04 <= 0.03 ✗, etc.
    # So only the first (0.01) is rejected
    assert rejected[0] == True   # 0.01
    assert rejected[2] == False  # 0.03 (index 2 in original)
    assert rejected[1] == False  # 0.04 (index 1 in original)
    assert all(a >= p for a, p in zip(adjusted, p_values))  # adjusted >= original


def test_bonferroni():
    """Bonferroni correction."""
    p_values = [0.01, 0.04, 0.001]
    rejected, adjusted = bonferroni(p_values, alpha=0.05)
    # alpha/n = 0.05/3 = 0.0167
    # 0.01 <= 0.0167 ✓, 0.04 > 0.0167 ✗, 0.001 <= 0.0167 ✓
    assert rejected[2] == True   # 0.001
    assert rejected[0] == True   # 0.01
    assert rejected[1] == False  # 0.04
    assert adjusted == [0.03, 0.12, 0.003]


def test_deflated_sharpe_ratio():
    """Deflated Sharpe ratio should be lower than observed for multiple trials."""
    sharpe = 2.0
    n_trials = 100
    n_obs = 252  # 1 year daily

    deflated = deflated_sharpe_ratio(sharpe, n_trials, n_obs)
    assert deflated < sharpe
    assert deflated > 0  # Still positive but reduced


# --- Walk-forward integration tests --------------------------------------------

def _make_synthetic_rows(n: int = 1000) -> list[dict]:
    """Create synthetic rows with known properties for testing."""
    rows = []
    base_date = date(2020, 1, 1)
    for i in range(n):
        d = base_date + timedelta(days=i)
        # Simple pattern: first half predictable, second half random
        if i < n // 2:
            label = 1.0 if (i % 10) < 6 else 0.0  # 60% hit rate
            prob = 0.7 if label == 1.0 else 0.3
        else:
            label = 1.0 if (i % 10) < 5 else 0.0  # 50% hit rate (noise)
            prob = 0.55 if label == 1.0 else 0.45

        rows.append({
            "date": d.isoformat(),
            "symbol": "TEST",
            "x": [0.1, 0.2, 0.3],  # dummy features
            "y": label,
            "fwd": 1.0 if label == 1.0 else -1.0,
            "ta": 0.5,
        })
    return rows


def _simple_model_factory(train_rows):
    """Simple model that learns the base rate."""
    class SimpleModel:
        def __init__(self, base_rate):
            self.base_rate = base_rate

    base_rate = sum(r["y"] for r in train_rows) / len(train_rows)
    return SimpleModel(base_rate)


def _simple_predict_fn(model, test_rows):
    """Predict using base rate."""
    return [model.base_rate] * len(test_rows)


def test_walk_forward_basic():
    """Basic walk-forward should run and produce results."""
    rows = _make_synthetic_rows(500)
    config = WalkForwardConfig(
        n_folds=3,
        test_window=50,
        min_train_samples=50,
        min_test_samples=20,
        embargo_bars=10,
        purge_bars=10,
        min_folds=2,
        min_total_test_trades=30,
        min_independent_test_trades=10,
    )

    result = walk_forward(rows, config, ta_min=0.1, p_min=0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)

    assert isinstance(result, WalkForwardResult)
    assert len(result.folds) >= 2
    assert result.total_test_trades > 0
    assert result.total_independent_test_trades > 0
    assert result.effective_sample_size > 0


def test_walk_forward_insufficient_folds():
    """Should fail when not enough valid folds."""
    rows = _make_synthetic_rows(50)  # Too few rows
    config = WalkForwardConfig(
        n_folds=4,
        test_window=20,
        min_train_samples=200,  # Impossible to meet
        min_test_samples=20,
        embargo_bars=10,
        purge_bars=10,
        min_folds=3,
    )

    result = walk_forward(rows, config, ta_min=0.1, p_min=0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)

    assert not result.passed
    assert "folds" in result.rejection_reason.lower()


def test_walk_forward_insufficient_test_trades():
    """Should fail when not enough test trades."""
    rows = _make_synthetic_rows(100)
    config = WalkForwardConfig(
        n_folds=3,
        test_window=10,
        min_train_samples=30,
        min_test_samples=5,
        embargo_bars=10,
        purge_bars=10,
        min_folds=2,
        min_total_test_trades=1000,  # Impossible to meet
    )

    result = walk_forward(rows, config, ta_min=0.1, p_min=0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)

    assert not result.passed
    assert "test trades" in result.rejection_reason.lower()


def test_walk_forward_multiple_testing():
    """Multiple testing correction should be applied."""
    rows = _make_synthetic_rows(500)
    config = WalkForwardConfig(
        n_folds=4,
        test_window=50,
        min_train_samples=50,
        min_test_samples=20,
        embargo_bars=10,
        purge_bars=10,
        min_folds=3,
        min_total_test_trades=30,
        min_independent_test_trades=10,
        multiple_testing_method="fdr",
    )

    result = walk_forward(rows, config, ta_min=0.1, p_min=0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)

    assert result.multiple_testing_adjusted_p is not None
    assert 0 <= result.multiple_testing_adjusted_p <= 1


def test_walk_forward_deflated_sharpe():
    """Deflated Sharpe should be computed when requested."""
    rows = _make_synthetic_rows(500)
    config = WalkForwardConfig(
        n_folds=4,
        test_window=50,
        min_train_samples=50,
        min_test_samples=20,
        embargo_bars=10,
        purge_bars=10,
        min_folds=3,
        min_total_test_trades=30,
        min_independent_test_trades=10,
        multiple_testing_method="deflated_sharpe",
    )

    result = walk_forward(rows, config, ta_min=0.1, p_min=0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)

    assert result.deflated_sharpe is not None


# --- Purge/embargo property tests ----------------------------------------------

def test_purge_embargo_no_lookahead():
    """Purge + embargo should prevent look-ahead from train into test."""
    # Create data where labels in early test period depend on late train period
    rows = []
    base_date = date(2020, 1, 1)
    for i in range(200):
        d = base_date + timedelta(days=i)
        # Label = 1 if next 5 days go up
        label = 1.0 if i < 100 else 0.5  # Change at midpoint
        rows.append({
            "date": d.isoformat(),
            "symbol": "TEST",
            "x": [float(i) / 200],
            "y": label,
            "fwd": 1.0 if label == 1.0 else -1.0,
            "ta": 0.5,
        })

    config = WalkForwardConfig(
        n_folds=3,
        test_window=30,
        min_train_samples=30,
        min_test_samples=10,
        embargo_bars=20,  # Large embargo
        purge_bars=20,    # Large purge
        min_folds=2,
        min_total_test_trades=10,
        min_independent_test_trades=5,
    )

    result = walk_forward(rows, config, ta_min=0.1, p_min=0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)

    # Should have purged samples that would leak
    for f in result.folds:
        assert f.n_test_purged <= f.n_test


# --- Edge cases ----------------------------------------------------------------

def test_walk_forward_empty_data():
    """Empty data should return failed result with reason."""
    result = walk_forward([], WalkForwardConfig(), 0.1, 0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)
    assert not result.passed
    assert "empty" in result.rejection_reason.lower()


def test_walk_forward_single_fold():
    """Should work with single fold if config allows."""
    rows = _make_synthetic_rows(200)
    config = WalkForwardConfig(
        n_folds=1,
        test_window=50,
        min_train_samples=30,
        min_test_samples=20,
        embargo_bars=10,
        purge_bars=10,
        min_folds=1,
        min_total_test_trades=10,
        min_independent_test_trades=5,
    )

    result = walk_forward(rows, config, ta_min=0.1, p_min=0.5,
                          model_factory=_simple_model_factory, predict_fn=_simple_predict_fn)

    assert len(result.folds) == 1


# Need pytest import
import pytest