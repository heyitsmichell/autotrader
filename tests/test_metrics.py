"""Pure performance math tests (Phase 0)."""

from __future__ import annotations

import math

import pytest

from app import metrics


def test_daily_returns_removes_deposits():
    # Day 2 includes a $10 deposit: the account did not actually gain on the day.
    values = [100.0, 110.0, 121.0]
    flows = [0.0, 10.0, 0.0]
    rets = metrics.daily_returns(values, flows)
    assert rets == pytest.approx([0.0, 0.10])
    assert metrics.total_return(values, flows) == pytest.approx(0.10)


def test_returns_from_prices_and_total_return():
    prices = [100.0, 105.0, 110.25]
    rets = metrics.returns_from_prices(prices)
    assert rets == pytest.approx([0.05, 0.05])
    assert metrics.total_return(prices) == pytest.approx(0.1025)


def test_annualized_return():
    # A 1% monthly gain compounded 12 times is 12.68% for the year.
    rets = [0.01] * 12
    assert metrics.annualized_return(rets, periods=12) == pytest.approx(1.01 ** 12 - 1)
    assert metrics.annualized_return([]) == 0.0


def test_volatility_and_sharpe():
    rets = [0.01, -0.01, 0.02, 0.0]
    vol = metrics.volatility(rets)
    expected = statistics_stdev(rets) * math.sqrt(252)
    assert vol == pytest.approx(expected)
    assert metrics.volatility([0.01]) is None
    assert metrics.sharpe([0.01, 0.01, 0.01]) is None  # zero variance


def test_max_drawdown_and_series():
    values = [100.0, 120.0, 90.0, 110.0]
    assert metrics.max_drawdown(values) == pytest.approx(-0.25)
    assert metrics.drawdown_series(values) == pytest.approx([0.0, 0.0, -0.25, -1 / 12])
    assert metrics.max_drawdown([100.0, 110.0]) == 0.0


def test_beta_alpha_tracking_error_and_information_ratio():
    bench = [0.01, -0.005, 0.02, 0.0, -0.01]
    asset = [0.02, -0.010, 0.04, 0.0, -0.020]
    assert metrics.beta(asset, bench) == pytest.approx(2.0)
    assert metrics.alpha(asset, bench) == pytest.approx(0.0, abs=1e-12)

    excess = [a - b for a, b in zip(asset, bench)]
    te = metrics.tracking_error(excess)
    ir = metrics.information_ratio(excess)
    assert te is not None and te > 0
    assert ir is not None
    assert ir == pytest.approx(metrics_hypothetical_ir(excess))
    assert metrics.hit_rate([0.01, -0.01, 0.0, 0.02]) == pytest.approx(0.5)
    assert metrics.beta([0.01], [0.01]) is None


def test_turnover():
    notional = [100.0, 200.0]
    assert metrics.turnover(notional, 1000.0) == pytest.approx(150.0 / 1000.0 * 252)
    assert metrics.turnover(notional, [1000.0, 1000.0]) == pytest.approx(0.15 * 252)
    assert metrics.turnover([], 1000.0) is None
    assert metrics.turnover([100.0], 0.0) is None


def test_newey_west_se_positive_and_lag_rule():
    values = [0.01, -0.005, 0.02, -0.01, 0.0] * 20
    se = metrics.newey_west_se(values)
    assert se is not None and se > 0
    assert metrics.newey_west_lags(100) >= 1
    assert metrics.newey_west_se([0.01]) is None


def test_significance_reports_not_enough_data_below_window():
    result = metrics.significance([0.001] * 20, min_days=60)
    assert result["state"] == "insufficient_data"
    assert result["n"] == 20
    assert result["days_needed"] == 40
    assert "p_value" not in result


def test_significance_detects_a_clear_excess_return():
    # Small positive mean with modest noise over 120 days.
    values = [0.001 if i % 2 == 0 else 0.0005 for i in range(120)]
    result = metrics.significance(values, min_days=60)
    assert result["state"] == "ok"
    assert result["p_value"] is not None and result["p_value"] < 0.05
    assert result["significant"] is True
    assert result["ci_low_annualized"] <= result["mean_annualized"] <= result["ci_high_annualized"]


def test_significance_does_not_call_noise_an_edge():
    values = [0.002, -0.002] * 60
    result = metrics.significance(values, min_days=60)
    assert result["state"] == "ok"
    assert result["p_value"] is not None and result["p_value"] > 0.05
    assert result["significant"] is False


def test_summarize_uses_time_weighted_account_returns():
    values = [1000.0, 1010.0, 1111.0]   # +1%, then +10% with no deposit
    spy = [100.0, 101.0, 102.01]        # +1%, +1%
    out = metrics.summarize(values, None, spy, min_days=2)
    assert out["account"]["total_return"] == pytest.approx(0.111)
    assert out["benchmark"]["total_return"] == pytest.approx(0.0201)
    assert out["excess_return"] == pytest.approx(0.0909)
    assert out["days"] == 2


# -- local helpers -----------------------------------------------------------
def statistics_stdev(values):
    import statistics
    return statistics.stdev(values)


def metrics_hypothetical_ir(excess):
    import statistics
    return statistics.fmean(excess) / statistics.stdev(excess) * math.sqrt(252)
