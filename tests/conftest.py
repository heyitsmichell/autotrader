"""Pytest configuration and fixtures for AI Trader tests."""

import asyncio
import contextlib
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Isolate the whole session from the real data/ and logs/ folders. app.config resolves
# DATA_DIR / LOG_DIR at import time, so this must happen before any app import.
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="trader-tests-"))
(_SESSION_DIR / "data").mkdir()
(_SESSION_DIR / "logs").mkdir()
# Never read the developer's real .env (app/config/env.py honours this).
os.environ["TRADER_SKIP_DOTENV"] = "1"
os.environ["TRADER_DATA_DIR"] = str(_SESSION_DIR / "data")
os.environ["TRADER_LOG_DIR"] = str(_SESSION_DIR / "logs")
os.environ.setdefault("MOCK_STATE_FILE", str(_SESSION_DIR / "data" / "mock_state.json"))

# Set test environment variables before importing app modules
os.environ.update(
    BROKER_MODE="mock",
    MARKET_HOURS_ONLY="false",
    TRADING_MODE="approve",
    WATCHLIST="AAPL,MSFT,SPY",
    MAX_ORDER_USD="1000",
    MAX_POSITION_USD="2000",
    MAX_ORDER_QTY="50",
    MAX_TRADES_PER_DAY="5",
    PRICE_BAND_PCT="1.0",
    MAX_RISK_PER_TRADE_USD="100",
    BRACKET_ORDERS="true",
    SENTIMENT_LLM="false",
    ML_HISTORY_BARS="700",
    ML_SIZE_SCALING="false",  # sizing tests pin the limit-based quantity; scaling is covered in test_ml_first
    AUTO_START_SCHEDULER="false",
    KILL_MAX_DRAWDOWN_PCT="10",
    KILL_DAILY_LOSS_USD="0",
    KILL_MAX_CONSECUTIVE_ERRORS="3",
    KILL_ACTION="pause",
    FLATTEN_SLIPPAGE_PCT="0.5",
    MARKET_CLOSED_DATES="",
    MARKET_EARLY_CLOSE_DATES="",
    ALERT_NTFY_TOPIC="",
    ALERT_TELEGRAM_BOT_TOKEN="",
    ALERT_WEBHOOK_URL="",
    ALERT_SMTP_HOST="",
    WEBULL_TOKEN_DIR="./conf",
    # Never let a developer's .env account leak into tests: the mock broker has its own
    # account, and the real account id makes every mock call fail with "unknown account_id".
    WEBULL_ACCOUNT_ID="",
    WEBULL_ENVIRONMENT="uat",
    # Core-Satellite (Phase 4)
    CORE_SYMBOL="SPY",
    CORE_TARGET_PCT="80",
    SATELLITE_MAX_PCT="20",
    CORE_REBALANCE_BAND_PCT="2.0",
    CORE_MIN_TRADE_USD="100",
    CORE_REBALANCE_MARKET_HOURS_ONLY="true",
    SATELLITE_DRAWDOWN_GUARD_PCT="10",
    SATELLITE_DRAWDOWN_ACTION="shrink",
    # Champion/Challenger (Phase 5)
    ML_CHALLENGER_MIN_TRADES="20",
    ML_CHALLENGER_MIN_HIT_RATE_IMPROVEMENT="0.02",
    ML_CHALLENGER_MIN_RETURN_IMPROVEMENT="0.1",
    ML_CHALLENGER_MAX_DAYS="14",
    # Eligibility - mock broker has ~3.9% synthetic drift, so set threshold higher
    ELIGIBILITY_MAX_GAP_PCT="10.0",
    ELIGIBILITY_ENABLED="true",
)

# Set test time to a regular trading hour (10:00 AM ET on a Friday)
# 2026-11-20 is a Friday, regular trading day
TEST_TIME_ET = datetime.fromisoformat("2026-11-20T10:00:00-05:00")
os.environ["TRADER_TEST_TIME"] = TEST_TIME_ET.isoformat()

