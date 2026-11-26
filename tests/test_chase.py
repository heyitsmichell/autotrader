import time
from unittest.mock import AsyncMock, MagicMock
import pytest
from app import exits
from app.broker import BrokerError
from app.engine import chase
from app.market import set_test_time, ET
from app.risk import OptionRiskManager
from test_market_orders import _chase_setup, _proposal, _set

@pytest.fixture(autouse=True)
def _clock():
    yield
    from conftest import TEST_TIME_ET
    set_test_time(TEST_TIME_ET.astimezone(ET))


# --- C1 ------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c1_marker_cleared_after_successful_chase(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    assert await chase.EntryChaser(test_engine).run_once() == [pid]
    assert not test_engine.db.get_kv(f"chase_cancel:{pid}")


@pytest.mark.asyncio
async def test_c1_marker_cleared_when_cancel_unconfirmed(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch, confirm=False)
    assert await chase.EntryChaser(test_engine).run_once() == []
    assert not test_engine.db.get_kv(f"chase_cancel:{pid}")
    # the order is cancelled later (manually / expiry): the stale marker must not resume the chase
    book.orders["old-1"] = ("CANCELLED", 0)
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_c1_marker_cleared_when_cancel_raises(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    mock_broker.cancel_order = AsyncMock(side_effect=BrokerError("boom"))
    assert await chase.EntryChaser(test_engine).run_once() == []
    assert not test_engine.db.get_kv(f"chase_cancel:{pid}")


@pytest.mark.asyncio
async def test_c1_unconfirmed_marker_does_not_resume(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    book.orders["old-1"] = ("CANCELLED", 0)
    test_engine.db.set_kv(f"chase_cancel:{pid}", {"coid": "old-1", "at": time.time()})
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_awaited()
    assert not test_engine.db.get_kv(f"chase_cancel:{pid}")


@pytest.mark.asyncio
async def test_c1_confirmed_marker_resumes_but_expired_never(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    book.orders["old-1"] = ("EXPIRED", 0)
    test_engine.db.set_kv(f"chase_cancel:{pid}", {"at": time.time(), "confirmed_at": time.time()})
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_awaited()
    book.orders["old-1"] = ("CANCELLED", 0)
    test_engine.db.set_kv(f"chase_cancel:{pid}", {"at": time.time(), "confirmed_at": time.time()})
    assert await chase.EntryChaser(test_engine).run_once() == [pid]


@pytest.mark.asyncio
async def test_c1_refuses_order_placed_before_todays_open(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch, age=30 * 86400)
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.cancel_order.assert_not_awaited()
    mock_broker.place_order.assert_not_awaited()


# --- C2 ------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c2_chase_shrinks_qty_to_fit_cap_at_new_price(test_engine, mock_broker, monkeypatch):
    # 4 x 100.0 = 400 fills the cap exactly; 4 x 100.2 does not -> shrink to 3
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    _set(test_engine, max_order_usd=400.0)
    assert await chase.EntryChaser(test_engine).run_once() == [pid]
    assert mock_broker.place_order.await_args.args[2] == 3
    assert test_engine.db.get_proposal(pid)["quantity"] == 3
    assert test_engine.db.get_trade(pid)["quantity_requested"] == 3


@pytest.mark.asyncio
async def test_c2_plan_none_sets_retry_delay(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    _set(test_engine, max_order_usd=50.0)
    ch = chase.EntryChaser(test_engine)
    assert await ch.run_once() == []
    assert ch._retry_at[pid] > time.monotonic()
    calls = mock_broker.quote.await_count
    assert await ch.run_once() == []
    assert mock_broker.quote.await_count == calls          # no re-quote on the next tick


# --- C6 / C17 ------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c6_failed_positions_read_keeps_profit_state(test_engine, mock_broker):
    db = test_engine.db
    db.set_kv("profit_state_symbols", ["AAPL"])
    db.set_kv("hwm:AAPL", 123.0)
    db.set_kv("scaled:AAPL", 1)
    mock_broker.positions = AsyncMock(side_effect=BrokerError("down"))
    await test_engine._generate_proposals_deterministic(db.create_run("t"), [])
    assert db.get_kv("hwm:AAPL") == 123.0 and db.get_kv("scaled:AAPL") == 1
    assert db.get_kv("profit_state_symbols") == ["AAPL"]


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_c17_unfilled_rows_do_not_reset_entry_time(test_engine, mock_broker, monkeypatch):
    db = test_engine.db
    filled = _proposal(db, status="placed")
    db.add_trade_entry(filled, db.get_proposal(filled), None)
    db.conn.execute("UPDATE trade_ledger SET entry_fill_price=100, "
                    "entry_fill_time='2026-01-02T10:00:00+00:00' WHERE proposal_id=?", (filled,))
    unfilled = _proposal(db, status="placed")
    db.add_trade_entry(unfilled, db.get_proposal(unfilled), None)
    mock_broker.positions = AsyncMock(return_value={"AAPL": 4.0})
    test_engine.signals.ensure = AsyncMock(return_value={"symbol": "AAPL"})
    seen = {}

    def fake_held_days(stamp, now):
        seen["stamp"] = stamp
        raise _Stop

    monkeypatch.setattr(exits, "held_days", fake_held_days)
    with pytest.raises(_Stop):
        await test_engine._generate_proposals_deterministic(db.create_run("t"), [])
    assert seen["stamp"] == "2026-01-02T10:00:00+00:00"


# --- S14 -----------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s14_opening_option_blocked_by_daily_loss_and_trade_count(test_engine):
    _set(test_engine, max_daily_loss_usd=1000.0, max_trades_per_day=2)
    rm = OptionRiskManager(test_engine.s)
    broker = MagicMock()
    broker.day_pnl = AsyncMock(return_value=-1500.0)
    p = {"position_intent": "BUY_TO_OPEN"}
    ok, why = await rm.check_option_open_account(p, broker, test_engine.db)
    assert not ok and "daily loss" in why[0]
    # closing orders are never blocked
    ok, _ = await rm.check_option_open_account({"position_intent": "SELL_TO_CLOSE"}, broker,
                                               test_engine.db)
    assert ok
    broker.day_pnl = AsyncMock(return_value=0.0)
    ok, _ = await rm.check_option_open_account(p, broker, test_engine.db)
    assert ok
    for _i in range(2):         # S14 counts OPTION entries only (verifier round 2)
        pid = _proposal(test_engine.db, status="placed")
        test_engine.db.update_proposal(pid, asset_class="option")
    ok, why = await rm.check_option_open_account(p, broker, test_engine.db)
    assert not ok and "daily limit" in why[0]
    broker.day_pnl = AsyncMock(return_value=None)
    _set(test_engine, max_trades_per_day=0)
    rm = OptionRiskManager(test_engine.s)
    ok, why = await rm.check_option_open_account(p, broker, test_engine.db)
    assert not ok and "unavailable" in why[0]
