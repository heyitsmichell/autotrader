"""Status additions, trades row shape (L11), proposals/lessons models, route table."""

from __future__ import annotations

import pytest

from app.api.models import TradeRow
from tests.test_api_support import HDR, make_app, make_client


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path)
    yield a
    a.state.db.close()


async def test_status_has_new_and_existing_keys(app):
    async with make_client(app) as c:
        r = await c.get("/api/status")
    j = r.json()
    for key in ("broker_label", "kill_switch", "scheduler_on", "limits", "consensus", "research",
                "settings", "supervisor", "alerts", "flattening", "kill_reason", "day_pnl",
                "duplicate_instance", "realtime", "usage_today"):
        assert key in j, key
    assert j["token_required"] is True
    assert j["broker_mode_warning"] is None
    assert "data_age_seconds" in j
    # scheduler off and kill off -> paused
    assert j["paused"] is True
    g = j["daily_loss_guard"]
    assert set(g) == {"enabled", "tripped", "pnl", "limit"}


async def test_status_paused_false_when_killed_or_running(app):
    eng = app.state.engine
    eng.kill_switch = True
    async with make_client(app) as c:
        assert (await c.get("/api/status")).json()["paused"] is False
        eng.kill_switch = False
        eng._sched = True
        assert (await c.get("/api/status")).json()["paused"] is False


async def test_status_mode_warning(app):
    app.state.broker.mode_warning = "orders are SIMULATED"
    async with make_client(app) as c:
        assert (await c.get("/api/status")).json()["broker_mode_warning"] == "orders are SIMULATED"


async def test_daily_loss_guard_tripped_and_cached(tmp_path):
    import dataclasses
    from app.config import load_settings
    s = dataclasses.replace(load_settings(), max_daily_loss_usd=200.0)
    app = make_app(tmp_path, settings=s, pnl=-250.0)
    async with make_client(app) as c:
        g1 = (await c.get("/api/status")).json()["daily_loss_guard"]
        g2 = (await c.get("/api/status")).json()["daily_loss_guard"]
    assert g1 == {"enabled": True, "tripped": True, "pnl": -250.0, "limit": 200.0} == g2
    assert app.state.broker.pnl_calls == 1          # cached for >= 5 s
    app.state.db.close()


async def test_daily_loss_guard_never_fails_status(tmp_path):
    app = make_app(tmp_path, pnl=RuntimeError("broker down"))
    async with make_client(app) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200
    assert r.json()["daily_loss_guard"]["pnl"] is None
    app.state.db.close()


def _insert_trade(db, **kw):
    row = {"symbol": "AAPL", "side": "BUY", "quantity_requested": 5, "limit_price": 10.0,
           "status": "open", "created_at": "2026-01-01T00:00:00",
           "broker_mode": db.broker_mode, "webull_environment": db.webull_environment,
           "account_id": db.account_id}
    row.update(kw)
    cols = ", ".join(row)
    db.conn.execute(f"INSERT INTO trade_ledger ({cols}) VALUES ({', '.join('?' * len(row))})",
                    tuple(row.values()))


async def test_trades_same_shape_for_every_filter(app):
    db = app.state.db
    _insert_trade(db, proposal_id=1, status="open", entry_fill_price=10.0)
    _insert_trade(db, proposal_id=2, status="closed", entry_fill_price=10.0,
                  exit_fill_price=11.0, realized_pnl=5.0, entry_features='{"a": 1}')
    _insert_trade(db, proposal_id=3, symbol="MSFT", status="closed")
    shape = set(TradeRow.model_fields)
    async with make_client(app) as c:
        out = {}
        for st in ("open", "closed", "all"):
            r = await c.get(f"/api/trades?status={st}")
            assert r.status_code == 200
            out[st] = r.json()
            assert out[st], st
            for row in out[st]:
                assert set(row) == shape
        default = (await c.get("/api/trades")).json()
        by_sym = (await c.get("/api/trades?status=closed&symbol=msft")).json()
    assert {t["proposal_id"] for t in out["open"]} == {1}
    assert {t["proposal_id"] for t in out["closed"]} == {2, 3}
    assert {t["proposal_id"] for t in out["all"]} == {1, 2, 3} == {t["proposal_id"] for t in default}
    assert [t["proposal_id"] for t in by_sym] == [3]
    closed2 = next(t for t in out["closed"] if t["proposal_id"] == 2)
    assert closed2["entry_features"] == '{"a": 1}' and closed2["realized_pnl"] == 5.0


