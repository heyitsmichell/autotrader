"""Extended-hours position protection: no doomed combo, stop on fill, soft stop, auto-run safety."""

import dataclasses
import time
from unittest.mock import AsyncMock

import pytest

from app.broker import BrokerError
from app.engine import execution, risk_loop
from app.quotes import Quote


def _set(engine, **kw):
    engine.s = dataclasses.replace(engine.s, **kw)
    engine.portfolio.s = engine.s


def _events(db):
    return [dict(level=r[0], message=r[1]) for r in
            db.conn.execute("SELECT level, message FROM events").fetchall()]


def _proposal(db, side="BUY", status="submitting"):
    return db.add_proposal(None, dict(symbol="AAPL", side=side, quantity=4, limit_price=100.0,
                                      confidence=0.5, rationale="t"),
                           status, sl=95.0, tp=110.0)


# --- issue 1: premarket entry skips the combo -----------------------------------------------------
@pytest.mark.asyncio
async def test_premarket_entry_skips_bracket(test_engine, mock_broker, monkeypatch):
    monkeypatch.setattr(execution, "order_session", lambda *a, **k: "ALL")
    monkeypatch.setattr(execution, "session_phase", lambda *a, **k: "premarket")
    _set(test_engine, bracket_orders=True)
    mock_broker.preview_order = AsyncMock(return_value="ok")
    mock_broker.place_bracket = AsyncMock(side_effect=AssertionError("combo must not be sent"))
    mock_broker.place_order = AsyncMock(return_value="placed\nok")
    db = test_engine.db
    pid = _proposal(db)
    await test_engine._send(pid, db.get_proposal(pid), "user", "what")
    mock_broker.place_bracket.assert_not_called()
    mock_broker.place_order.assert_awaited_once()
    assert mock_broker.place_order.await_args.kwargs["trading_session"] == "ALL"
    assert db.get_proposal(pid)["status"] == "placed"
    assert db.protective("AAPL") == []
    assert not any("rejected the bracket" in e["message"] for e in _events(db))


# --- issue 4: _auto_execute_run survives unexpected errors ----------------------------------------
@pytest.mark.asyncio
async def test_auto_execute_run_continues_after_runtime_error(test_engine):
    test_engine.trading_mode = "auto"
    test_engine.db.pending_for_run = lambda run_id: [{"id": 1}, {"id": 2}]
    calls = []

    async def fake_execute(pid, actor):
        calls.append(pid)
        if pid == 1:
            raise RuntimeError("boom")
    test_engine.execute = fake_execute
    await test_engine._auto_execute_run(7)
    assert calls == [1, 2]
    assert any(e["level"] == "error" and "#1" in e["message"] for e in _events(test_engine.db))


# --- issue 2: stop placed on fill, no duplicate ---------------------------------------------------
def _trade(**kw):
    t = {"symbol": "AAPL", "side": "BUY", "stop_price": 95.0, "proposal_id": 11}
    t.update(kw)
    return t


@pytest.mark.asyncio
async def test_fill_places_core_stop_once(test_engine, mock_broker):
    pf = test_engine.portfolio
    _set(test_engine, bracket_orders=True)
    mock_broker.place_stop = AsyncMock(return_value="ok")
    await pf._protect_filled_entry(_trade(), 3, {"AAPL": 3.0}, set())
    mock_broker.place_stop.assert_awaited_once()
    args = mock_broker.place_stop.await_args
    assert args.args[:3] == ("AAPL", 3.0, 95.0)
    assert args.kwargs["allow_when_killed"] is True
    rows = test_engine.db.protective("AAPL")
    assert [(r["kind"], r["status"], r["quantity"]) for r in rows] == [("sl", "active", 3.0)]
    # a second pass and reconcile's periodic check both see the row: no duplicate
    await pf._protect_filled_entry(_trade(), 3, {"AAPL": 3.0}, set())
    mock_broker.positions = AsyncMock(return_value={"AAPL": 3.0})
    mock_broker.open_orders_text = AsyncMock(return_value="")
    assert await test_engine._verify_protective_stops() == 0
    assert mock_broker.place_stop.await_count == 1


@pytest.mark.asyncio
async def test_fill_skips_when_working_sell_or_no_stop(test_engine, mock_broker):
    pf = test_engine.portfolio
    _set(test_engine, bracket_orders=True)
    mock_broker.place_stop = AsyncMock()
    await pf._protect_filled_entry(_trade(), 3, {"AAPL": 3.0}, {"AAPL"})
    await pf._protect_filled_entry(_trade(stop_price=None), 3, {"AAPL": 3.0}, set())
    await pf._protect_filled_entry(_trade(), 3, {}, set())          # position not visible yet
    mock_broker.place_stop.assert_not_called()


