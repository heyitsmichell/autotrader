"""Tests for kill switch."""

import pytest
from pathlib import Path
from conftest import db_tmpdir


class TestKillSwitch:
    """Test kill switch functionality."""

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_proposals(self, test_engine, bullish_signal, mock_broker):
        """Kill switch should block proposals."""
        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        await test_engine.set_kill_switch(True)
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=1, limit_price=price,
                 confidence=0.5, rationale="test")
        )
        assert r.startswith("REJECTED")
        assert "kill switch" in r

    @pytest.mark.asyncio
    async def test_kill_switch_survives_restart(self, test_engine, bullish_signal, mock_broker):
        """Kill switch should survive a restart."""
        from app.engine import Engine
        from app.config import load_settings
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.portfolio import Portfolio
        import tempfile

        with db_tmpdir() as tmp:
            settings = load_settings()
            db = DB(Path(tmp) / "trader.db", settings.broker_mode,
                    settings.webull_environment, settings.webull_account_id)
            # Set the kill switch state in the DB
            db.set_kv("kill_switch", {"on": True, "reason": "test reason", "at": "2024-01-01T00:00:00"})

            broker = mock_broker
            sentiment = SentimentEngine(settings, broker, db)
            ml = MLFilter(settings, db)
            research = ResearchService(settings, broker, db)
            signals = SignalService(settings, broker, db, sentiment, ml, research)
            portfolio = Portfolio(settings, db, broker)
            reborn = Engine(settings, db, broker, signals, portfolio)

            assert reborn.kill_switch is True
            assert reborn.kill_reason == "test reason"

            # Now update the same DB to turn off kill switch
            db.set_kv("kill_switch", {"on": False, "reason": "", "at": ""})
            # Create another engine instance with the same DB
            reborn2 = Engine(settings, db, broker, signals, portfolio)
            assert not reborn2.kill_switch

    @pytest.mark.asyncio
    async def test_trips_after_3_failed_runs(self, test_engine, test_settings, mock_broker, test_db):
        """Should trip after 3 failed runs in a row."""
        from app.supervisor import Supervisor
        from app.alerts import Alerts

        alerts = Alerts(test_settings, test_db)
        sup = Supervisor(test_settings, test_db, mock_broker, test_engine, test_engine.portfolio, alerts)

        test_engine.run_failures = 3
        test_engine.last_failure = "Groq API returned HTTP 500"
        r = await sup.check_kill_triggers()
        assert test_engine.kill_switch is True
        assert r is not None
        assert "3 failed AI runs" in r

    @pytest.mark.asyncio
    async def test_release_resets_failure_count(self, test_engine, test_settings, mock_broker, test_db):
        """Release should reset the failure count."""
        from app.supervisor import Supervisor
        from app.alerts import Alerts

        alerts = Alerts(test_settings, test_db)
        sup = Supervisor(test_settings, test_db, mock_broker, test_engine, test_engine.portfolio, alerts)

        test_engine.run_failures = 3
        test_engine.last_failure = "Groq API returned HTTP 500"
        await sup.check_kill_triggers()

        sup.on_release()
        await test_engine.set_kill_switch(False)
        assert test_engine.run_failures == 0
        assert not test_engine.kill_switch

    @pytest.mark.asyncio
    async def test_drawdown_trips_at_limit(self, test_engine, test_settings, mock_broker, test_db):
        """Should trip at drawdown limit."""
        from app.supervisor import Supervisor
        from app.alerts import Alerts

        alerts = Alerts(test_settings, test_db)
        sup = Supervisor(test_settings, test_db, mock_broker, test_engine, test_engine.portfolio, alerts)

        # Peak recorded
        assert sup._money_trigger({"net_liq": 10_000.0}) is None
        # 8% drawdown under 10% limit
        assert sup._money_trigger({"net_liq": 9_200.0}) is None
        # 10.5% drawdown trips
        r = sup._money_trigger({"net_liq": 8_950.0})
        assert r is not None
        assert "drawdown" in r

    @pytest.mark.asyncio
    async def test_daily_loss_trips(self, test_engine, test_settings, mock_broker, test_db):
        """Should trip on daily loss."""
        from dataclasses import replace
        from app.supervisor import Supervisor
        from app.alerts import Alerts

        s_loss = replace(test_settings, kill_daily_loss_usd=100.0)
        alerts = Alerts(s_loss, test_db)
        sup = Supervisor(s_loss, test_db, mock_broker, test_engine, test_engine.portfolio, alerts)

        test_db.set_kv("daily_loss_ack", None)
        r = sup._money_trigger({"net_liq": 9_000.0, "day_pnl": -150.0})
        assert r is not None
        assert "daily loss" in r

        test_engine.kill_reason = f"automatic: {r}"   # on_release acks only the trigger that tripped
        sup.on_release()
        assert sup._money_trigger({"net_liq": 9_000.0, "day_pnl": -150.0}) is None