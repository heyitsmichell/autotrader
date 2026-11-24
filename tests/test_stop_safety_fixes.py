"""Stop-safety fixes (verifier A: B1, S1-S7 + soft-stop positions throttle) and the entry-chase
cancel guard in track_trade_fills. Ported from the verifier's probe tests, inverted."""

import dataclasses
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.broker import BrokerError
from app.engine import risk_loop
from app.market import ET, now_utc, set_test_time
from app.quotes import Quote


def _set(engine, **kw):
    engine.s = dataclasses.replace(engine.s, **kw)
    engine.portfolio.s = engine.s


def _premarket():
    """Suite clock is 10:00 ET (CORE); most of these need the extended session."""
    set_test_time(datetime.fromisoformat("2026-11-20T08:00:00-05:00").astimezone(ET))


def _mk(db, sym, stop, fill, atr=2.0, qty=1):
    pid = db.add_proposal(None, dict(symbol=sym, side="BUY", quantity=qty, limit_price=fill,
                                     confidence=0.5, rationale="t"), "placed", sl=stop, tp=fill * 1.1)
    db.add_trade_entry(pid, dict(symbol=sym, side="BUY", quantity=qty, limit_price=fill,
                                 sl=stop, tp=fill * 1.1), {"atr": atr})
    db.update_trade_fill(pid, fill, "2026-11-20T15:00:00", qty)
    return pid


def _orders(*rows):
    out = []
    for coid, sym, side, status in rows:
        out += [f"Client Order ID: {coid}", f"  Client Order ID: {coid}", f"  Symbol: {sym}",
                f"  Side: {side}", f"  Status: {status}"]
    return "\n".join(out)


def _age_row(db, rid, seconds):
    ts = (now_utc() - timedelta(seconds=seconds)).isoformat()
    db.conn.execute("UPDATE protective_orders SET created_at=?, updated_at=? WHERE id=?",
                    (ts, ts, rid))


def _events(db):
    return [dict(level=r[0], message=r[1]) for r in
            db.conn.execute("SELECT level, message FROM events").fetchall()]


def _stale_env(engine, broker, live=328.0, held=None, cancel="ok"):
    _premarket()
    _set(engine, bracket_orders=True)
    broker.positions = AsyncMock(return_value=held or {"ADI": 1.0})
    broker.open_orders_text = AsyncMock(return_value="")
    broker.order_history_text = AsyncMock(return_value="")
    broker.cancel_order = AsyncMock(return_value=cancel)
    broker.last_price = AsyncMock(return_value=live)
    broker.place_stop = AsyncMock(return_value="ok")


# --- S3: unconfirmed cancel replies ------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    "Order not found; it may have been filled or already cancelled",
    "Permission denied; please check account permissions",
    "order may have been filled"])
async def test_unconfirmed_cancel_reply_does_not_start_reanchor(test_engine, mock_broker, reply):
    db = test_engine.db
    pid = _mk(db, "ADI", 348.45, 343.0, atr=21.1244)
    db.add_protective(pid, "ADI", "sl", "old", 1, 348.45, "active", "x")
    _stale_env(test_engine, mock_broker, cancel=reply)
    await test_engine._verify_protective_stops()
    mock_broker.cancel_order.assert_awaited_once_with("old")
    assert not db.get_kv(f"reanchor_pending:{pid}")
    mock_broker.place_stop.assert_not_called()
    assert [r["client_order_id"] for r in db.protective("ADI")] == ["old"]    # still the stop


