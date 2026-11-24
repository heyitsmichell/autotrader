"""SSE: multiplexed hub stream (prime, diff, heartbeat, unsubscribe), chart mode, publisher."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api import stream as S
from app.eventhub import hub
from tests.test_api_support import make_app, make_client


class FakeRequest:
    def __init__(self):
        self.gone = False

    async def is_disconnected(self):
        return self.gone


@pytest.fixture
def app(tmp_path):
    hub._latest.clear()
    a = make_app(tmp_path)
    yield a
    a.state.db.close()
    hub._latest.clear()


def parse(msg: str):
    assert msg.endswith("\n\n")
    ev, data = msg.strip().split("\n", 1)
    assert ev.startswith("event: ") and data.startswith("data: ")
    return ev[7:], json.loads(data[6:])


async def nxt(agen, timeout=2.0):
    return await asyncio.wait_for(agen.__anext__(), timeout)


async def test_mux_primes_with_hub_latest_and_fresh_snapshots(app):
    hub.publish("kill", {"on": False, "reason": None, "at": None})
    hub.publish("proposals", [{"id": 999}])               # stale: fresh snapshot must win
    req = FakeRequest()
    agen = S.mux_stream(req, app.state, heartbeat=5)
    got = {}
    for _ in range(4):
        t, d = parse(await nxt(agen))
        got[t] = d
    assert set(got) == {"kill", "status", "portfolio", "proposals"}
    assert got["proposals"] == []                         # fresh, not the stale hub value
    assert got["status"]["token_required"] is True
    assert got["kill"]["on"] is False
    assert hub.subscriber_count == 1
    req.gone = True
    await agen.aclose()
    assert hub.subscriber_count == 0


async def test_mux_diffs_identical_payloads_and_heartbeats(app):
    req = FakeRequest()
    agen = S.mux_stream(req, app.state, heartbeat=0.1)
    for _ in range(3):
        await nxt(agen)                                   # status, portfolio, proposals
    hub.publish("proposals", [])                          # identical to primed -> skipped
    t, d = parse(await nxt(agen))
    assert t == "heartbeat" and "ts" in d
    hub.publish("proposals", [{"id": 1}])                 # changed -> sent
    t, d = parse(await nxt(agen))
    assert (t, d) == ("proposals", [{"id": 1}])
    hub.publish("proposals", [{"id": 1}])                 # duplicate again -> skipped
    assert parse(await nxt(agen))[0] == "heartbeat"
    # fills are never diffed
    hub.publish("fill", {"symbol": "AAPL"})
    hub.publish("fill", {"symbol": "AAPL"})
    assert parse(await nxt(agen))[0] == "fill" and parse(await nxt(agen))[0] == "fill"
    await agen.aclose()
    assert hub.subscriber_count == 0


async def test_mux_status_diff_ignores_volatile_time(app):
    a = S.fingerprint("status", {"server_time": "1", "x": 1, "now_et": "a", "data_age_seconds": 3})
    b = S.fingerprint("status", {"server_time": "2", "x": 1, "now_et": "b", "data_age_seconds": 9})
    c = S.fingerprint("status", {"server_time": "2", "x": 2})
    assert a == b != c


async def test_mux_skips_portfolio_when_broker_offline(app):
    app.state.broker.connected = False
    agen = S.mux_stream(FakeRequest(), app.state, heartbeat=0.05)
    types = [parse(await nxt(agen))[0] for _ in range(3)]
    await agen.aclose()
    assert types == ["status", "proposals", "heartbeat"]


async def test_chart_mode_is_byte_compatible(app):
    feed = app.state.feed
    feed.last = {"AAPL": {"p": 1.5}}
    req = FakeRequest()
    agen = S.chart_stream(req, feed, ["AAPL"])
    hello = await nxt(agen)
    assert hello == 'event: hello\ndata: ' + json.dumps({"mode": "poll", "last": {"AAPL": {"p": 1.5}}}) + "\n\n"
    (sym, q), = feed.queues
    q.put_nowait({"symbol": "AAPL", "t": 1, "p": 2.0})
    assert await nxt(agen) == 'data: ' + json.dumps({"symbol": "AAPL", "t": 1, "p": 2.0}) + "\n\n"
    await agen.aclose()
    assert feed.queues == []


async def test_chart_mode_keepalive(app, monkeypatch):
    monkeypatch.setattr(S, "CHART_KEEPALIVE", 0.05)
    agen = S.chart_stream(FakeRequest(), app.state.feed, ["AAPL"])
    await nxt(agen)
    assert await nxt(agen) == ": keep-alive\n\n"
    await agen.aclose()


async def test_stream_route_validation(app):
    async with make_client(app) as c:
        assert (await c.get("/api/stream?symbol=ZZZZ")).status_code == 404
        assert (await c.get("/api/stream?symbol=")).status_code == 400


async def test_publisher_publishes_only_on_change(app):
    q = hub.subscribe()
    task = asyncio.create_task(S.publisher_loop(app, interval=0.02))
    try:
        await asyncio.sleep(0.3)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait().type)
        # one of each on the first tick, nothing more while nothing changes
        assert sorted(seen) == ["portfolio", "proposals", "status"]
        app.state.db.add_proposal(None, {"symbol": "AAPL", "side": "BUY", "quantity": 1,
                                         "limit_price": 10.0}, "pending")
        await asyncio.sleep(0.3)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait().type)
        assert seen == ["proposals"]
    finally:
        task.cancel()
        hub.unsubscribe(q)


async def test_publisher_idle_without_subscribers(app):
    task = asyncio.create_task(S.publisher_loop(app, interval=0.02))
    await asyncio.sleep(0.1)
    task.cancel()
    assert hub.latest() == []
