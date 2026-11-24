"""Stop re-anchoring when an entry fills at/below the planned stop (ADI 2026-11-24)."""

import dataclasses
from unittest.mock import AsyncMock

import pytest

from app.broker import BrokerError


def _set(engine, **kw):
    engine.s = dataclasses.replace(engine.s, **kw)
    engine.portfolio.s = engine.s


def _premarket():
    """The suite clock is 10:00 ET (CORE); stale-stop correction and plain stop placement above
    the market only happen outside CORE."""
    from datetime import datetime
    from app.market import ET, set_test_time
    set_test_time(datetime.fromisoformat("2026-11-20T08:00:00-05:00").astimezone(ET))


def _events(db):
    """Events as dicts; alert is True when the message was also sent to the alert channels."""
    if not getattr(db, "_alert_msgs", None):
        db._alert_msgs = set()
        db.listeners.append(lambda lv, m, a: db._alert_msgs.add(m) if a else None)
    return [dict(level=r[0], message=r[1], alert=r[1] in db._alert_msgs) for r in
            db.conn.execute("SELECT level, message FROM events").fetchall()]


def _mk(db, atr=2.0, stop=348.45, fill=343.0, sym="ADI"):
    pid = db.add_proposal(None, dict(symbol=sym, side="BUY", quantity=1, limit_price=378.75,
                                     confidence=0.5, rationale="t"), "placed", sl=stop, tp=420.0)
    db.add_trade_entry(pid, dict(symbol=sym, side="BUY", quantity=1, limit_price=378.75,
                                 sl=stop, tp=420.0), {"atr": atr} if atr else None)
    if fill:
        db.update_trade_fill(pid, fill, "2026-11-24T10:00:00", 1)
    return pid, db.get_trade(pid)


async def _run(engine, broker, trade, fill, live, held=1.0):
    _premarket()
    _set(engine, bracket_orders=True)
    broker.place_stop = AsyncMock(return_value="ok")
    broker.last_price = AsyncMock(return_value=live)
    await engine.portfolio._protect_filled_entry(trade, 1, {trade["symbol"]: held}, set(), fill)


@pytest.mark.asyncio
async def test_normal_fill_unchanged(test_engine, mock_broker):
    pid, t = _mk(test_engine.db, stop=95.0, fill=None, sym="AAPL")
    await _run(test_engine, mock_broker, t, 100.0, 100.0)
    assert mock_broker.place_stop.await_args.args[2] == 95.0
    assert test_engine.db.get_trade(pid)["stop_price"] == 95.0
    assert not test_engine.db.get_kv(f"reanchored:{pid}")


@pytest.mark.asyncio
async def test_fill_below_stop_reanchors(test_engine, mock_broker):
    db = test_engine.db
    _events(db)
    pid, t = _mk(db, fill=None)
    await _run(test_engine, mock_broker, t, 343.0, 328.0)
    s = test_engine.s
    from app import ta
    sl, tp = ta.levels(343.0, 2.0, s)
    assert mock_broker.place_stop.await_args.args[2] == sl < 343.0
    row = db.get_trade(pid)
    assert (row["stop_price"], row["target_price"]) == (sl, tp)
    ev = [e for e in _events(db) if e["alert"] and "re-anchored" in e["message"]]
    assert ev and "ADI: fill $343.00 was below the planned stop $348.45" in ev[0]["message"]
    # live (328) below the new stop (clamped distance) -> extra alert, stop still placed
    assert any("already below the re-anchored stop" in e["message"] for e in _events(db))


@pytest.mark.asyncio
async def test_atr_fallbacks(test_engine, mock_broker):
    db = test_engine.db
    # signal-store ATR
    pid, t = _mk(db, atr=None, fill=None)
    db.add_signal(None, {"symbol": "ADI", "atr": 4.0})
    pf = test_engine.portfolio
    from app import ta
    assert pf._reanchored_levels(t, 343.0) == ta.levels(343.0, 4.0, test_engine.s)
    # no ATR anywhere -> fill * (1 - MAX_SL_PCT/100)
    pid2, t2 = _mk(db, atr=None, fill=None, sym="ZZZ")
    sl, _tp = pf._reanchored_levels(t2, 100.0)
    assert sl == round(100 * (1 - test_engine.s.max_sl_pct / 100), 2)


