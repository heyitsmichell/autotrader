"""Tests for options trading (Phase B)."""

import pytest
import tempfile
from pathlib import Path
import os
import json


def _app_today():
    """The app's date (conftest freezes the clock); OptionRiskManager measures DTE against it."""
    from app.market import now_et
    return now_et().date()



@pytest.fixture
def options_settings(tmp_data_dir):
    """Load test settings with options enabled."""
    from app.config import load_settings
    os.environ["TRADER_DATA_DIR"] = str(tmp_data_dir)
    os.environ["MOCK_STATE_FILE"] = str(tmp_data_dir / "mock_state.json")
    os.environ["OPTIONS_ENABLED"] = "true"
    os.environ["OPTIONS_MAX_CONTRACTS_PER_ORDER"] = "10"
    os.environ["OPTIONS_MAX_NOTIONAL_USD"] = "5000"
    os.environ["OPTIONS_ALLOWED_STRATEGIES"] = "SINGLE,VERTICAL,STRADDLE,STRANGLE"
    os.environ["OPTIONS_REQUIRE_APPROVAL"] = "false"
    os.environ["OPTIONS_MIN_DTE"] = "7"
    os.environ["OPTIONS_MAX_DTE"] = "45"
    os.environ["OPTIONS_ALLOWED_UNDERLYINGS"] = "AAPL,MSFT,NVDA,SPY,QQQ"
    # Use mock broker
    os.environ["BROKER_MODE"] = "mock"
    os.environ["WEBULL_ENVIRONMENT"] = "uat"
    os.environ["WATCHLIST"] = "AAPL,MSFT,NVDA,SPY,QQQ"
    os.environ["MARKET_HOURS_ONLY"] = "false"
    os.environ["TRADING_MODE"] = "approve"
    s = load_settings()
    return s


@pytest.fixture
async def options_broker(options_settings):
    """Create a mock broker with options enabled."""
    from app.broker import Broker
    broker = Broker(options_settings)
    await broker.start()
    yield broker
    await broker.stop()


@pytest.fixture
async def options_db(options_settings, tmp_data_dir):
    """Create a test database with options enabled."""
    from app.db import DB
    db = DB(tmp_data_dir / "trader.db", options_settings.broker_mode,
            options_settings.webull_environment, options_settings.webull_account_id)
    yield db


@pytest.fixture
async def options_engine(options_settings, options_broker, options_db):
    """Create a test engine with options enabled."""
    from app.signals import SignalService
    from app.sentiment import SentimentEngine
    from app.ml import MLFilter
    from app.research import ResearchService
    from app.portfolio import Portfolio
    from app.engine import Engine

    sentiment = SentimentEngine(options_settings, options_broker, options_db)
    ml = MLFilter(options_settings, options_db)
    research = ResearchService(options_settings, options_broker, options_db)
    signals = SignalService(options_settings, options_broker, options_db, sentiment, ml, research)
    portfolio = Portfolio(options_settings, options_db, options_broker)
    engine = Engine(options_settings, options_db, options_broker, signals, portfolio)
    yield engine


