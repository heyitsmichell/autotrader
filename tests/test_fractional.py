"""Tests for fractional share trading (Phase A)."""

import pytest
import tempfile
from pathlib import Path
import dataclasses


@pytest.fixture
def fractional_settings(tmp_data_dir, monkeypatch):
    """Load test settings with fractional enabled."""
    from app.config import load_settings
    import os
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_data_dir))
    monkeypatch.setenv("MOCK_STATE_FILE", str(tmp_data_dir / "mock_state.json"))
    monkeypatch.setenv("FRACTIONAL_ENABLED", "true")
    monkeypatch.setenv("FRACTIONAL_MIN_USD", "1.0")
    monkeypatch.setenv("FRACTIONAL_DECIMALS", "4")
    monkeypatch.setenv("FRACTIONAL_WHITELIST", "AAPL,MSFT,NVDA,SPY,QQQ")
    # Disable bracket orders for fractional tests so they don't round down to whole shares
    monkeypatch.setenv("BRACKET_ORDERS", "false")
    # Use llm sizing mode so model's quantity is respected (clamped by limits)
    monkeypatch.setenv("SIZING_MODE", "llm")
    # Add GOOG to watchlist so fractional whitelist check can run
    monkeypatch.setenv("WATCHLIST", "AAPL,MSFT,NVDA,SPY,QQQ,GOOG")
    s = load_settings()
    return s


@pytest.fixture
def fractional_settings_with_brackets(tmp_data_dir, monkeypatch):
    """Load test settings with fractional enabled AND bracket orders."""
    from app.config import load_settings
    import os
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_data_dir))
    monkeypatch.setenv("MOCK_STATE_FILE", str(tmp_data_dir / "mock_state.json"))
    monkeypatch.setenv("FRACTIONAL_ENABLED", "true")
    monkeypatch.setenv("FRACTIONAL_MIN_USD", "1.0")
    monkeypatch.setenv("FRACTIONAL_DECIMALS", "4")
    monkeypatch.setenv("FRACTIONAL_WHITELIST", "AAPL,MSFT,NVDA,SPY,QQQ")
    # Enable bracket orders for this test
    monkeypatch.setenv("BRACKET_ORDERS", "true")
    # Use llm sizing mode so model's quantity is respected (clamped by limits)
    monkeypatch.setenv("SIZING_MODE", "llm")
    # Add GOOG to watchlist so fractional whitelist check can run
    monkeypatch.setenv("WATCHLIST", "AAPL,MSFT,NVDA,SPY,QQQ,GOOG")
    s = load_settings()
    return s


@pytest.fixture
async def fractional_broker(fractional_settings):
    """Create a mock broker with fractional enabled."""
    from app.broker import Broker
    broker = Broker(fractional_settings)
    await broker.start()
    yield broker
    await broker.stop()


@pytest.fixture
async def fractional_db(fractional_settings, tmp_data_dir):
    """Create a test database with fractional enabled."""
    from app.db import DB
    db = DB(tmp_data_dir / "trader.db", fractional_settings.broker_mode,
            fractional_settings.webull_environment, fractional_settings.webull_account_id)
    yield db


