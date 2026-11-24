"""Shared stubs for the tests/test_api_*.py suites (no tests in this module)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from app.api.app import create_app
from app.config import load_settings
from app.db import DB

TOKEN = "test-token-abc"
HDR = {"X-Trader": "1", "X-Trader-Token": TOKEN}


class Ret:
    """Return value that can be awaited or used synchronously (falsy)."""

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return None

    def __bool__(self):
        return False


class Rec:
    """Recording stub: unknown methods log '<name>.<method>' into ``calls`` and return Ret()."""

    def __init__(self, name: str, calls: list, **attrs):
        self._name, self._calls = name, calls
        for k, v in attrs.items():
            setattr(self, k, v)

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)

        def fn(*a, **k):
            self._calls.append(f"{self._name}.{item}")
            return Ret()
        return fn


class StubEngine:
    def __init__(self, db=None):
        self.db = db
        self.trading_mode = "approve"
        self.kill_switch = False
        self.kill_reason = None
        self.flattening = False
        self.running = False
        self.next_run_at = None
        self._sched = False
        self.kill_calls: list = []
        self.mode_calls: list = []

    @property
    def scheduler_on(self):
        return self._sched

    def set_mode(self, mode):
        if mode not in ("approve", "auto"):
            raise ValueError("mode must be 'approve' or 'auto'")
        self.mode_calls.append(mode)
        self.trading_mode = mode

    async def set_kill_switch(self, on, reason="turned on by you"):
        self.kill_calls.append((on, reason))
        self.kill_switch = on
        self.kill_reason = reason if on else None

    async def stop_scheduler(self):
        self._sched = False

    def start_scheduler(self):
        self._sched = True


class StubBroker:
    def __init__(self, pnl=0.0):
        self.connected = True
        self.error = None
        self.mode_warning = None
        self.tools = []
        self.pnl = pnl
        self.pnl_calls = 0

    async def day_pnl(self):
        self.pnl_calls += 1
        if isinstance(self.pnl, Exception):
            raise self.pnl
        return self.pnl


class StubPortfolio:
    cached = None

    def invalidate(self):
        pass

    async def snapshot(self, max_age=5.0):
        return {"balance": {"net_liq": 1000.0, "day_pnl": 0.0}, "positions": [], "orders": []}

    def equity_today(self):
        return []


class StubFeed:
    mode = "poll"
    error = None

    def __init__(self):
        self.last = {}
        self.queues = []

    def quote(self, sym):
        return None

    def subscribe(self, sym, q):
        self.queues.append((sym, q))

    def unsubscribe(self, sym, q):
        self.queues = [(s, x) for s, x in self.queues if x is not q]


def build_state(app, tmp_path, *, settings=None, pnl=0.0):
    calls: list = []
    db = DB(tmp_path / "t.db", "mock", None, None)
    st = app.state
    st.settings = settings or load_settings()
    st.db = db
    st.engine = StubEngine(db)
    st.broker = StubBroker(pnl)
    st.portfolio = StubPortfolio()
    st.feed = StubFeed()
    st.sentiment = SimpleNamespace(scoring=False, last_scoring=None, fear_greed=None,
                                   summary=lambda s: {})
    st.signals = SimpleNamespace(refreshing=False, error=None, last_refresh=None)
    st.research = SimpleNamespace(last_refresh=None, refreshing=False, manual_symbols=[],
                                  auto_symbols=[], errors=[])
    st.ml = SimpleNamespace(available=False, training=False, model_id=None)
    st.supervisor = Rec("supervisor", calls, status=lambda: {})
    st.alerts = Rec("alerts", calls, status=lambda: {}, channels=[])
    st.tasks = set()
    st.calls = calls
    return st


def make_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def make_app(tmp_path, web_dir=None, **kw):
    """App with a no-op lifespan and stubbed state (state.api_token == TOKEN)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop(app):
        yield

    app = create_app(lifespan=noop, web_dir=web_dir or (tmp_path / "no_web"), token=TOKEN)
    build_state(app, tmp_path, **kw)
    return app
