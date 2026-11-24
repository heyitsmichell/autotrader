"""yfinance wrapper: timeout, negative cache, circuit breaker (M6)."""
import time
from types import SimpleNamespace

import pytest

from app.yfinance_service import YahooFinanceService


def _svc(**kw):
    s = SimpleNamespace(yfinance_timeout_seconds=0.2, yfinance_negative_ttl_seconds=60.0,
                        yfinance_breaker_failures=3, yfinance_breaker_cooldown_seconds=100.0)
    for k, v in kw.items():
        setattr(s, k, v)
    return YahooFinanceService(s)


@pytest.mark.asyncio
async def test_timeout_returns_error_and_negative_caches():
    svc = _svc()
    calls = []

    def slow(symbol):
        calls.append(symbol)
        time.sleep(0.6)
        return {"symbol": symbol}

    svc._fetch_sync = slow
    t0 = time.monotonic()
    r = await svc.get_summary("AAPL")
    assert time.monotonic() - t0 < 0.5 and "timed out" in r["error"]
    r2 = await svc.get_summary("AAPL")                 # served from the negative cache
    assert "negative cache" in r2["error"] and calls == ["AAPL"]


@pytest.mark.asyncio
async def test_breaker_opens_then_recovers():
    now = [0.0]
    svc = _svc(yfinance_negative_ttl_seconds=0.0)
    svc._clock = lambda: now[0]
    n = []

    def boom(symbol):
        n.append(symbol)
        raise RuntimeError("blocked")

    svc._fetch_sync = boom
    for sym in ("A", "B", "C"):
        assert "blocked" in (await svc.get_summary(sym))["error"]
    assert svc.breaker_open
    r = await svc.get_summary("D")
    assert "circuit breaker" in r["error"] and n == ["A", "B", "C"]   # D never hit Yahoo
    now[0] = 101.0                                                    # cooldown over
    svc._fetch_sync = lambda symbol: {"symbol": symbol, "ok": True}
    assert (await svc.get_summary("D"))["ok"] is True and not svc.breaker_open


@pytest.mark.asyncio
async def test_success_is_cached_and_macro_is_guarded():
    svc = _svc()
    n = []
    svc._fetch_sync = lambda s: n.append(s) or {"symbol": s}
    await svc.get_summary("msft")
    await svc.get_summary("MSFT")
    assert n == ["MSFT"]

    def hang():
        time.sleep(0.6)
        return {}

    svc._fetch_macro_sync = hang
    assert "timed out" in (await svc.get_macro_context())["error"]