# --- S2: per trade, not per symbol ---------------------------------------------------------------
async def test_multi_trade_symbol_only_the_stale_trade_is_touched(test_engine, mock_broker):
    db = test_engine.db
    p1 = _mk(db, "AAPL", 105.0, 100.0)      # starter whose stop is above its fill (stale)
    p2 = _mk(db, "AAPL", 104.0, 110.0)      # add-on with a legitimate stop below its fill
    db.add_protective(p1, "AAPL", "sl", "r1", 1, 105.0, "active", "x")
    db.add_protective(p2, "AAPL", "sl", "r2", 1, 104.0, "active", "x")
    _stale_env(test_engine, mock_broker, live=98.0, held={"AAPL": 2.0})
    await test_engine.portfolio.correct_stale_stops({"AAPL": 2.0})
    mock_broker.cancel_order.assert_awaited_once_with("r1")
    assert db.get_trade(p2)["stop_price"] == 104.0
    assert db.get_trade(p1)["stop_price"] == 105.0           # levels persist only once placed


async def test_profit_locking_and_valid_stops_are_not_touched(test_engine, mock_broker):
    db = test_engine.db
    p1 = _mk(db, "AAPL", 95.0, 100.0)
    db.add_protective(p1, "AAPL", "sl", "r1", 1, 95.0, "active", "x")
    _stale_env(test_engine, mock_broker, live=90.0, held={"AAPL": 1.0})
    await test_engine.portfolio.correct_stale_stops({"AAPL": 1.0})
    mock_broker.cancel_order.assert_not_called()             # stop below the fill


async def test_stop_below_live_price_is_not_stale(test_engine, mock_broker):
    db = test_engine.db
    pid = _mk(db, "ADI", 348.45, 343.0)
    db.add_protective(pid, "ADI", "sl", "old", 1, 348.45, "active", "x")
    _stale_env(test_engine, mock_broker, live=360.0)
    await test_engine.portfolio.correct_stale_stops({"ADI": 1.0})
    mock_broker.cancel_order.assert_not_called()


async def test_null_proposal_row_matches_only_the_sole_trade(test_engine, mock_broker):
    db = test_engine.db
    pid = _mk(db, "ADI", 348.45, 343.0, atr=21.1244)
    db.add_protective(None, "ADI", "sl", "adi91", 1, 348.45, "active", "x")
    _stale_env(test_engine, mock_broker)
    await test_engine.portfolio.correct_stale_stops({"ADI": 1.0})
    mock_broker.cancel_order.assert_awaited_once_with("adi91")
    assert db.get_kv(f"reanchor_pending:{pid}")
    # two trades in the symbol: an id-less row is ambiguous and left alone
    _mk(db, "MSFT", 105.0, 100.0)
    _mk(db, "MSFT", 106.0, 101.0)
    db.add_protective(None, "MSFT", "sl", "ms", 2, 105.0, "active", "x")
    mock_broker.cancel_order.reset_mock()
    await test_engine.portfolio.correct_stale_stops({"MSFT": 2.0})
    mock_broker.cancel_order.assert_not_called()


async def test_no_correction_in_core(test_engine, mock_broker):
    db = test_engine.db
    pid = _mk(db, "ADI", 348.45, 343.0)
    db.add_protective(pid, "ADI", "sl", "old", 1, 348.45, "active", "x")
    _stale_env(test_engine, mock_broker)
    set_test_time(datetime.fromisoformat("2026-11-20T10:00:00-05:00").astimezone(ET))
    await test_engine.portfolio.correct_stale_stops({"ADI": 1.0})
    mock_broker.cancel_order.assert_not_called()


