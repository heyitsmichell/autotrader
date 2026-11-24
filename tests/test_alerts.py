"""Tests for alerts."""

import pytest
import dataclasses
import asyncio


class TestAlerts:
    """Test alert system."""

    @pytest.mark.asyncio
    async def test_errors_and_marked_lines_sent(self, test_settings, test_db):
        """Errors and marked lines should be sent."""
        from app.alerts import Alerts

        s_alert = dataclasses.replace(test_settings, alert_ntfy_topic="smoke-test-topic",
                                      alert_dedupe_minutes=30)
        db_alert = test_db  # Use same DB
        al = Alerts(s_alert, db_alert)
        sent = []

        async def fake_ntfy(title, body, level):
            sent.append((title, body))
        al._ntfy = fake_ntfy

        db_alert.log("error", "something broke")
        db_alert.log("info", "routine message")
        db_alert.log("info", "order placed", alert=True)
        db_alert.log("error", "something broke")  # duplicate
        await asyncio.sleep(0.05)

        titles = [t for t, _ in sent]
        assert titles == ["Trader ERROR", "Trader"]
        assert al.suppressed == 1

    @pytest.mark.asyncio
    async def test_alerts_name_broker_environment(self, test_settings, test_db):
        """Alerts should name the broker environment."""
        from app.alerts import Alerts

        s_alert = dataclasses.replace(test_settings, alert_ntfy_topic="smoke-test-topic",
                                      alert_dedupe_minutes=30)
        db_alert = test_db
        al = Alerts(s_alert, db_alert)
        sent = []

        async def fake_ntfy(title, body, level):
            sent.append((title, body))
        al._ntfy = fake_ntfy

        db_alert.log("error", "test error", alert=True)
        await asyncio.sleep(0.05)

        assert len(sent) == 1
        assert "Mock broker" in sent[0][1]

    @pytest.mark.asyncio
    async def test_daily_summary_text(self, test_settings, mock_broker, test_db, test_engine):
        """Daily summary text should be generated."""
        from app.supervisor import Supervisor
        from app.alerts import Alerts

        alerts = Alerts(test_settings, test_db)
        sup = Supervisor(test_settings, test_db, mock_broker, test_engine,
                         test_engine.portfolio, alerts)
        summary = await sup.summary_text()
        assert "Net liquidation" in summary

    @pytest.mark.asyncio
    async def test_alert_secrets_hidden_from_settings_repr(self, test_settings):
        """Alert secrets should be hidden from settings repr."""
        assert "smoke-test-topic" not in repr(test_settings)