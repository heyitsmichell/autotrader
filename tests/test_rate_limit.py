"""HTTP 429 handling: retry with backoff, concurrency cap, one batched snapshot request."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from app import broker as broker_mod
from app.broker import Broker, BrokerError, is_rate_limited, retry_after_seconds
from app.config import load_settings


def _result(text, error=False):
    return SimpleNamespace(content=[TextContent(type="text", text=text)], is_error=error)


class FakeSession:
    def __init__(self, replies, delay=0.0):
        self.replies = list(replies)
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay = delay

    async def call_tool(self, name, arguments=None):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            return reply
        finally:
            self.in_flight -= 1


def _broker(session, **kw):
    s = replace(load_settings(), broker_backoff_seconds=0.01, **kw)
    b = Broker(s)
    b.session = session
    return b


@pytest.fixture(autouse=True)
def no_jitter(monkeypatch):
    monkeypatch.setattr(broker_mod.random, "uniform", lambda a, b: 0.0)


def test_detects_rate_limit_but_not_prices():
    assert is_rate_limited("HTTP 429 Too Many Requests")
    assert is_rate_limited("too many requests")
    assert not is_rate_limited("MPWR last price 429.50")
    assert not is_rate_limited("order id 14290")
    assert retry_after_seconds("429, Retry-After: 3") == 3.0


@pytest.mark.asyncio
async def test_retries_429_then_succeeds():
    sess = FakeSession([_result("Error: 429 too many requests", True),
                        _result("Error: 429 too many requests", True),
                        _result("ok")])
    b = _broker(sess)
    assert await b.call("get_stock_snapshot", symbols="AAPL") == "ok"
    assert sess.calls == 3


@pytest.mark.asyncio
async def test_gives_up_after_configured_retries():
    sess = FakeSession([_result("Error: 429 too many requests", True)])
    b = _broker(sess, broker_rate_limit_retries=2)
    with pytest.raises(BrokerError):
        await b.call("get_stock_snapshot", symbols="AAPL")
    assert sess.calls == 3  # first try + 2 retries


@pytest.mark.asyncio
async def test_concurrency_is_capped():
    sess = FakeSession([_result("ok")], delay=0.02)
    b = _broker(sess, broker_max_concurrency=2)
    await asyncio.gather(*(b.call("get_account_list", n=i) for i in range(10)))
    assert sess.max_in_flight <= 2


@pytest.mark.asyncio
async def test_many_symbols_make_one_snapshot_request(monkeypatch):
    sess = FakeSession([_result("snap")], delay=0.01)
    b = _broker(sess)
    syms = [f"S{i}" for i in range(17)]
    monkeypatch.setattr("app.broker.market_data.parse_snapshot",
                        lambda text, *a, **k: {s: {"price": 10.0 + i} for i, s in enumerate(syms)})
    prices = await asyncio.gather(*(b.last_price(s) for s in syms))
    assert sess.calls == 1
    assert all(p is not None for p in prices)