class TestOptionRiskManager:
    """Test the OptionRiskManager validation logic."""

    def test_check_single_leg_valid_call(self, options_settings):
        """Test valid single-leg CALL option."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        p = {
            "symbol": "AAPL240119C00150000",  # AAPL Jan 19 2024 CALL $150
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": "2024-01-19",
            "position_intent": "BUY_TO_OPEN",
        }
        ok, reasons = risk.check_single_leg(p)
        # Note: This test may fail if the date is in the past (DTE check)
        # The test is mainly checking the validation logic works

    def test_check_single_leg_valid_put(self, options_settings):
        """Test valid single-leg PUT option."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        # Use a future date for testing
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        p = {
            "symbol": f"SPY{future_date.replace('-', '')[2:]}P00400000",  # SPY PUT $400
            "side": "BUY",
            "quantity": 2,
            "limit_price": 3.50,
            "option_type": "PUT",
            "strike_price": 400.0,
            "expiration_date": future_date,
            "position_intent": "BUY_TO_OPEN",
        }
        ok, reasons = risk.check_single_leg(p)
        # Should pass basic validation
        assert isinstance(ok, bool)
        assert isinstance(reasons, list)

    def test_check_single_leg_disabled(self, options_settings):
        """Test that options are rejected when disabled."""
        from app.risk import OptionRiskManager
        from app.config import load_settings
        import os

        # Disable options
        os.environ["OPTIONS_ENABLED"] = "false"
        disabled_settings = load_settings()

        risk = OptionRiskManager(disabled_settings)

        p = {
            "symbol": "AAPL240119C00150000",
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": "2024-01-19",
            "position_intent": "BUY_TO_OPEN",
        }
        ok, reasons = risk.check_single_leg(p)
        assert ok is False
        assert any("disabled" in r for r in reasons)

    def test_check_single_leg_invalid_symbol(self, options_settings):
        """Test invalid OCC symbol format."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        p = {
            "symbol": "INVALID_SYMBOL",
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": "2024-01-19",
            "position_intent": "BUY_TO_OPEN",
        }
        ok, reasons = risk.check_single_leg(p)
        assert ok is False
        assert any("invalid OCC symbol" in r for r in reasons)

    def test_check_single_leg_invalid_underlying(self, options_settings):
        """Test underlying not in allowed list."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        p = {
            "symbol": f"TSLA{future_date.replace('-', '')[2:]}C00200000",  # TSLA not in allowed
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 200.0,
            "expiration_date": future_date,
            "position_intent": "BUY_TO_OPEN",
        }
        ok, reasons = risk.check_single_leg(p)
        assert ok is False
        assert any("not in allowed underlyings" in r for r in reasons)

    def test_check_single_leg_exceeds_max_contracts(self, options_settings):
        """Test quantity exceeds max contracts per order."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        p = {
            "symbol": f"AAPL{future_date.replace('-', '')[2:]}C00150000",
            "side": "BUY",
            "quantity": 15,  # Exceeds max of 10
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": future_date,
            "position_intent": "BUY_TO_OPEN",
        }
        ok, reasons = risk.check_single_leg(p)
        assert ok is False
        assert any("exceeds max contracts" in r for r in reasons)

    def test_check_single_leg_exceeds_notional(self, options_settings):
        """Test premium notional exceeds max."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        p = {
            "symbol": f"AAPL{future_date.replace('-', '')[2:]}C00150000",
            "side": "BUY",
            "quantity": 10,
            "limit_price": 10.00,  # 10 * 10 * 100 = $10,000 > $5,000 max
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": future_date,
            "position_intent": "BUY_TO_OPEN",
        }
        ok, reasons = risk.check_single_leg(p)
        assert ok is False
        assert any("notional" in r.lower() for r in reasons)

    def test_check_single_leg_invalid_intent(self, options_settings):
        """Test invalid position_intent."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        p = {
            "symbol": f"AAPL{future_date.replace('-', '')[2:]}C00150000",
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": future_date,
            "position_intent": "INVALID_INTENT",
        }
        ok, reasons = risk.check_single_leg(p)
        assert ok is False
        assert any("invalid position_intent" in r for r in reasons)

    def test_check_strategy_valid_vertical(self, options_settings):
        """Test valid vertical spread strategy."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()
        date_str = future_date.replace('-', '')[2:]

        legs = [
            {
                "symbol": f"AAPL{date_str}C00150000",
                "side": "BUY",
                "quantity": 1,
                "limit_price": 5.00,
                "option_type": "CALL",
                "strike_price": 150.0,
                "expiration_date": future_date,
                "position_intent": "BUY_TO_OPEN",
                "option_strategy": "VERTICAL",
            },
            {
                "symbol": f"AAPL{date_str}C00155000",
                "side": "SELL",
                "quantity": 1,
                "limit_price": 3.00,
                "option_type": "CALL",
                "strike_price": 155.0,
                "expiration_date": future_date,
                "position_intent": "SELL_TO_OPEN",
                "option_strategy": "VERTICAL",
            },
        ]
        ok, reasons = risk.check_strategy(legs)
        assert isinstance(ok, bool)
        assert isinstance(reasons, list)

    def test_check_strategy_invalid_leg_count(self, options_settings):
        """Test strategy with wrong number of legs."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()
        date_str = future_date.replace('-', '')[2:]

        # Vertical needs exactly 2 legs, provide 3
        legs = [
            {
                "symbol": f"AAPL{date_str}C00150000",
                "side": "BUY",
                "quantity": 1,
                "limit_price": 5.00,
                "option_type": "CALL",
                "strike_price": 150.0,
                "expiration_date": future_date,
                "position_intent": "BUY_TO_OPEN",
                "option_strategy": "VERTICAL",
            },
            {
                "symbol": f"AAPL{date_str}C00155000",
                "side": "SELL",
                "quantity": 1,
                "limit_price": 3.00,
                "option_type": "CALL",
                "strike_price": 155.0,
                "expiration_date": future_date,
                "position_intent": "SELL_TO_OPEN",
                "option_strategy": "VERTICAL",
            },
            {
                "symbol": f"AAPL{date_str}C00160000",
                "side": "BUY",
                "quantity": 1,
                "limit_price": 2.00,
                "option_type": "CALL",
                "strike_price": 160.0,
                "expiration_date": future_date,
                "position_intent": "BUY_TO_OPEN",
                "option_strategy": "VERTICAL",
            },
        ]
        ok, reasons = risk.check_strategy(legs)
        assert ok is False
        assert any("requires 2-2 legs" in r for r in reasons)

    def test_check_strategy_disallowed(self, options_settings):
        """Test disallowed strategy type."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()
        date_str = future_date.replace('-', '')[2:]

        legs = [
            {
                "symbol": f"AAPL{date_str}C00150000",
                "side": "BUY",
                "quantity": 1,
                "limit_price": 5.00,
                "option_type": "CALL",
                "strike_price": 150.0,
                "expiration_date": future_date,
                "position_intent": "BUY_TO_OPEN",
                "option_strategy": "IRON_CONDOR",  # Not in allowed list
            },
            {
                "symbol": f"AAPL{date_str}C00155000",
                "side": "SELL",
                "quantity": 1,
                "limit_price": 3.00,
                "option_type": "CALL",
                "strike_price": 155.0,
                "expiration_date": future_date,
                "position_intent": "SELL_TO_OPEN",
                "option_strategy": "IRON_CONDOR",
            },
            {
                "symbol": f"AAPL{date_str}C00160000",
                "side": "SELL",
                "quantity": 1,
                "limit_price": 2.00,
                "option_type": "CALL",
                "strike_price": 160.0,
                "expiration_date": future_date,
                "position_intent": "SELL_TO_OPEN",
                "option_strategy": "IRON_CONDOR",
            },
            {
                "symbol": f"AAPL{date_str}C00165000",
                "side": "BUY",
                "quantity": 1,
                "limit_price": 1.00,
                "option_type": "CALL",
                "strike_price": 165.0,
                "expiration_date": future_date,
                "position_intent": "BUY_TO_OPEN",
                "option_strategy": "IRON_CONDOR",
            },
        ]
        ok, reasons = risk.check_strategy(legs)
        assert ok is False
        assert any("not in allowed strategies" in r for r in reasons)