# --- S2+S3: two phases ----------------------------------------------------------------------------
async def test_two_phase_waits_for_confirmed_cancel_then_places(test_engine, mock_broker):
    db = test_engine.db
    pid = _mk(db, "ADI", 348.45, 343.0, atr=21.1244)
    old = db.add_protective(pid, "ADI", "sl", "old", 1, 348.45, "active", "x")
    _stale_env(test_engine, mock_broker)
    pf = test_engine.portfolio
    await pf.correct_stale_stops({"ADI": 1.0})
    m = db.get_kv(f"reanchor_pending:{pid}")
    assert m and m["sl"] < 343.0
    await pf.correct_stale_stops({"ADI": 1.0})                    # old row still active
    mock_broker.place_stop.assert_not_called()
    # the verify pass keeps the OLD row as the armed stop and re-places nothing
    assert await test_engine._verify_protective_stops() == 0
    mock_broker.place_stop.assert_not_called()
    db.set_protective(old, "cancelled", "confirmed")
    assert await pf.correct_stale_stops({"ADI": 1.0}) == 1
    a = mock_broker.place_stop.await_args
    assert a.args[2] == m["sl"] and a.kwargs["allow_when_killed"] is True
    assert db.get_trade(pid)["stop_price"] == m["sl"]
    assert not db.get_kv(f"reanchor_pending:{pid}") and db.get_kv(f"reanchored:{pid}")


async def test_old_stop_filled_clears_marker_and_places_nothing(test_engine, mock_broker):
    db = test_engine.db
    pid = _mk(db, "ADI", 348.45, 343.0)
    old = db.add_protective(pid, "ADI", "sl", "old", 1, 348.45, "active", "x")
    _stale_env(test_engine, mock_broker)
    pf = test_engine.portfolio
    await pf.correct_stale_stops({"ADI": 1.0})
    assert db.get_kv(f"reanchor_pending:{pid}")
    db.set_protective(old, "filled")
    await pf.correct_stale_stops({"ADI": 0.0})
    mock_broker.place_stop.assert_not_called()
    assert not db.get_kv(f"reanchor_pending:{pid}")
    assert db.get_trade(pid)["stop_price"] == 348.45


async def test_failed_phase2_placement_is_retried_with_the_new_stop(test_engine, mock_broker):
    db = test_engine.db
    pid = _mk(db, "ADI", 348.45, 343.0, atr=21.1244)
    old = db.add_protective(pid, "ADI", "sl", "old", 1, 348.45, "active", "x")
    _stale_env(test_engine, mock_broker)
    pf = test_engine.portfolio
    await pf.correct_stale_stops({"ADI": 1.0})
    sl = db.get_kv(f"reanchor_pending:{pid}")["sl"]
    db.set_protective(old, "cancelled", "confirmed")
    mock_broker.place_stop = AsyncMock(side_effect=BrokerError("rejected"))
    await pf.correct_stale_stops({"ADI": 1.0})
    assert db.get_kv(f"reanchor_pending:{pid}")               # still pending
    # the periodic check re-places at the PENDING stop, not at the old (above-market) one
    mock_broker.place_stop = AsyncMock(return_value="ok")
    await test_engine._verify_protective_stops()
    assert mock_broker.place_stop.await_args.args[2] == sl


# --- S1: never widen a valid stop -----------------------------------------------------------------
async def test_reanchor_does_not_widen_when_fill_above_stop(test_engine, mock_broker):
    db = test_engine.db
    _mk(db, "TXN", 267.89, 280.41, atr=15.4793)
    _stale_env(test_engine, mock_broker, live=267.0, held={"TXN": 1.0})
    await test_engine._verify_protective_stops()
    assert mock_broker.place_stop.await_args.args[2] == 267.89   # normal stop-out, not widened


# --- B1: unknown rows ------------------------------------------------------------------------------
async def test_stale_unknown_row_is_not_armed_and_is_replaced(test_engine, mock_broker):
    db = test_engine.db
    _mk(db, "ADI", 340.0, 343.0)
    rid = db.add_protective(None, "ADI", "sl", "lost", 1, 340.0, "unknown", "transport error")
    _age_row(db, rid, 600)
    _stale_env(test_engine, mock_broker, live=345.0)
    assert await test_engine._verify_protective_stops() == 1
    assert mock_broker.place_stop.await_args.args[2] == 340.0
    rows = {r["client_order_id"]: r["status"] for r in db.protective("ADI", open_only=False)}
    assert rows["lost"] == "closed"


