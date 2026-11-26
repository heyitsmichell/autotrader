"""Tests for risk checks."""

import pytest
import os
import dataclasses
from pathlib import Path
from conftest import db_tmpdir


class TestRiskChecks:
    """Test risk checks in engine."""

    @pytest.mark.asyncio
    async def test_rejects_symbol_off_watchlist(self, test_engine, bullish_signal):
        """Should reject symbol off the watchlist."""
        r = await test_engine._on_proposal(
            test_engine.db.create_run("test"),
            dict(symbol="TSLA", side="BUY", quantity=1, limit_price=250,
                 confidence=0.5, rationale="test")
        )
        assert r.startswith("REJECTED")
        assert "watchlist" in r

    @pytest.mark.asyncio
    async def test_rejects_limit_price_far_from_market(self, test_engine, bullish_signal, mock_broker):
        """Should reject limit price far from market."""
        price = await mock_broker.last_price("AAPL")
        r = await test_engine._on_proposal(
            test_engine.db.create_run("test"),
            dict(symbol="AAPL", side="BUY", quantity=1, limit_price=round(price * 1.1, 2),
                 confidence=0.5, rationale="test")
        )
        assert r.startswith("REJECTED")
        assert "%" in r

    @pytest.mark.asyncio
    async def test_clamps_quantity_to_fit_order_limit(self, test_engine, bullish_signal, mock_broker):
        """Should clamp quantity to fit order limit."""
        price = await mock_broker.last_price("AAPL")
        r = await test_engine._on_proposal(
            test_engine.db.create_run("test"),
            dict(symbol="AAPL", side="BUY", quantity=40, limit_price=price,
                 confidence=0.5, rationale="test")
        )
        assert r.startswith("ACCEPTED")

    @pytest.mark.asyncio
    async def test_rejects_selling_shares_not_held(self, test_engine, bullish_signal, mock_broker):
        """Should reject selling shares not held."""
        price = await mock_broker.last_price("AAPL")
        r = await test_engine._on_proposal(
            test_engine.db.create_run("test"),
            dict(symbol="AAPL", side="SELL", quantity=1, limit_price=price,
                 confidence=0.5, rationale="test")
        )
        assert r.startswith("REJECTED")
        assert "no shorting" in r

    @pytest.mark.asyncio
    async def test_rejects_when_no_capacity_for_risk_limit(self, test_engine, bullish_signal, mock_broker):
        """Should reject when no capacity for risk limit."""
        price = await mock_broker.last_price("AAPL")
        from app.config import load_settings
        from app.engine import Engine
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.portfolio import Portfolio
        import tempfile
        from pathlib import Path

        with db_tmpdir() as tmp:
            tight_settings = load_settings()
            tight_settings = dataclasses.replace(tight_settings, max_risk_per_trade_usd=1)
            db = DB(Path(tmp) / "trader.db", tight_settings.broker_mode,
                    tight_settings.webull_environment, tight_settings.webull_account_id)
            broker = mock_broker
            sentiment = SentimentEngine(tight_settings, broker, db)
            ml = MLFilter(tight_settings, db)
            research = ResearchService(tight_settings, broker, db)
            signals = SignalService(tight_settings, broker, db, sentiment, ml, research)
            portfolio = Portfolio(tight_settings, db, broker)
            tight_engine = Engine(tight_settings, db, broker, signals, portfolio)
            # Override signals.ensure to return bullish signal (async function)
            async def agree(sym: str, max_age_minutes: int = 20, allow_stale: bool = False):
                return {"id": 1, "symbol": sym, "close": 150.0, "atr": 3.0, "trend": "up",
                        "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6, "features": {}}
            tight_engine.signals.ensure = agree

            r = await tight_engine._on_proposal(
                db.create_run("test"),
                dict(symbol="AAPL", side="BUY", quantity=2, limit_price=price,
                     confidence=0.5, rationale="test")
            )
            assert r.startswith("REJECTED")
            assert "no capacity" in r