@pytest.fixture(scope="session", autouse=True)
def set_test_market_time():
    """Set a fixed test time during regular market hours for all tests."""
    from app.market import set_test_time
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    set_test_time(TEST_TIME_ET.astimezone(ET))
    yield
    set_test_time(None)  # Reset after tests
    os.environ.pop("TRADER_TEST_TIME", None)


@pytest.fixture(autouse=True)
def _restore_test_clock():
    """Put the suite clock back after every test. Several tests move it with set_test_time()
    (premarket, overnight) and never restore it, which made later files order-dependent
    (e.g. test_bracket_orders.py failed after test_24h_trading.py)."""
    import app.market as m
    saved = m._test_time
    yield
    m.set_test_time(saved)


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@contextlib.contextmanager
def db_tmpdir():
    """Drop-in for ``tempfile.TemporaryDirectory()`` for tests that create ``DB`` objects in
    it. Every DB opened inside is closed BEFORE the directory is removed, so Windows does
    not fail the cleanup with WinError 32 (QA finding T1)."""
    from app.db import DB
    created: list = []
    orig = DB.__init__

    def tracking_init(self, *a, **k):
        orig(self, *a, **k)
        created.append(self)

    DB.__init__ = tracking_init
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                yield tmp
            finally:
                for d in created:
                    d.close()
    finally:
        DB.__init__ = orig


@pytest.fixture
def tmp_data_dir(tmp_path):
    """A per-test temporary directory for test data (pytest keeps the last few for inspection)."""
    return tmp_path


@pytest.fixture
def test_settings(tmp_data_dir):
    """Load test settings with temporary data directory."""
    from app.config import load_settings
    import os
    os.environ["TRADER_DATA_DIR"] = str(tmp_data_dir)
    os.environ["MOCK_STATE_FILE"] = str(tmp_data_dir / "mock_state.json")
    # Ensure fractional is disabled by default for non-fractional tests
    os.environ["FRACTIONAL_ENABLED"] = "false"
    s = load_settings()
    return s


@pytest.fixture
async def mock_broker(test_settings):
    """Create a mock broker instance."""
    from app.broker import Broker
    broker = Broker(test_settings)
    await broker.start()
    yield broker
    await broker.stop()


@pytest.fixture
async def test_db(test_settings, tmp_data_dir):
    """Create a test database instance."""
    from app.db import DB
    db = DB(tmp_data_dir / "trader.db", test_settings.broker_mode,
            test_settings.webull_environment, test_settings.webull_account_id)
    yield db
    db.close()   # release the file before the temp dir goes away (T1)


@pytest.fixture
async def test_engine(test_settings, mock_broker, test_db):
    """Create a test engine instance."""
    from app.signals import SignalService
    from app.sentiment import SentimentEngine
    from app.ml import MLFilter
    from app.research import ResearchService
    from app.portfolio import Portfolio
    from app.engine import Engine

    sentiment = SentimentEngine(test_settings, mock_broker, test_db)
    ml = MLFilter(test_settings, test_db)
    research = ResearchService(test_settings, mock_broker, test_db)
    signals = SignalService(test_settings, mock_broker, test_db, sentiment, ml, research)
    portfolio = Portfolio(test_settings, test_db, mock_broker)
    engine = Engine(test_settings, test_db, mock_broker, signals, portfolio)

    # Override signals.ensure to always return bullish for testing
    async def agree(sym: str, max_age_minutes: int = 20):
        return {"id": 1, "symbol": sym, "close": 150.0, "atr": 3.0, "trend": "up",
                "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": 0.6, "features": {}}
    engine.signals.ensure = agree

    yield engine


@pytest.fixture
def bullish_signal():
    """A signal where every engine agrees (used to test the order flow)."""
    return {"id": None, "symbol": "X", "close": 100.0, "atr": 2.0, "trend": "up",
            "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": None, "features": {}}


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "slow: mark test as slow")