"""Events, band and tearsheet endpoints (app/api/sleeves.py)."""
from __future__ import annotations

import pytest

from tests.test_api_support import HDR, make_client
from tests.test_sleeves_api import build


@pytest.fixture
def app(tmp_path):
    a = build(tmp_path)
    yield a
    a.state.db.close()


async def test_events_route_not_a_sleeve_id(app):
    db = app.state.db
    db.add_sleeve_event("momentum", "rebalance", "a")
    db.add_sleeve_event(None, "kill_on", "b")
    async with make_client(app) as c:
        r = await c.get("/api/sleeves/events", headers=HDR)
        assert r.status_code == 200
        ev = r.json()["events"]
        assert [e["kind"] for e in ev] == ["rebalance", "kill_on"]
        assert set(ev[0]) == {"id", "ts", "day", "sleeve", "kind", "text"}
        r = await c.get("/api/sleeves/events?sleeve=momentum&days=30", headers=HDR)
        assert [e["kind"] for e in r.json()["events"]] == ["rebalance"]
        assert (await c.get("/api/sleeves/events?sleeve=nope", headers=HDR)).status_code == 404


async def test_band_endpoint(app):
    from datetime import date, timedelta
    d0 = date.today() - timedelta(days=10)
    for i in range(8):
        app.state.db.record_sleeve_equity((d0 + timedelta(days=i)).isoformat(), "momentum", 100.0 + i)
    async with make_client(app) as c:
        j = (await c.get("/api/sleeves/momentum/band", headers=HDR)).json()
        assert j["sleeve"] == "momentum" and len(j["points"]) == 8 and j["status"] in ("inside", "above", "below")
        assert set(j["points"][1]) == {"day", "live", "p5", "p25", "p50", "p75", "p95"}
        e = (await c.get("/api/sleeves/vaa/band", headers=HDR)).json()
        assert e["status"] == "insufficient" and e["points"] == []
        assert (await c.get("/api/sleeves/zzz/band", headers=HDR)).status_code == 404


async def test_tearsheet_endpoint(app):
    async with make_client(app) as c:
        j = (await c.get("/api/sleeves/momentum/tearsheet?source=backtest&window=60", headers=HDR)).json()
        assert j["source"] == "backtest" and j["days"] > 1000 and j["metrics"]["cagr"] is not None
        assert j["end"] <= "2025-11-21" and len(j["worst_drawdowns"]) == 5 and j["monthly"] and j["yearly"]
        live = (await c.get("/api/sleeves/momentum/tearsheet", headers=HDR)).json()
        assert live["source"] == "live" and live["days"] == 0 and live["metrics"]["sharpe"] is None
        assert (await c.get("/api/sleeves/zzz/tearsheet", headers=HDR)).status_code == 404
        assert (await c.get("/api/sleeves/momentum/tearsheet?source=x", headers=HDR)).status_code == 400