@pytest.mark.asyncio
async def test_no_upward_reanchor(test_engine, mock_broker):
    db = test_engine.db
    # live below the stop but fill far above: re-anchoring from the fill would raise the stop
    pid, t = _mk(db, atr=50.0, stop=90.0, fill=None, sym="AAPL")
    await _run(test_engine, mock_broker, t, 100.0, 85.0)
    assert mock_broker.place_stop.await_args.args[2] == 90.0
    assert db.get_trade(pid)["stop_price"] == 90.0


@pytest.mark.asyncio
async def test_reconcile_uses_reanchored_stop(test_engine, mock_broker):
    db = test_engine.db
    pid, t = _mk(db)                                  # ledger stop 348.45, fill 343
    _set(test_engine, bracket_orders=True)
    mock_broker.positions = AsyncMock(return_value={"ADI": 1.0})
    mock_broker.open_orders_text = AsyncMock(return_value="")
    mock_broker.last_price = AsyncMock(return_value=340.0)
    mock_broker.place_stop = AsyncMock(return_value="ok")
    assert await test_engine._verify_protective_stops() == 1
    assert mock_broker.place_stop.await_args.args[2] < 343.0
    assert db.get_trade(pid)["stop_price"] < 343.0


@pytest.mark.asyncio
async def test_existing_stale_stop_replaced(test_engine, mock_broker):
    """Two phases: pass 1 only cancels (+ marker); the new stop goes in once the old row is
    cancelled."""
    _premarket()
    db = test_engine.db
    pid, t = _mk(db)
    old = db.add_protective(pid, "ADI", "sl", "old1", 1.0, 348.45, "active", "x")
    _set(test_engine, bracket_orders=True)
    mock_broker.positions = AsyncMock(return_value={"ADI": 1.0})
    mock_broker.open_orders_text = AsyncMock(return_value="")
    mock_broker.cancel_order = AsyncMock(return_value="ok")
    mock_broker.last_price = AsyncMock(return_value=328.0)
    mock_broker.place_stop = AsyncMock(return_value="ok")
    await test_engine._verify_protective_stops()
    mock_broker.cancel_order.assert_awaited_once_with("old1")
    mock_broker.place_stop.assert_not_called()                 # not before the cancel is confirmed
    assert db.get_kv(f"reanchor_pending:{pid}")
    assert db.get_trade(pid)["stop_price"] == 348.45           # levels persist only when placed
    db.set_protective(old, "cancelled", "broker confirmed")    # what _sync does
    await test_engine._verify_protective_stops()
    assert mock_broker.place_stop.await_args.args[2] < 343.0
    rows = db.protective("ADI")
    assert len(rows) == 1 and rows[0]["price"] < 343.0
    assert db.get_trade(pid)["stop_price"] < 343.0
    assert not db.get_kv(f"reanchor_pending:{pid}") and db.get_kv(f"reanchored:{pid}")
    await test_engine._verify_protective_stops()               # already re-anchored: nothing more
    mock_broker.cancel_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_stale_stop_cancel_fails_alerts_once(test_engine, mock_broker):
    _premarket()
    db = test_engine.db
    _events(db)
    pid, t = _mk(db)
    db.add_protective(pid, "ADI", "sl", "old1", 1.0, 348.45, "active", "x")
    _set(test_engine, bracket_orders=True)
    mock_broker.positions = AsyncMock(return_value={"ADI": 1.0})
    mock_broker.open_orders_text = AsyncMock(return_value="")
    mock_broker.last_price = AsyncMock(return_value=328.0)
    mock_broker.cancel_order = AsyncMock(side_effect=BrokerError("no reply", transport=True))
    mock_broker.place_stop = AsyncMock(return_value="ok")
    await test_engine._verify_protective_stops()
    await test_engine._verify_protective_stops()
    mock_broker.place_stop.assert_not_called()
    assert db.protective("ADI")[0]["client_order_id"] == "old1"
    assert len([e for e in _events(db) if e["alert"] and "cancel was not confirmed" in e["message"]]) == 1
