"""Tests for signals (TA + sentiment + ML)."""

import pytest
from pathlib import Path
from conftest import db_tmpdir


class TestSignals:
    """Test signal generation and consensus."""

    @pytest.mark.asyncio
    async def test_signal_per_watchlist_symbol(self, test_settings, mock_broker, test_db):
        """Should have a signal per watchlist symbol."""
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService

        sentiment = SentimentEngine(test_settings, mock_broker, test_db)
        ml = MLFilter(test_settings, test_db)
        research = ResearchService(test_settings, mock_broker, test_db)
        signals = SignalService(test_settings, mock_broker, test_db, sentiment, ml, research)
        sigs = await signals.refresh()
        # Should have one signal per symbol in the watchlist
        assert len(sigs) == len(test_settings.watchlist)

    @pytest.mark.asyncio
    async def test_ta_score_in_range(self, test_settings, mock_broker):
        """TA score should be in range [-1, 1]."""
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService

        import tempfile
        with db_tmpdir() as tmp:
            db = DB(Path(tmp) / "trader.db", test_settings.broker_mode,
                    test_settings.webull_environment, test_settings.webull_account_id)
            sentiment = SentimentEngine(test_settings, mock_broker, db)
            ml = MLFilter(test_settings, db)
            research = ResearchService(test_settings, mock_broker, db)
            signals = SignalService(test_settings, mock_broker, db, sentiment, ml, research)
            sigs = await signals.refresh()
            for sig in sigs:
                assert sig.get("ta_score") is not None
                assert -1 <= sig["ta_score"] <= 1

    @pytest.mark.asyncio
    async def test_trend_regime_set(self, test_settings, mock_broker):
        """Trend regime should be set."""
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService

        import tempfile
        with db_tmpdir() as tmp:
            db = DB(Path(tmp) / "trader.db", test_settings.broker_mode,
                    test_settings.webull_environment, test_settings.webull_account_id)
            sentiment = SentimentEngine(test_settings, mock_broker, db)
            ml = MLFilter(test_settings, db)
            research = ResearchService(test_settings, mock_broker, db)
            signals = SignalService(test_settings, mock_broker, db, sentiment, ml, research)
            sigs = await signals.refresh()
            for sig in sigs:
                assert sig.get("trend") in ("up", "down", "range")

    @pytest.mark.asyncio
    async def test_score_components_recorded(self, test_settings, mock_broker):
        """Score components should be recorded."""
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService

        import tempfile
        with db_tmpdir() as tmp:
            db = DB(Path(tmp) / "trader.db", test_settings.broker_mode,
                    test_settings.webull_environment, test_settings.webull_account_id)
            sentiment = SentimentEngine(test_settings, mock_broker, db)
            ml = MLFilter(test_settings, db)
            research = ResearchService(test_settings, mock_broker, db)
            signals = SignalService(test_settings, mock_broker, db, sentiment, ml, research)
            sigs = await signals.refresh()
            for sig in sigs:
                assert isinstance(sig.get("ta_components"), list)
                assert len(sig["ta_components"]) >= 4

    @pytest.mark.asyncio
    async def test_webull_sentiment_scored(self, test_settings, mock_broker):
        """Webull sentiment should be scored."""
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService

        import tempfile
        with db_tmpdir() as tmp:
            db = DB(Path(tmp) / "trader.db", test_settings.broker_mode,
                    test_settings.webull_environment, test_settings.webull_account_id)
            sentiment = SentimentEngine(test_settings, mock_broker, db)
            ml = MLFilter(test_settings, db)
            research = ResearchService(test_settings, mock_broker, db)
            signals = SignalService(test_settings, mock_broker, db, sentiment, ml, research)
            sigs = await signals.refresh()
            for sig in sigs:
                assert sig.get("sent_n", 0) > 0
                assert sig.get("sent_webull") is not None

    @pytest.mark.asyncio
    async def test_gate_verdict_and_atr_levels_set(self, test_settings, mock_broker):
        """Gate verdict and ATR levels should be set."""
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService

        import tempfile
        with db_tmpdir() as tmp:
            db = DB(Path(tmp) / "trader.db", test_settings.broker_mode,
                    test_settings.webull_environment, test_settings.webull_account_id)
            sentiment = SentimentEngine(test_settings, mock_broker, db)
            ml = MLFilter(test_settings, db)
            research = ResearchService(test_settings, mock_broker, db)
            signals = SignalService(test_settings, mock_broker, db, sentiment, ml, research)
            sigs = await signals.refresh()
            for sig in sigs:
                assert sig.get("consensus") in ("long", "blocked")
                assert sig.get("sl") is not None
                assert sig.get("tp") is not None

    @pytest.mark.asyncio
    async def test_get_signals_text_for_groq(self, test_settings, mock_broker):
        """get_signals text should be available for Groq."""
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService

        import tempfile
        with db_tmpdir() as tmp:
            db = DB(Path(tmp) / "trader.db", test_settings.broker_mode,
                    test_settings.webull_environment, test_settings.webull_account_id)
            sentiment = SentimentEngine(test_settings, mock_broker, db)
            ml = MLFilter(test_settings, db)
            research = ResearchService(test_settings, mock_broker, db)
            signals = SignalService(test_settings, mock_broker, db, sentiment, ml, research)
            await signals.refresh()
            desc = signals.describe()
            assert "Consensus for a BUY now" in desc

@pytest.mark.asyncio
async def test_refresh_with_market_regime_and_relative_strength(test_settings, mock_broker, test_db):
    """Regression (2026-11-25): with the regime filter / relative strength on, refresh() raised
    KeyError: -1 because indicators.features() returns a dict, not a list."""
    import dataclasses
    from app.signals import SignalService
    from app.sentiment import SentimentEngine
    from app.ml import MLFilter
    from app.research import ResearchService

    s = dataclasses.replace(test_settings, market_regime_filter_enabled=True,
                            relative_strength_enabled=True)
    svc = SignalService(s, mock_broker, test_db, SentimentEngine(s, mock_broker, test_db),
                        MLFilter(s, test_db), ResearchService(s, mock_broker, test_db))
    await svc.refresh()          # first pass fills the bars cache (SPY included)
    sigs = await svc.refresh()   # second pass takes the cached-benchmark branch
    assert len(sigs) == len(s.watchlist)
