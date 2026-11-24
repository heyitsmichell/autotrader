"""Rate-limit load cuts: batch daily bars, positions cache/single-flight, shared bars cache."""

import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.broker import Broker, BrokerError
from app.broker.parsers import parse_bars_by_symbol


def _line(day: int, close: float = 100.0, prefix: str = "") -> str:
    t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    return (f"  {prefix}{t.strftime('%Y-%m-%dT%H:%M:%SZ'):>20s}  O: {close:>10.2f}  H: {close:>10.2f}  "
            f"L: {close:>10.2f}  C: {close:>10.2f}  Vol: {1000:>12d}")


def _grouped(symbols, n=70):
    lines = ["=== Stock Bars (OHLCV) ==="]
    for s in symbols:
        lines.append(f"  --- {s} ---")
        lines += [_line(i) for i in range(n)]
    return "\n".join(lines)


class StubBroker(Broker):
    """Broker with a scripted transport (no __init__: no network, no settings)."""

    def __init__(self):
        self.calls = []
        self.missing = set()
        self.fail_batch = False
        self.positions_reply = "AAPL 10"
        self.delay = 0.0
        self._init_market_data()
        self._account_id = "ACC"

    async def call(self, tool, **args):
        self.calls.append((tool, args))
        if tool == "get_stock_bars":
            syms = args["symbols"].split(",")
            if self.fail_batch and len(syms) > 1:
                raise BrokerError("get_stock_bars: boom")
            return _grouped([s for s in syms if s not in self.missing], int(args["count"]))
        if tool == "get_account_positions":
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.positions_reply
        return ""

    def bar_calls(self):
        return [c for c in self.calls if c[0] == "get_stock_bars"]

    def pos_calls(self):
        return [c for c in self.calls if c[0] == "get_account_positions"]


def test_parse_grouped_and_mock_and_flat_formats():
    got = parse_bars_by_symbol(_grouped(["AAA", "BBB"], 5))
    assert set(got) == {"AAA", "BBB"} and len(got["AAA"]) == 5
    # mock server layout (same header, indented, extra disclaimer line)
    mock = "DISCLAIMER\n=== Stock Bars (OHLCV) ===\n  --- CCC ---\n" + _line(1) + "\n" + _line(2)
    assert len(parse_bars_by_symbol(mock)["CCC"]) == 2
    # flat layout: symbol prefix
    flat = "\n".join(_line(i, prefix=f"{'ZZZ':>8s}  ") for i in range(3))
    assert len(parse_bars_by_symbol(flat)["ZZZ"]) == 3


@pytest.mark.asyncio
async def test_chunking_and_missing_symbol_fallback():
    b = StubBroker()
    syms = [f"S{i}" for i in range(45)]
    b.missing = {"S3"}
    out = await b.daily_bars_many(syms, 70)
    assert set(out) == set(syms)
    batch = [c for c in b.bar_calls() if "," in c[1]["symbols"]]
    assert len(batch) == math.ceil(45 / b.BARS_BATCH_SIZE)
    singles = [c for c in b.bar_calls() if "," not in c[1]["symbols"]]
    assert [c[1]["symbols"] for c in singles] == ["S3"]
    assert out["S3"] == []     # single fallback also had nothing


@pytest.mark.asyncio
async def test_failed_batch_falls_back_to_singles():
    b = StubBroker()
    b.fail_batch = True
    out = await b.daily_bars_many(["A", "B", "C"], 70)
    assert set(out) == {"A", "B", "C"}
    assert len(b.bar_calls()) == 4


@pytest.mark.asyncio
async def test_signals_refresh_uses_batches(test_settings, test_db):
    from app.signals import SignalService

    class Sent:
        async def refresh_background(self, symbols, with_llm=None):
            pass

    b = StubBroker()
    svc = SignalService(test_settings, b, test_db, Sent(), None)
    svc._store = lambda run_id, sym, bars: {"symbol": sym, "n": len(bars)}
    syms = [f"T{i}" for i in range(30)]
    out = await svc.refresh(symbols=syms)
    assert len(out) == 30
    assert len(b.bar_calls()) == math.ceil(30 / b.BARS_BATCH_SIZE)
    # incremental refresh: also batched
    b.calls.clear()
    svc.BARS_TTL_SECONDS = 0
    svc._bars_ttl = lambda: 0
    await svc.refresh(symbols=syms)
    assert len(b.bar_calls()) == math.ceil(30 / b.BARS_BATCH_SIZE)
    assert all(c[1]["count"] == svc.BARS_INCREMENT for c in b.bar_calls())


@pytest.mark.asyncio
async def test_positions_ttl_and_single_flight():
    b = StubBroker()
    b.delay = 0.05
    res = await asyncio.gather(*[b.positions() for _ in range(10)])
    assert all(r == res[0] for r in res) and len(b.pos_calls()) == 1
    await b.positions()
    assert len(b.pos_calls()) == 1          # within TTL
    b.POSITIONS_TTL_SECONDS = 0.0
    await b.positions()
    assert len(b.pos_calls()) == 2          # expired


@pytest.mark.asyncio
async def test_positions_fresh_bypass_and_invalidation_after_order():
    b = StubBroker()
    await b.positions()
    await b.positions(fresh=True)
    assert len(b.pos_calls()) == 2
    await b.positions()
    assert len(b.pos_calls()) == 2          # fresh result was stored
    b.invalidate_positions()
    await b.positions()
    assert len(b.pos_calls()) == 3

    async def fake_submit(tool, **args):
        raise BrokerError("rejected")
    b._submit = fake_submit
    with pytest.raises(BrokerError):
        await b.cancel_order("X")           # failure also invalidates
    await b.positions()
    assert len(b.pos_calls()) == 4


@pytest.mark.asyncio
async def test_shared_bar_cache_hit():
    b = StubBroker()
    await b.daily_bars_many(["AAA", "BBB"], 70)
    n = len(b.bar_calls())
    got = await b.daily_bars("AAA", 30)
    assert len(got) == 30 and len(b.bar_calls()) == n
    await b.daily_bars("AAA", 30, use_cache=False)
    assert len(b.bar_calls()) == n + 1
    # a bigger request than cached is a miss
    await b.daily_bars("BBB", 200)
    assert len(b.bar_calls()) == n + 2
