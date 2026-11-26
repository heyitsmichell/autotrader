"""Tests for entry eligibility checks."""

import pytest
from datetime import datetime, timezone
from dataclasses import replace


class TestEligibility:
    """Test entry eligibility checks."""

    @pytest.fixture
    def eligibility_settings(self, test_settings):
        """Settings with higher gap threshold to accommodate mock broker's synthetic drift."""
        return replace(test_settings, eligibility_max_gap_pct=10.0)

    @pytest.mark.asyncio
    async def test_eligibility_check_passes_for_valid_buy(self, eligibility_settings, mock_broker, test_db):
        """Eligibility check should pass for valid BUY."""
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.eligibility import check_eligibility

        sentiment = SentimentEngine(eligibility_settings, mock_broker, test_db)
        ml = MLFilter(eligibility_settings, test_db)
        research = ResearchService(eligibility_settings, mock_broker, test_db)
        signals = SignalService(eligibility_settings, mock_broker, test_db, sentiment, ml, research)

        # Get live price from mock broker to ensure consistency with gap check
        last_price = await mock_broker.last_price("AAPL")

        # Create a valid signal using the mock broker's actual price
        signal = {"id": 1, "symbol": "AAPL", "close": last_price, "atr": 3.0, "trend": "up",
                  "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6,
                  "features": {}, "time": datetime.now(timezone.utc).isoformat()}

        result = await check_eligibility(eligibility_settings, mock_broker, signals, "AAPL", "BUY", last_price, signal, test_db)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_eligibility_disabled_allows_all(self, test_settings, mock_broker, test_db):
        """Eligibility disabled should allow all trades."""
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.eligibility import check_eligibility
        from dataclasses import replace

        disabled_settings = replace(test_settings, eligibility_enabled=False)

        sentiment = SentimentEngine(disabled_settings, mock_broker, test_db)
        ml = MLFilter(disabled_settings, test_db)
        research = ResearchService(disabled_settings, mock_broker, test_db)
        signals = SignalService(disabled_settings, mock_broker, test_db, sentiment, ml, research)

        signal = None  # No signal needed when disabled
        result = await check_eligibility(disabled_settings, mock_broker, signals, "AAPL", "BUY", 1.0, signal, test_db)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_sell_orders_bypass_eligibility(self, test_settings, mock_broker, test_db):
        """SELL orders should bypass eligibility checks."""
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.eligibility import check_eligibility

        sentiment = SentimentEngine(test_settings, mock_broker, test_db)
        ml = MLFilter(test_settings, test_db)
        research = ResearchService(test_settings, mock_broker, test_db)
        signals = SignalService(test_settings, mock_broker, test_db, sentiment, ml, research)

        result = await check_eligibility(test_settings, mock_broker, signals, "AAPL", "SELL", 150.0, None, test_db)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_min_price_check(self, eligibility_settings, mock_broker, test_db):
        """Min price check should reject below threshold."""
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.eligibility import check_eligibility
        from dataclasses import replace

        low_price_settings = replace(eligibility_settings, eligibility_min_price=1000.0)  # Set very high to ensure rejection

        sentiment = SentimentEngine(low_price_settings, mock_broker, test_db)
        ml = MLFilter(low_price_settings, test_db)
        research = ResearchService(low_price_settings, mock_broker, test_db)
        signals = SignalService(low_price_settings, mock_broker, test_db, sentiment, ml, research)

        # Get live price from mock broker
        last_price = await mock_broker.last_price("AAPL")

        signal = {"id": 1, "symbol": "AAPL", "close": last_price, "atr": 1.0, "trend": "up",
                  "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6,
                  "features": {}, "time": datetime.now(timezone.utc).isoformat()}

        result = await check_eligibility(low_price_settings, mock_broker, signals, "AAPL", "BUY", last_price, signal, test_db)
        assert result.ok is False
        assert "Min price" in result.reason

# ---------------------------------------------------------------- earnings blackout (QA M8)
class TestEarningsBlackout:
    @staticmethod
    def _checker(settings, provider, mode="webull"):
        from app.eligibility import EligibilityChecker
        s = replace(settings, broker_mode=mode, eligibility_enabled=True,
                    eligibility_earnings_blackout_days=3)
        c = EligibilityChecker(s, None, None)
        c.earnings_provider = provider
        return c

    def test_blackout_default_is_three_days_and_fail_closed(self, test_settings):
        assert test_settings.eligibility_earnings_blackout_days == 3
        assert test_settings.eligibility_earnings_fail_closed is True

    @pytest.mark.asyncio
    async def test_earnings_inside_window_blocks(self, test_settings):
        from datetime import timedelta
        from app.market import now_et
        soon = (now_et().date() + timedelta(days=2)).isoformat()
        r = await self._checker(test_settings, lambda sym: [soon])._check_earnings("AAPL")
        assert r.ok is False and "Earnings blackout" in r.reason

    @pytest.mark.asyncio
    async def test_earnings_outside_window_or_none_scheduled_passes(self, test_settings):
        from datetime import timedelta
        from app.market import now_et
        far = (now_et().date() + timedelta(days=10)).isoformat()
        assert (await self._checker(test_settings, lambda s: [far])._check_earnings("AAPL")).ok
        assert (await self._checker(test_settings, lambda s: [])._check_earnings("AAPL")).ok

    @staticmethod
    def _boom(sym):
        raise RuntimeError("down")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider,mode,fail_closed,ok,msg", [
        (lambda s: None, "webull", True, False, "calendar unavailable"),   # missing calendar
        ("boom", "webull", True, False, None),                             # provider exception
        (lambda s: None, "webull", False, True, None),                     # fail-closed switched off
        (lambda s: None, "mock", True, True, None),                        # mock: no real calendar
    ])
    async def test_earnings_calendar_failure_modes(self, test_settings, provider, mode,
                                                   fail_closed, ok, msg):
        c = self._checker(test_settings, self._boom if provider == "boom" else provider, mode=mode)
        c.s = replace(c.s, eligibility_earnings_fail_closed=fail_closed)
        r = await c._check_earnings("AAPL")
        assert r.ok is ok
        if msg:
            assert msg in r.reason

    @pytest.mark.asyncio
    async def test_sells_are_never_blocked_by_the_calendar(self, test_settings):
        c = self._checker(test_settings, lambda s: None)
        assert (await c.check("AAPL", "SELL", 100.0)).ok is True