async def test_fresh_unknown_row_still_counts_as_armed(test_engine, mock_broker):
    db = test_engine.db
    _mk(db, "ADI", 340.0, 343.0)
    db.add_protective(None, "ADI", "sl", "fresh", 1, 340.0, "unknown", "transport error")
    _stale_env(test_engine, mock_broker, live=345.0)
    assert await test_engine._verify_protective_stops() == 0
    mock_broker.place_stop.assert_not_called()


async def test_old_unknown_row_working_at_broker_is_kept(test_engine, mock_broker):
    db = test_engine.db
    _mk(db, "ADI", 340.0, 343.0)
    rid = db.add_protective(None, "ADI", "sl", "live1", 1, 340.0, "unknown", "transport error")
    _age_row(db, rid, 7200)
    _stale_env(test_engine, mock_broker, live=345.0)
    mock_broker.open_orders_text = AsyncMock(return_value=_orders(("live1", "ADI", "SELL", "WORKING")))
    assert await test_engine._verify_protective_stops() == 0
    mock_broker.place_stop.assert_not_called()
    assert db.protective("ADI")[0]["status"] == "unknown"


async def test_unknown_row_kept_when_history_cannot_be_read(test_engine, mock_broker):
    db = test_engine.db
    _mk(db, "ADI", 340.0, 343.0)
    rid = db.add_protective(None, "ADI", "sl", "lost", 1, 340.0, "unknown", "transport error")
    _age_row(db, rid, 600)
    _stale_env(test_engine, mock_broker, live=345.0)
    mock_broker.order_history_text = AsyncMock(side_effect=BrokerError("down"))
    await test_engine._verify_protective_stops()
    mock_broker.place_stop.assert_not_called()
    assert db.protective("ADI")[0]["status"] == "unknown"


def test_sync_gives_up_on_unknown_rows_after_the_reconcile_window(test_engine):
    db, pf = test_engine.db, test_engine.portfolio
    give_up = test_engine.s.reconcile_give_up_minutes * 60
    a = db.add_protective(None, "ADI", "sl", "a", 1, 340.0, "unknown", "x")
    b = db.add_protective(None, "ADI", "sl", "b", 1, 340.0, "unknown", "x")
    c = db.add_protective(None, "ADI", "sl", "c", 1, 340.0, "unknown", "x")
    _age_row(db, a, give_up + 60)
    _age_row(db, b, give_up - 120)
    _age_row(db, c, give_up + 60)
    pf._sync({"c": {"client_order_id": "c", "status": "WORKING"}}, {})   # c is working: kept
    st = {r["client_order_id"]: r["status"] for r in db.protective("ADI", open_only=False)}
    assert st == {"a": "closed", "b": "unknown", "c": "active"}


# --- soft stop --------------------------------------------------------------------------------------
def _soft_env(engine, broker, monkeypatch, positions, last=94.0, session="ALL", **quote_kw):
    monkeypatch.setattr(risk_loop, "order_session", lambda *a, **k: session)
    broker.positions = AsyncMock(return_value=positions)
    broker.quote = AsyncMock(return_value=Quote(positions and next(iter(positions)), bid=None,
                                                ask=None, last=last, as_of=time.time(), **quote_kw))
    broker.open_orders_text = AsyncMock(return_value="")
    broker.order_history_text = AsyncMock(return_value="")
    broker.cancel_order = AsyncMock(return_value="ok")
    broker.place_order = AsyncMock(return_value="ok")
    broker.place_stop = AsyncMock(return_value="ok")
    _set(engine, extended_hours_soft_stop=True)


class _Clock:
    def __init__(self, rl, t=1000.0):
        self.rl, self.t = rl, t
        rl._clock = lambda: self.t

    async def tick(self, dt=0.0):
        self.t += dt
        return await self.rl._soft_stops()


