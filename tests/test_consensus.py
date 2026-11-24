"""Tests for consensus gate."""

import pytest


class TestConsensusGate:
    """Test consensus gate logic."""

    @pytest.fixture
    def base_proposal(self):
        """Base proposal for testing."""
        return {"side": "BUY", "limit_price": 100.0}

    @pytest.fixture
    def bullish_signal(self):
        """A signal where every engine agrees."""
        return {"id": None, "symbol": "X", "close": 100.0, "atr": 2.0, "trend": "up",
                "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": None, "features": {}}

    @pytest.mark.asyncio
    async def test_passes_when_ta_news_and_model_agree(self, base_proposal, bullish_signal, test_settings):
        """Should pass when TA, news and model agree."""
        from app import consensus
        result = consensus.evaluate(base_proposal, bullish_signal, test_settings)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_blocks_weak_ta(self, base_proposal, bullish_signal, test_settings):
        """Should block weak TA."""
        from app import consensus
        weak_ta = {**bullish_signal, "ta_score": 0.0}
        result = consensus.evaluate(base_proposal, weak_ta, test_settings)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_blocks_downtrend(self, base_proposal, bullish_signal, test_settings):
        """Should block downtrend."""
        from app import consensus
        downtrend = {**bullish_signal, "trend": "down"}
        result = consensus.evaluate(base_proposal, downtrend, test_settings)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_blocks_negative_news(self, base_proposal, bullish_signal, test_settings):
        """Should block negative news."""
        from app import consensus
        negative_news = {**bullish_signal, "sent_score": -0.6}
        result = consensus.evaluate(base_proposal, negative_news, test_settings)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_too_few_headlines_counts_as_neutral(self, base_proposal, bullish_signal, test_settings):
        """Too few headlines should count as neutral."""
        from app import consensus
        few_headlines = {**bullish_signal, "sent_score": -0.9, "sent_n": 1}
        result = consensus.evaluate(base_proposal, few_headlines, test_settings)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_blocks_low_model_odds(self, base_proposal, bullish_signal, test_settings):
        """Should block low model odds."""
        from app import consensus
        low_ml = {**bullish_signal, "ml_p": 0.4}
        result = consensus.evaluate(base_proposal, low_ml, test_settings)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_never_blocks_sell(self, test_settings):
        """Should never block a SELL."""
        from app import consensus
        sell_proposal = {"side": "SELL", "limit_price": 100}
        result = consensus.evaluate(sell_proposal, None, test_settings)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_stop_below_and_target_above_entry(self, base_proposal, bullish_signal, test_settings):
        """Stop should be below and target above the entry."""
        from app import consensus
        result = consensus.evaluate(base_proposal, bullish_signal, test_settings)
        assert result["sl"] < 100 < result["tp"]