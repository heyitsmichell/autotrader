"""Order-style fixes: chase/fill-tracker race, ambiguous MARKET errors, unconfirmed cancels,
leftover extended/overnight prints, re-price risk checks, kill switch applies to openings only."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app import order_style
from app.broker import BrokerError
from app.broker.parsers import live_price_for_session, parse_snapshot
from app.engine import chase
from app.market import ET, set_test_time
from app.risk import reprice_violation
from test_market_orders import _chase_setup, _proposal, _set, _stub_sell


@pytest.fixture(autouse=True)
def _clock():
    yield
    from conftest import TEST_TIME_ET
    set_test_time(TEST_TIME_ET.astimezone(ET))


# --- 1. chase vs fill tracker -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chase_reopens_ledger_row_cancelled_by_fill_tracker(test_engine, mock_broker,
                                                                  monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    db = test_engine.db
    real_cancel = mock_broker.cancel_order.side_effect

    async def cancel(coid):
        r = await real_cancel(coid)
        db.cancel_trade(pid, "entry_cancelled")      # what track_trade_fills does
        return r
    mock_broker.cancel_order = AsyncMock(side_effect=cancel)
    assert await chase.EntryChaser(test_engine).run_once() == [pid]
    assert mock_broker.place_order.await_count == 1
    assert db.get_trade(pid)["status"] == "open"
    assert db.get_trade(pid)["exit_reason"] is None
    assert [t for t in db.open_trades() if t["proposal_id"] == pid]


@pytest.mark.asyncio
async def test_chase_does_not_send_when_ledger_row_cannot_reopen(test_engine, mock_broker,
                                                                 monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    db = test_engine.db
    db.conn.execute("DELETE FROM trade_ledger WHERE proposal_id=?", (pid,))
    prop = db.get_proposal(pid)
    ok = await chase.EntryChaser(test_engine)._resend({}, prop, 100.2)
    assert ok is False
    mock_broker.place_order.assert_not_awaited()


# --- 2. ambiguous MARKET errors never trigger a second SELL -------------------------------------
@pytest.mark.parametrize("msg", [
    "place_stock_order: Internal error: ReadTimeout: read timed out",
    "Server error (HTTP 502): [X] bad gateway",
])
@pytest.mark.asyncio
async def test_ambiguous_market_error_is_unknown_not_resent(test_engine, mock_broker, msg):
    _set(test_engine, exit_order_type="market")
    _stub_sell(test_engine, mock_broker)
    mock_broker.place_order = AsyncMock(side_effect=[BrokerError(msg), "client_order_id: y"])
    db = test_engine.db
    pid = _proposal(db, side="SELL")
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    assert mock_broker.place_order.await_count == 1
    assert db.get_proposal(pid)["status"] == "unknown"


def test_market_rejection_classification():
    D = order_style.market_rejection_is_definitive
    assert D(BrokerError("Validation error: bad"))
    assert D(BrokerError("Parameter error: [X] y"))
    assert D(BrokerError("OPENAPI_GTX_NOT_SUPPORT_MARKET"))
    assert D(BrokerError("market not allowed"))
    assert not D(BrokerError("Server error (HTTP 503): x"))
    assert not D(BrokerError("Internal error: ReadTimeout: t"))
    assert not D(BrokerError("Parameter error", transport=True))


# --- 3. unconfirmed cancel replies --------------------------------------------------------------
@pytest.mark.parametrize("reply", ["Order not found; it may have been filled or already cancelled",
                                   "Permission denied; please check account permissions"])
def test_cancel_reply_not_confirmed(reply):
    assert not order_style.cancel_reply_confirmed(reply)
    assert order_style.cancel_reply_confirmed("Order cancelled")


@pytest.mark.asyncio
async def test_chase_does_not_resend_on_unconfirmed_cancel_reply(test_engine, mock_broker,
                                                                 monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    mock_broker.cancel_order = AsyncMock(return_value="Order not found; it may have been filled "
                                                      "or already cancelled")
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sell_not_sent_when_stop_cancel_unconfirmed(test_engine, mock_broker):
    _stub_sell(test_engine, mock_broker)
    db = test_engine.db
    db.add_protective(None, "AAPL", "sl", "stop-1", 4, 95.0, "active", "t")
    mock_broker.cancel_order = AsyncMock(return_value="Permission denied; please check account "
                                                      "permissions")
    mock_broker.place_order = AsyncMock(return_value="client_order_id: y")
    pid = _proposal(db, side="SELL")
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    mock_broker.place_order.assert_not_awaited()
    assert db.get_proposal(pid)["status"] == "failed"


# --- 4. leftover extended / overnight prints ----------------------------------------------------
_SNAP = """=== Stock Snapshot ===
       BRK.B  Price:   100.00  PreClose:   99.00  Change: 1  Change%: 1%
            Open: N/A  High: N/A  Low: N/A  Close: N/A  Vol: N/A
            Bid: N/A x N/A  Ask: N/A x N/A
            OVN Price:     {ovn}  High: N/A  Low: N/A  Change: N/A (N/A)  Vol: N/A
            ExtHr Price:   {ext}  High: 1  Low: 1  Change: 1 (1)  Vol: 1,000
