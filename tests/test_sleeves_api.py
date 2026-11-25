"""HTTP API for sleeves (app/api/sleeves.py)."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from app.config import load_settings
from app.sleeves.manager import SleeveManager
from tests.test_api_support import HDR, TOKEN, make_app, make_client
from tests.test_sleeve_manager import FakeBroker


def build(tmp_path, enabled=True):
    tmp_path.mkdir(exist_ok=True)
    app = make_app(tmp_path)
    s = dataclasses.replace(load_settings(), sleeves_enabled=enabled)
    app.state.settings = s
    eng = SimpleNamespace(s=s, db=app.state.db, broker=FakeBroker(), portfolio=None, kill_switch=False,
                          scheduler_stopped_by_user=False)
    eng.sleeves = SleeveManager(eng)
    app.state.engine = eng
    return app


@pytest.fixture
def app(tmp_path):
    a = build(tmp_path)
    yield a
    a.state.db.close()


async def test_list_sleeves(app):
    async with make_client(app) as c:
        r = await c.get("/api/sleeves", headers=HDR)
    assert r.status_code == 200
    j = r.json()
    assert j["enabled"] is True and [s["id"] for s in j["sleeves"]] == ["swing", "momentum", "putwrite", "vaa"]


async def test_list_sleeves_disabled(tmp_path):
    a = build(tmp_path, enabled=False)
    try:
        async with make_client(a) as c:
            j = (await c.get("/api/sleeves", headers=HDR)).json()
        assert j["enabled"] is False and all(s["status"] == "disabled" for s in j["sleeves"])
    finally:
        a.state.db.close()


async def test_equity_series(app):
    from datetime import date
    app.state.db.record_sleeve_equity(date.today().isoformat(), "momentum", 123.0)
    async with make_client(app) as c:
        r = await c.get("/api/sleeves/equity?days=30", headers=HDR)
    assert r.status_code == 200
    assert r.json()["series"][0]["momentum"] == 123.0


async def test_detail_and_unknown(app):
    async with make_client(app) as c:
        r = await c.get("/api/sleeves/momentum", headers=HDR)
        assert r.status_code == 200
        j = r.json()
        assert j["id"] == "momentum" and j["holdings"] == [] and "targets" in j and "orders" in j
        assert (await c.get("/api/sleeves/nope", headers=HDR)).status_code == 404
        assert (await c.post("/api/sleeves/nope/pause", json={"on": True}, headers=HDR)).status_code == 404


async def test_pause_needs_header(app):
    async with make_client(app) as c:
        r = await c.post("/api/sleeves/momentum/pause", json={"on": True})
        assert r.status_code == 403
        r = await c.post("/api/sleeves/momentum/pause", json={"on": True}, headers=HDR)
        assert r.status_code == 200 and r.json() == {"ok": True}
    assert app.state.db.sleeve_state("momentum")["paused"] == 1


async def test_rebalance_swing_is_400_and_disabled_is_409(app, tmp_path):
    async with make_client(app) as c:
        assert (await c.post("/api/sleeves/swing/rebalance", headers=HDR)).status_code == 400
        ok = await c.post("/api/sleeves/momentum/rebalance", headers=HDR)
        assert ok.status_code == 200 and "queued" in ok.json()["message"]
    off = build(tmp_path / "off", enabled=False)
    try:
        async with make_client(off) as c:
            assert (await c.post("/api/sleeves/momentum/rebalance", headers=HDR)).status_code == 409
    finally:
        off.state.db.close()