async def test_soft_stop_one_sell_per_symbol_sized_to_held(test_engine, mock_broker, monkeypatch):
    db = test_engine.db
    p1 = _mk(db, "AAPL", 95.0, 100.0, qty=2)
    p2 = _mk(db, "AAPL", 99.0, 104.0, qty=2)
    db.add_protective(p1, "AAPL", "sl", "r1", 2, 95.0, "active", "x")
    db.add_protective(p2, "AAPL", "sl", "r2", 2, 99.0, "active", "x")
    _soft_env(test_engine, mock_broker, monkeypatch, {"AAPL": 4.0})
    clk = _Clock(test_engine.risk_loop)
    assert await clk.tick() == []
    fired = await clk.tick(31)
    assert len(fired) == 1
    assert [c.args[2] for c in mock_broker.place_order.await_args_list] == [4.0]
    assert await clk.tick(40) == []                      # the sibling trade does not fire again
    assert mock_broker.place_order.await_count == 1
    assert mock_broker.place_order.await_args.kwargs["allow_when_killed"] is True


async def test_soft_stop_skips_when_a_foreign_sell_is_working(test_engine, mock_broker, monkeypatch):
    db = test_engine.db
    p1 = _mk(db, "AAPL", 95.0, 100.0, qty=2)
    db.add_protective(p1, "AAPL", "sl", "r1", 2, 95.0, "active", "x")
    _soft_env(test_engine, mock_broker, monkeypatch, {"AAPL": 2.0})
    mock_broker.open_orders_text = AsyncMock(return_value=_orders(
        ("r1", "AAPL", "SELL", "WORKING"), ("manual", "AAPL", "SELL", "WORKING")))
    clk = _Clock(test_engine.risk_loop)
    await clk.tick()
    assert await clk.tick(31) == []
    mock_broker.place_order.assert_not_called()
    mock_broker.cancel_order.assert_not_called()
    # only our own CORE stop is working: it is cancelled and the soft stop fires
    mock_broker.open_orders_text = AsyncMock(return_value=_orders(("r1", "AAPL", "SELL", "WORKING")))
    assert len(await clk.tick(10)) == 1


async def test_soft_stop_needs_two_readings_30s_apart(test_engine, mock_broker, monkeypatch):
    db = test_engine.db
    p1 = _mk(db, "AAPL", 95.0, 100.0)
    db.add_protective(p1, "AAPL", "sl", "r1", 1, 95.0, "active", "x")
    _soft_env(test_engine, mock_broker, monkeypatch, {"AAPL": 1.0})
    clk = _Clock(test_engine.risk_loop)
    assert await clk.tick() == []                        # one print is not enough
    assert await clk.tick(10) == []                      # second reading but < 30 s
    mock_broker.place_order.assert_not_called()
    # price recovers: the count restarts
    mock_broker.quote = AsyncMock(return_value=Quote("AAPL", last=96.0, as_of=time.time()))
    assert await clk.tick(10) == []
    mock_broker.quote = AsyncMock(return_value=Quote("AAPL", last=94.0, as_of=time.time()))
    assert await clk.tick(40) == []                      # reading 1 again
    assert len(await clk.tick(31)) == 1


async def test_soft_stop_ignores_stale_quotes(test_engine, mock_broker, monkeypatch):
    db = test_engine.db
    p1 = _mk(db, "AAPL", 95.0, 100.0)
    db.add_protective(p1, "AAPL", "sl", "r1", 1, 95.0, "active", "x")
    _soft_env(test_engine, mock_broker, monkeypatch, {"AAPL": 1.0}, stale=True)
    clk = _Clock(test_engine.risk_loop)
    await clk.tick()
    assert await clk.tick(60) == []
    mock_broker.place_order.assert_not_called()


