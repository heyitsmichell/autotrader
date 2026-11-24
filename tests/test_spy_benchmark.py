"""Tests for SPY benchmark."""

import pytest


class TestSPYBenchmark:
    """Test SPY benchmark functionality."""

    @pytest.mark.asyncio
    async def test_spy_benchmark_disabled(self, test_settings, mock_broker, test_db, test_engine):
        """SPY benchmark should be disabled when setting is off."""
        from app.supervisor import Supervisor
        from app.alerts import Alerts
        from dataclasses import replace

        disabled_settings = replace(test_settings, spy_benchmark_enabled=False)
        alerts = Alerts(disabled_settings, test_db)
        sup = Supervisor(disabled_settings, test_db, mock_broker, test_engine, test_engine.portfolio, alerts)

        result = await sup.check_spy_benchmark()
        assert result is None

    @pytest.mark.asyncio
    async def test_spy_benchmark_not_enough_trades(self, test_settings, mock_broker, test_db, test_engine):
        """SPY benchmark should not trigger with too few trades."""
        from app.supervisor import Supervisor
        from app.alerts import Alerts

        alerts = Alerts(test_settings, test_db)
        sup = Supervisor(test_settings, test_db, mock_broker, test_engine, test_engine.portfolio, alerts)

        result = await sup.check_spy_benchmark()
        assert result is None

    @pytest.mark.asyncio
    async def test_spy_benchmark_uses_per_trade_alpha(self, test_settings, mock_broker, test_db):
        """SPY benchmark should use per-trade alpha."""
        from app.learning import Learner

        learner = Learner(test_settings, mock_broker, test_db)
        stats = learner.stats()
        buys_vs_spy = stats.get("buys_vs_spy", {})

        # Check that avg_alpha_5d is present
        assert "avg_alpha_5d" in buys_vs_spy
        assert "alpha_hit_rate" in buys_vs_spy