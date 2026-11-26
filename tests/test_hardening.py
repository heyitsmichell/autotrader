"""Hardening: token endpoint gating, anti-framing headers, PC_BOT_DISABLED guard."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_api_auth import HDR, make_app, make_client  # noqa: F401


async def test_token_endpoint_404_in_prod_like_request(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADER_DEV_TOKEN_ENDPOINT", raising=False)
    app = make_app(tmp_path)
    async with make_client(app) as c:
        r = await c.get("/api/auth/token", headers={"X-Trader": "1", "host": "x.ts.net"})
    app.state.db.close()
    assert r.status_code in (403, 404)
    assert "token" not in r.text.lower() or r.status_code != 200


async def test_token_endpoint_dev_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_DEV_TOKEN_ENDPOINT", "1")
    app = make_app(tmp_path)
    async with make_client(app) as c:
        r = await c.get("/api/auth/token", headers={"X-Trader": "1"})
    app.state.db.close()
    assert r.status_code == 200 and "token" in r.json()


async def test_frame_headers(tmp_path):
    app = make_app(tmp_path)
    async with make_client(app) as c:
        r = await c.get("/api/status")
        r2 = await c.get("/api/status", headers={"host": "evil.example"})
    app.state.db.close()
    for x in (r, r2):
        assert x.headers["x-frame-options"] == "DENY"
        assert x.headers["content-security-policy"] == "frame-ancestors 'none'"


def _load_trader():
    spec = importlib.util.spec_from_file_location(
        "trader_script", Path(__file__).resolve().parent.parent / "scripts" / "trader.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pc_disabled_guard(tmp_path, monkeypatch):
    mod = _load_trader()
    monkeypatch.setattr(mod, "pc_disabled", lambda *a: True)
    with pytest.raises(SystemExit) as e:
        mod._guard_pc_disabled(SimpleNamespace(override=False))
    assert e.value.code != 0
    mod._guard_pc_disabled(SimpleNamespace(override=True))
    monkeypatch.setattr(mod, "pc_disabled", lambda *a: False)
    mod._guard_pc_disabled(SimpleNamespace(override=False))