async def test_soft_stop_throttles_positions(test_engine, mock_broker, monkeypatch):
    db = test_engine.db
    p1 = _mk(db, "AAPL", 95.0, 100.0)
    db.add_protective(p1, "AAPL", "sl", "r1", 1, 95.0, "active", "x")
    _soft_env(test_engine, mock_broker, monkeypatch, {"AAPL": 1.0}, last=99.0)
    clk = _Clock(test_engine.risk_loop)
    for _ in range(4):
        await clk.tick(1.0)
    assert mock_broker.positions.await_count == 1
    await clk.tick(6.0)
    assert mock_broker.positions.await_count == 2


# --- S5: an unfilled soft-stop sell must not leave the position bare into CORE ------------------
def _fired_soft(engine, broker, monkeypatch, session="CORE"):
    db = engine.db
    pid = _mk(db, "AAPL", 95.0, 100.0)
    tid = db.get_trade(pid)["id"]
    db.add_protective(pid, "AAPL", "sl", "soft1", 1, 95.0, "active",
                      "extended-hours soft stop: ALL limit sell")
    db.set_kv(f"soft_stop:{tid}", {"coid": "soft1", "at": time.time()})
    _soft_env(engine, broker, monkeypatch, {"AAPL": 1.0}, session=session)
    broker.last_price = AsyncMock(return_value=100.0)
    return pid, tid


async def test_unfilled_soft_sell_is_cancelled_at_core_then_stop_restored(
        test_engine, mock_broker, monkeypatch):
    pid, tid = _fired_soft(test_engine, mock_broker, monkeypatch)
    mock_broker.open_orders_text = AsyncMock(return_value=_orders(("soft1", "AAPL", "SELL", "WORKING")))
    clk = _Clock(test_engine.risk_loop)
    await clk.tick()
    mock_broker.cancel_order.assert_awaited_once_with("soft1")
    mock_broker.place_stop.assert_not_called()                 # not before the cancel is confirmed
    mock_broker.open_orders_text = AsyncMock(return_value="")
    mock_broker.order_history_text = AsyncMock(return_value=_orders(("soft1", "AAPL", "SELL", "CANCELLED")))
    await clk.tick(6)
    a = mock_broker.place_stop.await_args
    assert a.args[:3] == ("AAPL", 1.0, 95.0) and a.kwargs["allow_when_killed"] is True
    assert test_engine.db.get_kv(f"soft_stop:{tid}")["settled"]
    await clk.tick(6)
    assert mock_broker.place_stop.await_count == 1


async def test_unfilled_soft_sell_exits_when_price_is_at_or_below_the_stop(
        test_engine, mock_broker, monkeypatch):
    _fired_soft(test_engine, mock_broker, monkeypatch)
    mock_broker.last_price = AsyncMock(return_value=93.0)
    mock_broker.order_history_text = AsyncMock(return_value=_orders(("soft1", "AAPL", "SELL", "EXPIRED")))
    test_engine.kill_switch = True
    await _Clock(test_engine.risk_loop).tick()
    mock_broker.place_stop.assert_not_called()
    a = mock_broker.place_order.await_args
    assert a.args[1:3] == ("SELL", 1.0) and a.kwargs["allow_when_killed"] is True
    assert a.kwargs["trading_session"] == "CORE"


async def test_filled_soft_sell_needs_nothing(test_engine, mock_broker, monkeypatch):
    _fired_soft(test_engine, mock_broker, monkeypatch, session="ALL")
    mock_broker.order_history_text = AsyncMock(return_value=_orders(("soft1", "AAPL", "SELL", "FILLED")))
    await _Clock(test_engine.risk_loop).tick()
    mock_broker.place_stop.assert_not_called()
    mock_broker.place_order.assert_not_called()


async def test_soft_sell_ending_unfilled_in_extended_hours_restores_the_stop(
        test_engine, mock_broker, monkeypatch):
    _fired_soft(test_engine, mock_broker, monkeypatch, session="ALL")
    mock_broker.order_history_text = AsyncMock(return_value=_orders(("soft1", "AAPL", "SELL", "CANCELLED")))
    await _Clock(test_engine.risk_loop).tick()
    assert mock_broker.place_stop.await_args.args[:3] == ("AAPL", 1.0, 95.0)


