"""Unfilled entries must leave the ledger; only session errors may drop a bracket's exits."""
from datetime import timedelta

import pytest

from app.broker import BrokerError
from app.engine import _session_rejection
from app.market import now_utc


@pytest.mark.parametrize("msg,expected", [
    ("OPENAPI_PARAM_ERR: invalid support_trading_session", True),
    ("[OPENAPI_OVERNIGHT_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS] Overnight Trading", True),
    ("Order error: session not supported for combo outside regular hours", True),
    ("[OPENAPI_CORE_SESSION_CLOSED] Regular session is closed.", True),
    ("HTTP 417 OPENAPI_GTX_NOT_SUPPORT_MARKET: This order type does not support Include Extended Hours", True),
    ("limit price is outside the allowed price band", False),
    ("order size outside the allowed range", False),
    ("fractional quantities not supported in combo orders", False),
    ("insufficient buying power", False),
    ("login session expired", False),
])
def test_session_rejection_is_specific(msg, expected):
    assert _session_rejection(BrokerError(msg)) is expected


class FakeBroker:
    def __init__(self, history="", open_text="", held=None):
        self.history, self.open_text, self.held = history, open_text, held or {}

    async def open_orders_text(self): return self.open_text
    async def order_history_text(self): return self.history
    async def positions(self): return self.held


def _order(coid, status, filled="0", price=""):
    """Order text in the format the Webull MCP server prints (see portfolio.parse_orders)."""
    lines = [f"Client Order ID: {coid}", f"  Client Order ID: {coid}", f"  Order ID: O{coid}",
             "  Symbol: AAPL", "  Side: BUY", f"  Status: {status}", "  Total Quantity: 2",
             f"  Filled Quantity: {filled}", "  Limit Price: 100.0"]
    if price:
        lines.append(f"  Average Fill Price: {price}")
    return "\n".join(lines) + "\n"


def _setup(test_db, test_settings, broker, age_hours=1.0):
    from app.portfolio import Portfolio
    p = {"symbol": "AAPL", "side": "BUY", "quantity": 2, "limit_price": 100.0}
    pid = test_db.add_proposal(None, p, "placed", "test", 100.0)
    test_db.update_proposal(pid, client_order_id="coid1")
    test_db.add_trade_entry(pid, p)
    test_db.conn.execute("UPDATE trade_ledger SET created_at=? WHERE proposal_id=?",
                         ((now_utc() - timedelta(hours=age_hours)).isoformat(timespec="seconds"), pid))
    return pid, Portfolio(test_settings, test_db, broker)


@pytest.mark.asyncio
async def test_expired_unfilled_entry_is_closed(test_db, test_settings):
    pid, pf = _setup(test_db, test_settings, FakeBroker(history=_order("coid1", "EXPIRED")))
    assert await pf.track_trade_fills() == 1
    assert test_db.open_trades() == []
    row = test_db.trades(status="cancelled")[0]
    assert row["exit_reason"] == "entry_expired" and row["realized_pnl"] is None


@pytest.mark.asyncio
async def test_partial_fill_then_cancel_keeps_the_fill(test_db, test_settings):
    pid, pf = _setup(test_db, test_settings,
                     FakeBroker(history=_order("coid1", "CANCELLED", filled="1", price="99.5")))
    await pf.track_trade_fills()
    row = test_db.open_trades()[0]
    assert row["entry_fill_price"] == 99.5 and row["quantity_filled_real"] == 1


@pytest.mark.asyncio
async def test_working_order_is_left_alone(test_db, test_settings):
    pid, pf = _setup(test_db, test_settings, FakeBroker(open_text=_order("coid1", "WORKING")))
    assert await pf.track_trade_fills() == 0
    assert len(test_db.open_trades()) == 1


@pytest.mark.asyncio
async def test_order_missing_for_over_36h_without_position_is_closed(test_db, test_settings):
    pid, pf = _setup(test_db, test_settings, FakeBroker(), age_hours=40)
    await pf.track_trade_fills()
    assert test_db.open_trades() == []
    assert test_db.trades(status="cancelled")[0]["exit_reason"] == "entry_not_found"


@pytest.mark.asyncio
async def test_missing_order_with_a_position_is_kept(test_db, test_settings):
    pid, pf = _setup(test_db, test_settings, FakeBroker(held={"AAPL": 2.0}), age_hours=40)
    await pf.track_trade_fills()
    assert len(test_db.open_trades()) == 1


@pytest.mark.asyncio
async def test_recent_missing_order_is_kept(test_db, test_settings):
    pid, pf = _setup(test_db, test_settings, FakeBroker(), age_hours=2)
    await pf.track_trade_fills()
    assert len(test_db.open_trades()) == 1