"""


def _snap(ovn="--", ext="--"):
    return parse_snapshot(_SNAP.format(ovn=ovn, ext=ext))["BRK.B"]


def _at(h, m):
    return datetime(2026, 11, 20, h, m, tzinfo=ET)


def test_night_start_overnight_print_is_stale():
    assert live_price_for_session(_snap(ovn="103.0"), "NIGHT", now=_at(20, 1)) == (103.0, "overnight", True)
    assert live_price_for_session(_snap(ovn="103.0"), "NIGHT", now=_at(21, 0))[2] is False


def test_after_close_leftover_premarket_print_is_stale():
    assert live_price_for_session(_snap(ext="105.0"), "ALL", now=_at(16, 2))[2] is True
    assert live_price_for_session(_snap(ext="100.2"), "ALL", now=_at(16, 2))[2] is False
    assert live_price_for_session(_snap(ext="105.0"), "ALL", now=_at(17, 0))[2] is False


# --- 5. risk: stale check uses one quote --------------------------------------------------------
@pytest.mark.asyncio
async def test_risk_rejects_stale_quote_outside_core(test_engine, mock_broker):
    from app.quotes import Quote
    set_test_time(_at(17, 0))
    mock_broker.quote = AsyncMock(return_value=Quote(symbol="AAPL", last=100.0, as_of=1.0,
                                                     stale=True))
    mock_broker.get_snapshot = AsyncMock(return_value=None)    # must not be consulted
    db = test_engine.db
    pid = _proposal(db, status="pending")
    v = await test_engine.risk.check(db.get_proposal(pid), mock_broker, db, kill_switch=False,
                                     at_execution=True, watchlist=["AAPL"])
    assert not v.ok and any("Extended-hours" in c["name"] for c in v.checks if not c["ok"])


# --- 6. re-price risk checks --------------------------------------------------------------------
def test_reprice_violation_rules(test_settings):
    s = test_settings
    assert reprice_violation(s, qty=1, px=100.0, sl=95.0, tp=110.0) is None
    assert reprice_violation(s, qty=1, px=111.0, sl=95.0, tp=110.0)          # tp <= px
    assert reprice_violation(s, qty=1, px=94.0, sl=95.0, tp=110.0)           # sl >= px
    assert reprice_violation(s, qty=10 ** 6, px=100.0, sl=95.0, tp=110.0)    # order value


# --- 7. kill switch: openings only --------------------------------------------------------------
@pytest.mark.asyncio
async def test_kill_switch_blocks_buy_proposal_but_not_sell_exit(test_engine, mock_broker):
    price = await mock_broker.last_price("AAPL")
    run_id = test_engine.db.create_run("test")
    await test_engine.set_kill_switch(True)
    buy = await test_engine._on_proposal(run_id, dict(symbol="AAPL", side="BUY", quantity=1,
                                         limit_price=price, confidence=0.5, rationale="t"))
    assert buy.startswith("REJECTED (kill switch)")
    sell = await test_engine._on_proposal(run_id, dict(symbol="AAPL", side="SELL", quantity=1,
                                          limit_price=price, confidence=0.5, rationale="t"))
    assert "kill switch" not in sell


# --- flatten: an unconfirmed cancel keeps the protective row tracked ----------------------------
@pytest.mark.asyncio
async def test_flatten_cancel_keeps_unconfirmed_stop_tracked(test_engine, mock_broker):
    db = test_engine.db
    live = db.add_protective(None, "AAPL", "sl", "stop-live", 4, 95.0, "active", "t")
    gone = db.add_protective(None, "MSFT", "sl", "stop-gone", 2, 400.0, "active", "t")
    text = "\n".join(
        f"Client Order ID: g-{c}\nCombo Type: NORMAL\n  Client Order ID: {c}\n  Symbol: {s}\n"
        f"  Side: SELL\n  Status: SUBMITTED\n  Filled Quantity: 0"
        for c, s in (("stop-live", "AAPL"), ("stop-gone", "MSFT")))
    mock_broker.open_orders_text = AsyncMock(return_value=text)

    async def cancel(coid):
        if coid == "stop-live":
            return "Order not found; it may have been filled or already cancelled"
        return "Order cancelled"
    mock_broker.cancel_order = AsyncMock(side_effect=cancel)
    n = await test_engine.cancel_open_orders(True, "flatten")
    assert n == 1
    rows = {r["id"]: r["status"] for r in db.protective(open_only=False)}
    assert rows[live] == "active"
    assert rows[gone] == "cancelled"