# --- S7: no sell stop above the market in CORE ------------------------------------------------
async def test_core_stop_above_market_exits_instead_once(test_engine, mock_broker):
    db = test_engine.db
    _set(test_engine, bracket_orders=True)
    set_test_time(datetime.fromisoformat("2026-11-20T10:00:00-05:00").astimezone(ET))
    mock_broker.place_stop = AsyncMock(return_value="ok")
    mock_broker.place_order = AsyncMock(return_value="ok")
    mock_broker.last_price = AsyncMock(return_value=94.0)
    test_engine.kill_switch = True
    pf = test_engine.portfolio
    trade = {"symbol": "AAPL", "side": "BUY", "stop_price": 95.0, "proposal_id": 11}
    await pf._protect_filled_entry(trade, 3, {"AAPL": 3.0}, set())
    mock_broker.place_stop.assert_not_called()
    a = mock_broker.place_order.await_args
    assert a.args[1:3] == ("SELL", 3.0) and a.kwargs["allow_when_killed"] is True
    assert a.kwargs["trading_session"] == "CORE"
    assert any("EXIT" in e["message"] for e in _events(db))
    await pf._protect_filled_entry(trade, 3, {"AAPL": 3.0}, set())
    assert mock_broker.place_order.await_count == 1            # once


async def test_core_missing_stop_above_market_exits_via_verify(test_engine, mock_broker):
    db = test_engine.db
    _mk(db, "AAPL", 95.0, 100.0)
    _stale_env(test_engine, mock_broker, live=94.0, held={"AAPL": 1.0})
    set_test_time(datetime.fromisoformat("2026-11-20T10:00:00-05:00").astimezone(ET))
    mock_broker.place_order = AsyncMock(return_value="ok")
    await test_engine._verify_protective_stops()
    mock_broker.place_stop.assert_not_called()
    assert mock_broker.place_order.await_count == 1
    await test_engine._verify_protective_stops()
    assert mock_broker.place_order.await_count == 1


# --- entry chase: do not cancel the ledger row mid cancel-and-resend ------------------------------
async def _unfilled_entry(engine, broker):
    db = engine.db
    pid = db.add_proposal(None, dict(symbol="AAPL", side="BUY", quantity=1, limit_price=100.0,
                                     confidence=0.5, rationale="t"), "placed", sl=95.0, tp=110.0,
                          client_order_id="ent1")
    db.add_trade_entry(pid, dict(symbol="AAPL", side="BUY", quantity=1, limit_price=100.0,
                                 sl=95.0, tp=110.0))
    broker.open_orders_text = AsyncMock(return_value="")
    broker.order_history_text = AsyncMock(return_value=_orders(("ent1", "AAPL", "BUY", "CANCELLED")))
    broker.positions = AsyncMock(return_value={})
    return pid


async def test_track_fills_skips_entry_mid_chase_cancel(test_engine, mock_broker):
    db = test_engine.db
    pid = await _unfilled_entry(test_engine, mock_broker)
    db.set_kv(f"chase_cancel:{pid}", {"at": time.time()})
    await test_engine.portfolio.track_trade_fills()
    assert db.get_trade(pid)["status"] == "open"
    db.set_kv(f"chase:{pid}", {"coid": "ent2"})                # chaser finished its resend
    await test_engine.portfolio.track_trade_fills()
    assert db.get_trade(pid)["status"] != "open"


async def test_track_fills_cancels_unfilled_entry_without_chase_marker(test_engine, mock_broker):
    db = test_engine.db
    pid = await _unfilled_entry(test_engine, mock_broker)
    await test_engine.portfolio.track_trade_fills()
    assert db.get_trade(pid)["status"] != "open"
