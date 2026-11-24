"""Ring buffer, incremental bar aggregator, subscription budget and the feed's hot-path API."""
import asyncio
from dataclasses import replace

import pytest

from app.config import load_settings
from app.eventhub import hub
from app.feed import BarAggregator, MarketFeed, RingBuffer, TickBus, budget_subscriptions


def test_ring_buffer_keeps_latest():
    r = RingBuffer(3)
    for i in range(5):
        r.append({"t": i, "p": i})
    assert len(r) == 3 and [t["t"] for t in r.ticks()] == [2, 3, 4]
    assert r.latest()["t"] == 4 and [t["t"] for t in r.ticks(2)] == [3, 4]


def test_bar_aggregator_incremental():
    a = BarAggregator(60)
    a.add(120.5, 10.0, 5)
    a.add(130.0, 12.0, 1)
    a.add(150.0, 9.0, 2)
    a.add(61.0, 99.0, 1)              # older than current bar: ignored
    a.add(185.0, 11.0, 4)             # next minute
    bars = a.bars()
    assert [b["t"] for b in bars] == [120, 180]
    assert bars[0] == {"t": 120, "open": 10.0, "high": 12.0, "low": 9.0, "close": 9.0, "volume": 8.0}
    assert bars[1]["open"] == bars[1]["close"] == 11.0
    assert a.bars(1)[0]["t"] == 180


def test_tickbus_bars_quote_and_event_throttle():
    bus = TickBus(ring_size=10)

    async def run():
        q = hub.subscribe()
        for i in range(10):          # 10 ticks "at the same instant" -> one event
            bus.publish({"symbol": "aapl", "t": 1000.0 + i, "p": 100.0 + i, "v": 1.0}, now=50.0)
        bus.publish({"symbol": "AAPL", "t": 1011.0, "p": 111.0, "v": 1.0}, now=50.3)
        await asyncio.sleep(0)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        hub.unsubscribe(q)
        return events

    events = [e for e in asyncio.run(run()) if e.type == "quote"]
    assert len(events) == 2
    assert set(events[0].data) == {"symbol", "price", "bid", "ask", "ts", "as_of"}
    assert bus.quote("AAPL").last == 111.0 and bus.quote("AAPL").as_of == 1011.0
    assert len(bus.ring("AAPL")) == 10                 # capped at ring_size
    b1 = bus.bars("AAPL", "1m")
    assert b1[-1]["close"] == 111.0
    assert bus.bars("AAPL", "5m")[-1]["high"] == 111.0
    with pytest.raises(ValueError):
        bus.bars("AAPL", "2m")


def test_subscription_budget():
    cands = {"AAPL": [f"AAPL2501{i:02d}C00100000" for i in range(10)],
             "MSFT": [f"MSFT2501{i:02d}C00100000" for i in range(10)]}
    out = budget_subscriptions({"TSLA", "AAPL250100C00100000"}, cands, top_k=4, max_subscriptions=100)
    assert out[:2] == ["AAPL250100C00100000", "TSLA"]            # held first
    assert len(out) == len(set(out)) == 2 + 4 + 4 - 1             # top-k per underlying, dedup held
    capped = budget_subscriptions({"TSLA", "NVDA"}, cands, top_k=4, max_subscriptions=5)
    assert len(capped) == 5 and {"TSLA", "NVDA"} <= set(capped)
    assert budget_subscriptions(set(), {}, 4, 5) == []


def test_marketfeed_api_keeps_tick_format_and_budgets():
    s = replace(load_settings(), feed_max_subscriptions=3, feed_options_top_k=1)
    feed = MarketFeed(s, broker=None)
    q = asyncio.Queue()
    feed.subscribe("AAPL", q)
    feed._publish("AAPL", 1_700_000_000.0, 150.0, 2.0)
    assert q.get_nowait() == {"symbol": "AAPL", "t": 1_700_000_000.0, "p": 150.0, "v": 2.0}
    assert feed.last["AAPL"]["p"] == 150.0
    assert feed.quote("AAPL").last == 150.0 and len(feed.ring("AAPL")) == 1
    assert feed.bars("AAPL", "1m")[0]["open"] == 150.0
    subs = feed.set_subscriptions({"AAPL"}, {"AAPL": ["O1", "O2"], "MSFT": ["O3", "O4"]})
    assert subs == ["AAPL", "O1", "O3"] and feed.subscriptions == subs
