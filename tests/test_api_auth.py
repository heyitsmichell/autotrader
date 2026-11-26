"""M21: local token auth, token injection, confirmations; M12: empty text guard."""

from __future__ import annotations

import dataclasses
import os
import stat
import sys

import pytest

from app.api import auth
from app.config import load_settings
from tests.test_api_support import HDR, TOKEN, make_app, make_client

TOKEN_DETAIL = {"detail": "missing or invalid X-Trader-Token"}


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path)
    yield a
    a.state.db.close()


async def test_post_without_headers_is_403(app):
    async with make_client(app) as c:
        r = await c.post("/api/alerts/test")
    assert r.status_code == 403 and r.json() == TOKEN_DETAIL


async def test_post_with_x_trader_but_no_token_is_403(app):
    async with make_client(app) as c:
        r = await c.post("/api/alerts/test", headers={"X-Trader": "1"})
    assert r.status_code == 403 and r.json() == TOKEN_DETAIL


async def test_post_with_wrong_token_is_403(app):
    async with make_client(app) as c:
        r = await c.post("/api/alerts/test", headers={"X-Trader": "1", "X-Trader-Token": "nope"})
    assert r.status_code == 403 and r.json() == TOKEN_DETAIL


async def test_post_with_token_but_no_x_trader_is_403(app):
    async with make_client(app) as c:
        r = await c.post("/api/alerts/test", headers={"X-Trader-Token": TOKEN})
    assert r.status_code == 403


async def test_post_with_valid_token_reaches_route(app):
    async with make_client(app) as c:
        r = await c.post("/api/alerts/test", headers=HDR)
    assert r.status_code == 409          # route ran: "No alert channel is set up"


async def test_put_and_delete_also_need_token(app):
    async with make_client(app) as c:
        assert (await c.put("/api/strategy", json={"text": "x"})).status_code == 403
        assert (await c.delete("/api/nothing")).status_code == 403


async def test_get_is_allowed_without_token(app):
    async with make_client(app) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200


async def test_non_loopback_host_is_forbidden(app):
    async with make_client(app) as c:
        r = await c.get("/api/status", headers={"host": "evil.example"})
        r2 = await c.post("/api/alerts/test", headers={**HDR, "host": "evil.example:8000"})
    assert r.status_code == 403 and r.json() == {"detail": "forbidden host"}
    assert r2.status_code == 403


async def test_ipv6_loopback_host_ok(app):
    async with make_client(app) as c:
        r = await c.get("/api/status", headers={"host": "[::1]:8000"})
    assert r.status_code == 200


async def test_auth_token_endpoint_requires_x_trader(app, monkeypatch):
    monkeypatch.setenv("TRADER_DEV_TOKEN_ENDPOINT", "1")
    async with make_client(app) as c:
        assert (await c.get("/api/auth/token")).status_code == 403
        r = await c.get("/api/auth/token", headers={"X-Trader": "1"})
    assert r.status_code == 200 and r.json() == {"token": TOKEN}


async def test_html_gets_token_meta(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html><head><title>x</title></head><body>hi</body></html>",
                                    encoding="utf-8")
    (web / "page.html").write_text("<html><head></head><body>p</body></html>", encoding="utf-8")
    (web / "app.css").write_text("body{}", encoding="utf-8")
    app = make_app(tmp_path, web_dir=web)
    meta = f'<meta name="trader-token" content="{TOKEN}"></head>'
    async with make_client(app) as c:
        r = await c.get("/")
        r2 = await c.get("/page.html")
        r3 = await c.get("/app.css")
    app.state.db.close()
    assert r.status_code == 200 and meta in r.text and r.headers["cache-control"] == "no-store"
    assert meta in r2.text
    assert r3.text == "body{}"


async def test_not_built_page_when_no_web_dir(app):
    async with make_client(app) as c:
        r = await c.get("/")
    assert r.status_code == 200 and "isn't built" in r.text


def test_inject_token_without_head():
    from app.api.app import inject_token
    assert inject_token("<p>x</p>", "abc").startswith('<meta name="trader-token" content="abc">')


def test_token_file_written_and_private(tmp_path):
    p = tmp_path / "sub" / "api_token"
    auth.write_token_file(p, "tok")
    assert p.read_text() == "tok"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert len(auth.new_token()) >= 40 and auth.new_token() != auth.new_token()


# -- confirmations -------------------------------------------------------------------
async def test_kill_on_needs_no_confirm(app):
    async with make_client(app) as c:
        r = await c.post("/api/kill", json={"on": True}, headers=HDR)
    assert r.status_code == 200 and app.state.engine.kill_switch is True


async def test_kill_release_needs_confirm(app):
    eng = app.state.engine
    eng.kill_switch = True
    async with make_client(app) as c:
        r = await c.post("/api/kill", json={"on": False}, headers=HDR)
        assert r.status_code == 400 and "RELEASE" in r.json()["detail"]
        assert eng.kill_switch is True
        r = await c.post("/api/kill", json={"on": False, "confirm": "release"}, headers=HDR)
        assert r.status_code == 400
        r = await c.post("/api/kill", json={"on": False, "confirm": "RELEASE"}, headers=HDR)
    assert r.status_code == 200 and eng.kill_switch is False
    assert "supervisor.on_release" in app.state.calls


async def test_mode_auto_on_live_needs_confirm(tmp_path):
    live = dataclasses.replace(load_settings(), broker_mode="webull", webull_environment="prod",
                               allow_live_auto=True)
    assert live.is_live
    app = make_app(tmp_path, settings=live)
    async with make_client(app) as c:
        r = await c.post("/api/mode", json={"mode": "auto"}, headers=HDR)
        assert r.status_code == 400 and "AUTO" in r.json()["detail"]
        assert app.state.engine.mode_calls == []
        r = await c.post("/api/mode", json={"mode": "auto", "confirm": "AUTO"}, headers=HDR)
        assert r.status_code == 200 and app.state.engine.trading_mode == "auto"
        # back to approve never needs a confirm
        r = await c.post("/api/mode", json={"mode": "approve"}, headers=HDR)
        assert r.status_code == 200
    app.state.db.close()


async def test_mode_auto_on_paper_needs_no_confirm(app):
    assert not app.state.settings.is_live
    async with make_client(app) as c:
        r = await c.post("/api/mode", json={"mode": "auto"}, headers=HDR)
    assert r.status_code == 200


# -- M12 -------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/strategy", "/api/lessons"])
@pytest.mark.parametrize("text", ["", "   \n\t "])
async def test_empty_text_rejected(app, tmp_path, monkeypatch, path, text):
    from app.api import learning
    f = tmp_path / "doc.md"
    f.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(learning, "STRATEGY_FILE", f)
    monkeypatch.setattr(learning, "LESSONS_FILE", f)
    async with make_client(app) as c:
        r = await c.put(path, json={"text": text}, headers=HDR)
    assert r.status_code == 400
    assert f.read_text(encoding="utf-8") == "keep me"


async def test_nonempty_text_saved(app, tmp_path, monkeypatch):
    from app.api import learning
    f = tmp_path / "strategy.md"
    monkeypatch.setattr(learning, "STRATEGY_FILE", f)
    async with make_client(app) as c:
        r = await c.put("/api/strategy", json={"text": "  buy dips  "}, headers=HDR)
    assert r.status_code == 200 and f.read_text(encoding="utf-8") == "buy dips\n"