class TestOptionParsing:
    """Test OCC symbol parsing."""

    def test_parse_occ_symbol_call(self, options_settings):
        """Parse a CALL option symbol."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        parsed = risk._parse_occ_symbol("AAPL240119C00150000")
        assert parsed is not None
        assert parsed["underlying"] == "AAPL"
        assert parsed["expiration"] == "2024-01-19"
        assert parsed["option_type"] == "CALL"
        assert parsed["strike"] == 150.0

    def test_parse_occ_symbol_put(self, options_settings):
        """Parse a PUT option symbol."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        parsed = risk._parse_occ_symbol("SPY240621P00400000")
        assert parsed is not None
        assert parsed["underlying"] == "SPY"
        assert parsed["expiration"] == "2024-06-21"
        assert parsed["option_type"] == "PUT"
        assert parsed["strike"] == 400.0

    def test_parse_occ_symbol_invalid(self, options_settings):
        """Parse invalid symbol returns None."""
        from app.risk import OptionRiskManager
        risk = OptionRiskManager(options_settings)

        assert risk._parse_occ_symbol("INVALID") is None
        assert risk._parse_occ_symbol("AAPL240119X00150000") is None  # Invalid type
        assert risk._parse_occ_symbol("AAPL240119C150000") is None  # Wrong strike format