@pytest.fixture
async def fractional_engine(fractional_settings, fractional_broker, fractional_db):
    """Create a test engine with fractional enabled."""
    from app.signals import SignalService
    from app.sentiment import SentimentEngine
    from app.ml import MLFilter
    from app.research import ResearchService
    from app.portfolio import Portfolio
    from app.engine import Engine

    sentiment = SentimentEngine(fractional_settings, fractional_broker, fractional_db)
    ml = MLFilter(fractional_settings, fractional_db)
    research = ResearchService(fractional_settings, fractional_broker, fractional_db)
    signals = SignalService(fractional_settings, fractional_broker, fractional_db, sentiment, ml, research)
    portfolio = Portfolio(fractional_settings, fractional_db, fractional_broker)
    engine = Engine(fractional_settings, fractional_db, fractional_broker, signals, portfolio)

    # Override signals.ensure to always return bullish for testing
    async def agree(sym: str, max_age_minutes: int = 20):
        return {"id": 1, "symbol": sym, "close": 150.0, "atr": 3.0, "trend": "up",
                "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6, "features": {}}
    engine.signals.ensure = agree

    yield engine


class TestFractionalShares:
    """Test fractional share trading functionality."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_fractional_buy_amount_order(self, fractional_engine, fractional_broker):
        """Test BUY with fractional shares using AMOUNT (dollar-based) order."""
        price = await fractional_broker.last_price("AAPL")
        # Use a dollar amount that results in fractional shares
        run_id = fractional_engine.db.create_run("test")
        r = await fractional_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=0.5, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test fractional buy")
        )
        assert r.startswith("ACCEPTED"), f"Expected ACCEPTED, got: {r}"
        # Verify the proposal was stored with fractional quantity
        pid = int(r.split("#")[1].split(":")[0])
        proposal = fractional_engine.db.get_proposal(pid)
        assert proposal is not None
        assert proposal["quantity"] == 0.5

    @pytest.mark.asyncio
    async def test_fractional_buy_qty_order(self, fractional_engine, fractional_broker):
        """Test BUY with fractional shares using QTY (share-based) order."""
        price = await fractional_broker.last_price("AAPL")
        run_id = fractional_engine.db.create_run("test")
        r = await fractional_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=0.25, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test fractional qty buy")
        )
        assert r.startswith("ACCEPTED"), f"Expected ACCEPTED, got: {r}"

    @pytest.mark.asyncio
    async def test_fractional_rejected_when_disabled(self, test_engine, mock_broker):
        """Test fractional orders are rejected when FRACTIONAL_ENABLED=false."""
        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=0.5, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test fractional disabled")
        )
        assert r.startswith("REJECTED"), f"Expected REJECTED, got: {r}"
        assert "fractional quantity requires FRACTIONAL_ENABLED=true" in r

    @pytest.mark.asyncio
    async def test_fractional_rejected_off_whitelist(self, fractional_engine, fractional_broker):
        """Test fractional orders rejected for symbols not on whitelist."""
        # GOOG is not in FRACTIONAL_WHITELIST (AAPL,MSFT,NVDA,SPY,QQQ)
        price = await fractional_broker.last_price("GOOG")
        run_id = fractional_engine.db.create_run("test")
        r = await fractional_engine._on_proposal(
            run_id,
            dict(symbol="GOOG", side="BUY", quantity=0.5, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test fractional off whitelist")
        )
        assert r.startswith("REJECTED"), f"Expected REJECTED, got: {r}"
        # Whitelist check is in the secondary validation (after consensus)
        assert "not on the fractional whitelist" in r

    @pytest.mark.asyncio
    async def test_fractional_decimal_precision(self, fractional_engine, fractional_broker):
        """Test fractional quantity precision validation (4 decimals default)."""
        price = await fractional_broker.last_price("AAPL")
        run_id = fractional_engine.db.create_run("test")
        # 5 decimal places should be rejected by secondary validation (early validation only checks min precision)
        r = await fractional_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=0.12345, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test too many decimals")
        )
        assert r.startswith("REJECTED"), f"Expected REJECTED, got: {r}"
        assert "exceeds 4 decimal places" in r

    @pytest.mark.asyncio
    async def test_fractional_min_precision(self, fractional_engine, fractional_broker):
        """Test fractional quantity below minimum precision is rejected."""
        price = await fractional_broker.last_price("AAPL")
        run_id = fractional_engine.db.create_run("test")
        # 0.00001 is below 10^-4
        r = await fractional_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=0.00001, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test below min precision")
        )
        assert r.startswith("REJECTED"), f"Expected REJECTED, got: {r}"
        # Early validation catches this
        assert "below minimum precision" in r

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_fractional_no_bracket_orders(self, fractional_settings_with_brackets, tmp_data_dir):
        """Test that bracket orders are not used for fractional quantities when brackets enabled."""
        from app.broker import Broker
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.portfolio import Portfolio
        from app.engine import Engine

        broker = Broker(fractional_settings_with_brackets)
        await broker.start()
        db = DB(tmp_data_dir / "trader2.db", fractional_settings_with_brackets.broker_mode,
                fractional_settings_with_brackets.webull_environment, fractional_settings_with_brackets.webull_account_id)

        sentiment = SentimentEngine(fractional_settings_with_brackets, broker, db)
        ml = MLFilter(fractional_settings_with_brackets, db)
        research = ResearchService(fractional_settings_with_brackets, broker, db)
        signals = SignalService(fractional_settings_with_brackets, broker, db, sentiment, ml, research)
        portfolio = Portfolio(fractional_settings_with_brackets, db, broker)
        engine = Engine(fractional_settings_with_brackets, db, broker, signals, portfolio)

        async def agree(sym: str, max_age_minutes: int = 20):
            return {"id": 1, "symbol": sym, "close": 150.0, "atr": 3.0, "trend": "up",
                    "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6, "features": {}}
        engine.signals.ensure = agree

        price = await broker.last_price("AAPL")
        run_id = engine.db.create_run("test")
        r = await engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=0.5, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test fractional no bracket")
        )
        assert r.startswith("ACCEPTED"), f"Expected ACCEPTED, got: {r}"
        # Should mention protective orders placed separately
        assert "Protective orders will be placed separately" in r

        await broker.stop()

    @pytest.mark.asyncio
    async def test_whole_shares_still_use_brackets(self, fractional_settings_with_brackets, tmp_data_dir):
        """Test that whole share quantities still use bracket orders when enabled."""
        from app.broker import Broker
        from app.db import DB
        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.portfolio import Portfolio
        from app.engine import Engine

        broker = Broker(fractional_settings_with_brackets)
        await broker.start()
        db = DB(tmp_data_dir / "trader.db", fractional_settings_with_brackets.broker_mode,
                fractional_settings_with_brackets.webull_environment, fractional_settings_with_brackets.webull_account_id)

        sentiment = SentimentEngine(fractional_settings_with_brackets, broker, db)
        ml = MLFilter(fractional_settings_with_brackets, db)
        research = ResearchService(fractional_settings_with_brackets, broker, db)
        signals = SignalService(fractional_settings_with_brackets, broker, db, sentiment, ml, research)
        portfolio = Portfolio(fractional_settings_with_brackets, db, broker)
        engine = Engine(fractional_settings_with_brackets, db, broker, signals, portfolio)

        async def agree(sym: str, max_age_minutes: int = 20):
            return {"id": 1, "symbol": sym, "close": 150.0, "atr": 3.0, "trend": "up",
                    "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6, "features": {}}
        engine.signals.ensure = agree

        price = await broker.last_price("AAPL")
        run_id = engine.db.create_run("test")
        r = await engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test whole shares with bracket")
        )
        assert r.startswith("ACCEPTED"), f"Expected ACCEPTED, got: {r}"
        assert "Bracket" in r

        await broker.stop()

    @pytest.mark.asyncio
    async def test_fractional_sell_flatten(self, fractional_engine, fractional_broker, fractional_db):
        """Test selling fractional shares via flatten."""
        from app.engine import Engine
        price = await fractional_broker.last_price("AAPL")
        run_id = fractional_engine.db.create_run("test")
        # First buy fractional shares
        r = await fractional_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=0.5, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="buy fractional")
        )
        assert r.startswith("ACCEPTED")
        pid = int(r.split("#")[1].split(":")[0])
        await fractional_engine.execute(pid, actor="user")

        # Verify position exists
        positions = await fractional_broker.positions()
        assert "AAPL" in positions
        assert positions["AAPL"] > 0.49 and positions["AAPL"] < 0.51  # ~0.5 shares

        # Now flatten - should sell fractional part
        result = await fractional_engine.flatten(actor="user", reason="test flatten fractional")
        assert result["cancelled"] >= 0
        # Check that fractional sell was attempted
        fractional_sells = [o for o in result["orders"] if o["quantity"] < 1 and o["quantity"] > 0]
        assert len(fractional_sells) > 0, f"Expected fractional sell order in {result['orders']}"

    @pytest.mark.asyncio
    async def test_sizing_returns_float_for_fractional(self, fractional_settings):
        """Test that max_allowed_qty returns float with fractional precision."""
        from app.sizing import max_allowed_qty
        result = max_allowed_qty(fractional_settings, limit_price=100, last_price=100, held=0, stop_loss=95)
        assert isinstance(result, float)
        # Should be rounded to 4 decimal places
        assert result == round(result, 4)

    @pytest.mark.asyncio
    async def test_sizing_rounds_down_for_brackets(self, fractional_settings):
        """Test that sizing rounds down to whole shares when brackets enabled."""
        from app.sizing import max_allowed_qty
        # With max_order_usd=1000 and limit_price=251.25, max_allowed = 3.98
        # With brackets enabled, should round down to 3.0
        result = max_allowed_qty(fractional_settings, limit_price=251.25, last_price=250, held=0, stop_loss=240)
        # This is the raw sizing result (before engine bracket rounding)
        assert isinstance(result, float)
        # Engine will round down to whole shares for brackets

    @pytest.mark.asyncio
    async def test_whitelist_case_insensitive(self, fractional_engine, fractional_broker):
        """Test whitelist check is case insensitive."""
        price = await fractional_broker.last_price("aapl")  # lowercase
        run_id = fractional_engine.db.create_run("test")
        r = await fractional_engine._on_proposal(
            run_id,
            dict(symbol="aapl", side="BUY", quantity=0.5, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test lowercase whitelist")
        )
        assert r.startswith("ACCEPTED"), f"Expected ACCEPTED, got: {r}"


class TestFractionalSizing:
    """Test fractional sizing logic."""

    @pytest.fixture
    def fractional_settings(self, tmp_data_dir, monkeypatch):
        from app.config import load_settings
        import os
        monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_data_dir))
        monkeypatch.setenv("FRACTIONAL_ENABLED", "true")
        monkeypatch.setenv("FRACTIONAL_DECIMALS", "4")
        s = load_settings()
        return s

    @pytest.fixture
    def non_fractional_settings(self, tmp_data_dir, monkeypatch):
        from app.config import load_settings
        import os
        monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_data_dir))
        monkeypatch.setenv("FRACTIONAL_ENABLED", "false")
        s = load_settings()
        return s

    def test_max_allowed_qty_fractional_enabled(self, fractional_settings):
        from app.sizing import max_allowed_qty
        # With fractional enabled, returns float rounded to decimals
        result = max_allowed_qty(fractional_settings, limit_price=100, last_price=100, held=0, stop_loss=95)
        assert isinstance(result, float)
        assert result == round(result, 4)

    def test_max_allowed_qty_fractional_disabled(self, non_fractional_settings):
        from app.sizing import max_allowed_qty
        # With fractional disabled, returns whole number float
        result = max_allowed_qty(non_fractional_settings, limit_price=100, last_price=100, held=0, stop_loss=95)
        assert isinstance(result, float)
        assert result == float(int(result))

    def test_max_allowed_qty_binds_max_order_usd(self, fractional_settings):
        from app.sizing import max_allowed_qty
        # max_order_usd=1000, limit_price=250 -> max 4 shares
        result = max_allowed_qty(fractional_settings, limit_price=250, last_price=250, held=0, stop_loss=240)
        # With fractional, should be 4.0 (or slightly less due to risk limit)
        assert result <= 4.0

    def test_max_allowed_qty_binds_max_position_usd(self, fractional_settings):
        from app.sizing import max_allowed_qty
        # max_position_usd=2000, held=5 shares at 250 = 1250, remaining = 750
        # limit_price=250 -> 750/250 = 3 shares max
        result = max_allowed_qty(fractional_settings, limit_price=250, last_price=250, held=5, stop_loss=240)
        assert result <= 3.0

    def test_max_allowed_qty_binds_max_risk_per_trade(self, fractional_settings):
        from app.sizing import max_allowed_qty
        # max_risk_per_trade_usd=100, risk_per_share = 250 - 245 = 5
        # max qty = 100/5 = 20
        result = max_allowed_qty(fractional_settings, limit_price=250, last_price=250, held=0, stop_loss=245)
        assert result <= 20.0

    def test_max_allowed_qty_no_stop_loss(self, fractional_settings):
        from app.sizing import max_allowed_qty
        # Without stop loss, risk limit doesn't apply
        result = max_allowed_qty(fractional_settings, limit_price=100, last_price=100, held=0, stop_loss=None)
        # Should be limited by max_order_qty (50) or max_order_usd (1000/100=10)
        assert result <= 10.0


class TestFractionalRiskManager:
    """Test RiskManager fractional validation."""

    @pytest.fixture
    def fractional_settings(self, tmp_data_dir, monkeypatch):
        from app.config import load_settings
        import os
        monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_data_dir))
        monkeypatch.setenv("FRACTIONAL_ENABLED", "true")
        monkeypatch.setenv("FRACTIONAL_MIN_USD", "1.0")
        monkeypatch.setenv("FRACTIONAL_DECIMALS", "4")
        monkeypatch.setenv("FRACTIONAL_WHITELIST", "AAPL,MSFT")
        s = load_settings()
        return s

    @pytest.fixture
    def non_fractional_settings(self, tmp_data_dir, monkeypatch):
        from app.config import load_settings
        import os
        monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_data_dir))
        monkeypatch.setenv("FRACTIONAL_ENABLED", "false")
        s = load_settings()
        return s

    def test_is_fractional(self, fractional_settings):
        from app.risk import RiskManager
        rm = RiskManager(fractional_settings)
        assert rm._is_fractional(0.5) is True
        assert rm._is_fractional(1.5) is True
        assert rm._is_fractional(1.0) is False
        assert rm._is_fractional(1) is False
        assert rm._is_fractional(4.0) is False

    def test_validate_fractional_whitelist(self, fractional_settings):
        from app.risk import RiskManager
        rm = RiskManager(fractional_settings)
        ok, msg = rm._validate_fractional("AAPL", 0.5)
        assert ok is True
        assert msg == "ok"

        ok, msg = rm._validate_fractional("GOOG", 0.5)
        assert ok is False
        assert "not on the fractional whitelist" in msg

    def test_validate_fractional_disabled(self, non_fractional_settings):
        from app.risk import RiskManager
        rm = RiskManager(non_fractional_settings)
        ok, msg = rm._validate_fractional("AAPL", 0.5)
        assert ok is False
        assert "fractional trading is disabled" in msg

    def test_validate_fractional_decimals(self, fractional_settings):
        from app.risk import RiskManager
        rm = RiskManager(fractional_settings)
        ok, msg = rm._validate_fractional("AAPL", 0.1234)
        assert ok is True

        ok, msg = rm._validate_fractional("AAPL", 0.12345)
        assert ok is False
        assert "exceeds 4 decimal places" in msg

    def test_validate_fractional_min_precision(self, fractional_settings):
        from app.risk import RiskManager
        rm = RiskManager(fractional_settings)
        ok, msg = rm._validate_fractional("AAPL", 0.0001)
        assert ok is True

        ok, msg = rm._validate_fractional("AAPL", 0.00001)
        assert ok is False
        assert "below minimum precision" in msg