@pytest.mark.asyncio
async def test_fill_stop_sized_to_held_and_failure_is_contained(test_engine, mock_broker):
    pf = test_engine.portfolio
    _set(test_engine, bracket_orders=True)
    mock_broker.place_stop = AsyncMock(side_effect=BrokerError("nope"))
    await pf._protect_filled_entry(_trade(), 5, {"AAPL": 2.0}, set())
    assert mock_broker.place_stop.await_args.args[1] == 2.0
    assert test_engine.db.protective("AAPL") == []        # row closed, so reconcile can retry


# --- issue 3: soft stop ---------------------------------------------------------------------------
async def _soft(rl):
    """Soft-stop pass honouring S6: a first reading below the stop, 31 s later a second one."""
    t = getattr(rl, "_test_t", 1000.0)
    rl._clock = lambda: t
    first = await rl._soft_stops()
    rl._test_t = t + 31.0
    rl._clock = lambda: t + 31.0
    return first + await rl._soft_stops()


def _setup_soft(test_engine, mock_broker, monkeypatch, session="ALL", bid=94.0, ask=94.2,
                age=0.0, last=94.1):
    monkeypatch.setattr(risk_loop, "order_session", lambda *a, **k: session)
    db = test_engine.db
    pid = _proposal(db, status="placed")
    db.add_trade_entry(pid, db.get_proposal(pid) | {"sl": 95.0, "tp": 110.0})
    db.update_trade_fill(pid, 100.0, "2026-01-01T00:00:00", 4, 0.0, 0.0)
    db.add_protective(pid, "AAPL", "sl", "stop-coid-1", 4, 95.0, "active", "core stop")
    mock_broker.positions = AsyncMock(return_value={"AAPL": 4.0})
    mock_broker.quote = AsyncMock(return_value=Quote("AAPL", bid=bid, ask=ask, last=last,
                                                      as_of=time.time() - age))
    mock_broker.cancel_order = AsyncMock(return_value="ok")
    mock_broker.place_order = AsyncMock(return_value="ok")
    _set(test_engine, extended_hours_soft_stop=True)
    return pid


@pytest.mark.asyncio
async def test_soft_stop_fires_once_in_premarket_even_when_killed(test_engine, mock_broker,
                                                                  monkeypatch):
    _setup_soft(test_engine, mock_broker, monkeypatch)
    test_engine.kill_switch = True
    rl = test_engine.risk_loop
    assert len(await _soft(rl)) == 1
    mock_broker.cancel_order.assert_awaited_once_with("stop-coid-1")
    mock_broker.place_order.assert_awaited_once()
    a = mock_broker.place_order.await_args
    assert a.args[1:4] == ("SELL", 4.0, 93.53)           # 94.00 * 0.995
    assert a.kwargs["trading_session"] == "ALL" and a.kwargs["allow_when_killed"] is True
    assert await rl._soft_stops() == []                  # guarded: not repeated
    mock_broker.place_order.assert_awaited_once()
    assert any(r["kind"] == "sl" and r["status"] == "active" and r["price"] == 95.0
               for r in test_engine.db.protective("AAPL"))
    assert any(e["level"] == "warn" and "SOFT STOP" in e["message"] for e in _events(test_engine.db))


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    dict(session="CORE"),                 # broker stop handles regular hours
    dict(age=120.0),                      # stale quote
    dict(bid=96.0, ask=96.2),             # above the stop
    dict(session="NIGHT"),                # overnight not enabled for AAPL
])
async def test_soft_stop_does_not_fire(test_engine, mock_broker, monkeypatch, kw):
    _setup_soft(test_engine, mock_broker, monkeypatch, **kw)
    assert await test_engine.risk_loop._soft_stops() == []
    mock_broker.place_order.assert_not_called()
    mock_broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_soft_stop_setting_off_and_last_fallback(test_engine, mock_broker, monkeypatch):
    _setup_soft(test_engine, mock_broker, monkeypatch, bid=None, ask=None, last=94.0)
    _set(test_engine, extended_hours_soft_stop=False)
    assert await test_engine.risk_loop._soft_stops() == []
    _set(test_engine, extended_hours_soft_stop=True)
    assert len(await _soft(test_engine.risk_loop)) == 1     # no bid: last is used


@pytest.mark.asyncio
async def test_soft_stop_rejection_allows_retry_later(test_engine, mock_broker, monkeypatch):
    _setup_soft(test_engine, mock_broker, monkeypatch)
    mock_broker.place_order = AsyncMock(side_effect=BrokerError("rejected"))
    rl = test_engine.risk_loop
    assert await _soft(rl) == []
    assert await rl._soft_stops() == []                  # inside the retry backoff
    assert mock_broker.place_order.await_count == 1