async def test_proposals_and_lessons_models_keep_keys(app):
    db = app.state.db
    pid = db.add_proposal(None, {"symbol": "AAPL", "side": "BUY", "quantity": 1,
                                 "limit_price": 10.0, "confidence": 0.7, "rationale": "r"},
                          "pending")
    async with make_client(app) as c:
        rows = (await c.get("/api/proposals")).json()
        lessons = (await c.get("/api/lessons")).json()
    assert rows and rows[0]["id"] == pid and rows[0]["symbol"] == "AAPL"
    assert {"status", "client_order_id", "run_id"} <= set(rows[0])      # extra db columns survive
    assert lessons == []


async def test_unknown_api_route_404(app):
    async with make_client(app) as c:
        r = await c.get("/api/does/not/exist")
        r2 = await c.post("/api/does/not/exist", headers=HDR)
    assert r.status_code == 404 and r2.status_code == 404


async def test_route_table_has_all_legacy_paths(app):
    spec = app.openapi()["paths"]
    have = {(m.upper(), p) for p, ops in spec.items() for m in ops}
    legacy = [
        ("GET", "/api/status"), ("GET", "/api/portfolio"), ("GET", "/api/signals"),
        ("GET", "/api/fastloop"), ("GET", "/api/options/chain"), ("GET", "/api/options/snapshot"),
        ("GET", "/api/options/status"), ("GET", "/api/execution-quality"),
        ("GET", "/api/performance"), ("GET", "/api/research"), ("GET", "/api/yfinance/{symbol}"),
        ("GET", "/api/yfinance/macro/context"), ("GET", "/api/signals/{symbol}"),
        ("GET", "/api/news"), ("GET", "/api/sentiment"), ("GET", "/api/proposals"),
        ("GET", "/api/proposals/{pid}"), ("GET", "/api/runs"), ("GET", "/api/events"),
        ("GET", "/api/autopsies"), ("GET", "/api/autopsies/{trade_id}"), ("GET", "/api/trades"),
        ("GET", "/api/trades/{proposal_id}"), ("GET", "/api/lessons"),
        ("POST", "/api/lessons/{lesson_id}/status"), ("POST", "/api/lessons/{lesson_id}/rollback"),
        ("GET", "/api/ml/{model_id}/scorecard"), ("GET", "/api/strategy"), ("GET", "/api/learning"),
        ("GET", "/api/ml"), ("GET", "/api/chart/history"), ("GET", "/api/chart/overlays"),
        ("GET", "/api/stream"), ("POST", "/api/run"), ("POST", "/api/proposals/{pid}/approve"),
        ("POST", "/api/proposals/{pid}/requote"), ("POST", "/api/proposals/{pid}/dismiss"),
        ("POST", "/api/mode"), ("POST", "/api/scheduler"), ("POST", "/api/kill"),
        ("POST", "/api/flatten"), ("POST", "/api/alerts/test"), ("GET", "/api/alerts/summary"),
        ("POST", "/api/shutdown"), ("POST", "/api/broker/reconnect"),
        ("POST", "/api/signals/refresh"), ("POST", "/api/research/refresh"),
        ("POST", "/api/sentiment/score"), ("POST", "/api/ml/train"), ("POST", "/api/ml/promote"),
        ("POST", "/api/ml/reject"), ("POST", "/api/ml/deactivate"), ("PUT", "/api/strategy"),
        ("PUT", "/api/lessons"), ("POST", "/api/learning/evaluate"),
        ("POST", "/api/learning/review"), ("POST", "/api/learning/refresh"),
        ("GET", "/api/auth/token"),
    ]
    missing = [x for x in legacy if x not in have]
    assert not missing, missing


async def test_flatten_still_needs_confirm_and_token(app):
    async with make_client(app) as c:
        assert (await c.post("/api/flatten", json={})).status_code == 403
        r = await c.post("/api/flatten", json={"confirm": "no"}, headers=HDR)
    assert r.status_code == 400


async def test_shutdown_without_server_is_409(app):
    async with make_client(app) as c:
        r = await c.post("/api/shutdown", headers=HDR)
    assert r.status_code == 409
