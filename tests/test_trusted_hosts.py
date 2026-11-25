"""TRUSTED_HOSTS: extra Host names (e.g. a private Tailscale name) besides loopback."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import install_auth


def _client(trusted):
    app = FastAPI()
    install_auth(app, trusted)

    @app.get("/ping")
    def ping():
        return {"ok": True}
    return TestClient(app)


def test_loopback_only_by_default():
    c = _client(None)
    assert c.get("/ping", headers={"host": "127.0.0.1:8000"}).status_code == 200
    assert c.get("/ping", headers={"host": "trader.tail1234.ts.net"}).status_code == 403


def test_trusted_host_allowed_case_insensitive_and_others_refused():
    c = _client(["Trader.tail1234.ts.net"])
    assert c.get("/ping", headers={"host": "trader.tail1234.ts.net"}).status_code == 200
    assert c.get("/ping", headers={"host": "evil.example.com"}).status_code == 403