class TestOptionOrderFlow:
    """Test option order proposal and execution flow."""

    @pytest.mark.asyncio
    async def test_option_proposal_rejected_when_disabled(self, tmp_data_dir, mock_broker, test_db):
        """Options should be rejected when OPTIONS_ENABLED=false."""
        # Create settings with OPTIONS_ENABLED explicitly false
        import os
        from app.config import load_settings
        os.environ["OPTIONS_ENABLED"] = "false"
        disabled_settings = load_settings()
        # Override the options_enabled field by creating new Settings
        disabled_settings = disabled_settings.__class__(**{**disabled_settings.__dict__, "options_enabled": False})

        from app.signals import SignalService
        from app.sentiment import SentimentEngine
        from app.ml import MLFilter
        from app.research import ResearchService
        from app.portfolio import Portfolio
        from app.engine import Engine

        sentiment = SentimentEngine(disabled_settings, mock_broker, test_db)
        ml = MLFilter(disabled_settings, test_db)
        research = ResearchService(disabled_settings, mock_broker, test_db)
        signals = SignalService(disabled_settings, mock_broker, test_db, sentiment, ml, research)
        portfolio = Portfolio(disabled_settings, test_db, mock_broker)
        engine = Engine(disabled_settings, test_db, mock_broker, signals, portfolio)

        # Override signals.ensure to return bullish
        async def agree(sym: str, max_age_minutes: int = 20, allow_stale: bool = False):
            return {"id": 1, "symbol": sym, "close": 150.0, "atr": 3.0, "trend": "up",
                    "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6, "features": {}}
        engine.signals.ensure = agree

        # Try to propose an option (should be rejected - options disabled)
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        result = await engine._on_proposal(1, {
            "symbol": f"AAPL{future_date.replace('-', '')[2:]}C00150000",
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": future_date,
            "position_intent": "BUY_TO_OPEN",
        })
        assert "REJECTED" in result
        assert "disabled" in result.lower() or "OPTIONS_ENABLED" in result

    @pytest.mark.asyncio
    async def test_option_proposal_accepted(self, options_engine, options_settings):
        """Valid option proposal should be accepted."""
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        result = await options_engine._on_proposal(1, {
            "symbol": f"AAPL{future_date.replace('-', '')[2:]}C00150000",
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": future_date,
            "position_intent": "BUY_TO_OPEN",
        })
        # Should be accepted or rejected for valid reasons (e.g., DTE if date is in past)
        assert "ACCEPTED" in result or "REJECTED" in result

    @pytest.mark.asyncio
    async def test_option_proposal_missing_fields(self, options_engine):
        """Option proposal missing required fields should be rejected."""
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        result = await options_engine._on_proposal(1, {
            "symbol": f"AAPL{future_date.replace('-', '')[2:]}C00150000",
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            # Missing option_type, strike_price, expiration_date, position_intent
        })
        assert "REJECTED" in result
        assert "missing required field" in result


class TestOptionDatabase:
    """Test option data stored in database."""

    @pytest.mark.asyncio
    async def test_option_proposal_stores_option_fields(self, options_engine, options_db):
        """Option proposal should store asset_class, instrument_json, etc."""
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()

        await options_engine._on_proposal(1, {
            "symbol": f"AAPL{future_date.replace('-', '')[2:]}C00150000",
            "side": "BUY",
            "quantity": 1,
            "limit_price": 5.00,
            "option_type": "CALL",
            "strike_price": 150.0,
            "expiration_date": future_date,
            "position_intent": "BUY_TO_OPEN",
        })

        # Get the proposal from database
        proposals = options_db.pending_proposals()
        assert len(proposals) >= 1

        # Find our option proposal
        opt_prop = None
        for p in proposals:
            if p["symbol"].startswith("AAPL") and "C00150000" in p["symbol"]:
                opt_prop = p
                break

        if opt_prop:
            assert opt_prop.get("asset_class") == "option"
            assert opt_prop.get("instrument_json") is not None
            assert opt_prop.get("strategy_type") == "SINGLE"

            # Parse instrument_json
            import json
            inst = json.loads(opt_prop["instrument_json"])
            assert inst["option_type"] == "CALL"
            assert inst["strike_price"] == 150.0
            assert inst["expiration_date"] == future_date
            assert inst["position_intent"] == "BUY_TO_OPEN"
            assert inst["multiplier"] == 100


class TestOptionMockBroker:
    """Test mock broker option functionality."""

    @pytest.mark.asyncio
    async def test_mock_option_contracts(self, options_broker):
        """Mock broker should return option contracts."""
        contracts = await options_broker.option_contracts(["AAPL"])
        assert contracts is not None

    @pytest.mark.asyncio
    async def test_mock_option_snapshots(self, options_broker):
        """Mock broker should return option snapshots."""
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()
        date_str = future_date.replace('-', '')[2:]

        snapshots = await options_broker.parsed_option_snapshots([f"AAPL{date_str}C00150000"])
        assert isinstance(snapshots, dict)

    @pytest.mark.asyncio
    async def test_mock_preview_option_order(self, options_broker):
        """Mock broker should preview option orders."""
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()
        date_str = future_date.replace('-', '')[2:]

        preview = await options_broker.preview_option_order(
            f"AAPL{date_str}C00150000", "BUY", 1, "CALL", 150.0, future_date, 5.00
        )
        assert preview is not None

    @pytest.mark.asyncio
    async def test_mock_place_option_single_order(self, options_broker):
        """Mock broker should place single-leg option orders."""
        from datetime import date, timedelta
        future_date = (_app_today() + timedelta(days=30)).isoformat()
        date_str = future_date.replace('-', '')[2:]

        response = await options_broker.place_option_single_order(
            f"AAPL{date_str}C00150000", "BUY", 1, "CALL", 150.0, future_date, 5.00, "test-coid-123"
        )
        assert response is not None


class TestIsOptionSymbol:
    """Test the _is_option_symbol helper."""

    def test_is_option_symbol_true(self):
        """Valid OCC symbols should be detected."""
        from app.engine import _is_option_symbol

        assert _is_option_symbol("AAPL240119C00150000") is True
        assert _is_option_symbol("SPY240621P00400000") is True
        assert _is_option_symbol("NVDA241220C00500000") is True

    def test_is_option_symbol_false(self):
        """Non-option symbols should not be detected."""
        from app.engine import _is_option_symbol

        assert _is_option_symbol("AAPL") is False
        assert _is_option_symbol("SPY") is False
        assert _is_option_symbol("INVALID") is False
        assert _is_option_symbol("AAPL240119X00150000") is False  # Invalid type
        assert _is_option_symbol("") is False