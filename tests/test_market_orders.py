"""Market-style orders: MARKET exits (CORE only), marketable entries, entry chase, guard, recording."""

import dataclasses
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app import order_style
from app.broker import BrokerError, OrderGuardError
from app.broker.guard import OrderGuard
from app.engine import chase, execution
from app.market import now_utc
from app.quotes import Quote


def _set(engine, **kw):
    engine.s = dataclasses.replace(engine.s, **kw)
    engine.portfolio.s = engine.s


def _events(db):
    return [dict(level=r[0], message=r[1]) for r in
            db.conn.execute("SELECT level, message FROM events").fetchall()]


def _proposal(db, side="BUY", status="submitting", qty=4, price=100.0, **extra):
    extra.setdefault("sl", 95.0)
    extra.setdefault("tp", 110.0)
    return db.add_proposal(None, dict(symbol="AAPL", side=side, quantity=qty, limit_price=price,
                                      confidence=0.5, rationale="t"),
                           status, ref_price=price, **extra)


def _stub_sell(engine, mock_broker, held=4.0):
    engine.portfolio.sync_statuses = AsyncMock(return_value=0)
    mock_broker.positions = AsyncMock(return_value={"AAPL": held})
    mock_broker.preview_order = AsyncMock(return_value="ok")
    mock_broker.cancel_order = AsyncMock(return_value="ok")


# --- settings / defaults -------------------------------------------------------------------------
def test_defaults_are_unchanged(test_settings):
    s = test_settings
    assert s.exit_order_type == "limit"
    assert s.entry_order_type == "limit"
    assert s.entry_chase_seconds == 0.0
    assert s.marketable_offset_bps == 10.0


def test_invalid_choice_is_rejected(monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("EXIT_ORDER_TYPE", "stop")
    with pytest.raises(ValueError):
        load_settings()


@pytest.mark.asyncio
async def test_default_sell_stays_limit(test_engine, mock_broker):
    _stub_sell(test_engine, mock_broker)
    mock_broker.place_order = AsyncMock(return_value="client_order_id: x")
    db = test_engine.db
    pid = _proposal(db, side="SELL")
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    kw = mock_broker.place_order.await_args.kwargs
    assert kw["order_type"] == "LIMIT"
    assert db.get_proposal(pid)["order_type"] == "limit"


# --- market exits ---------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_market_exit_in_core(test_engine, mock_broker):
    _set(test_engine, exit_order_type="market")
    _stub_sell(test_engine, mock_broker)
    mock_broker.place_order = AsyncMock(return_value="client_order_id: x")
    db = test_engine.db
    pid = _proposal(db, side="SELL")
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    assert mock_broker.place_order.await_args.kwargs["order_type"] == "MARKET"
    assert mock_broker.preview_order.await_args.kwargs["order_type"] == "MARKET"
    row = db.get_proposal(pid)
    assert row["order_type"] == "market" and row["status"] == "placed"


@pytest.mark.asyncio
async def test_market_exit_falls_back_to_limit_outside_core(test_engine, mock_broker, monkeypatch):
    monkeypatch.setattr(execution, "order_session", lambda *a, **k: "ALL")
    _set(test_engine, exit_order_type="market")
    _stub_sell(test_engine, mock_broker)
    mock_broker.place_order = AsyncMock(return_value="client_order_id: x")
    db = test_engine.db
    pid = _proposal(db, side="SELL")
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    assert mock_broker.place_order.await_args.kwargs["order_type"] == "LIMIT"
    assert db.get_proposal(pid)["order_type"] == "limit"


@pytest.mark.asyncio
async def test_rejected_market_exit_is_resent_as_limit(test_engine, mock_broker):
    _set(test_engine, exit_order_type="market")
    _stub_sell(test_engine, mock_broker)
    mock_broker.place_order = AsyncMock(side_effect=[BrokerError("market not allowed"),
                                                     "client_order_id: y"])
    db = test_engine.db
    pid = _proposal(db, side="SELL")
    old = db.get_proposal(pid)
    await test_engine._send(pid, old, "auto", "w")
    assert mock_broker.place_order.await_count == 2
    first, second = mock_broker.place_order.await_args_list
    assert first.kwargs["order_type"] == "MARKET" and second.kwargs["order_type"] == "LIMIT"
    assert first.args[4] != second.args[4]                       # a new client order id
    row = db.get_proposal(pid)
    assert row["status"] == "placed" and row["order_type"] == "limit"
    assert row["client_order_id"] == second.args[4]


@pytest.mark.asyncio
async def test_flatten_sell_is_market_in_core_and_passes_kill_switch(test_engine, mock_broker):
    _set(test_engine, exit_order_type="market")
    test_engine.kill_switch = True
    mock_broker.place_order = AsyncMock(return_value="client_order_id: x")
    status = await test_engine._flatten_sell("AAPL", 3, 99.5, "user", "test")
    assert status == "placed"
    kw = mock_broker.place_order.await_args.kwargs
    assert kw["order_type"] == "MARKET" and kw["allow_when_killed"] is True


@pytest.mark.asyncio
async def test_flatten_sell_limit_outside_core(test_engine, mock_broker, monkeypatch):
    from app.engine import flatten
    monkeypatch.setattr(flatten, "order_session", lambda *a, **k: "ALL")
    _set(test_engine, exit_order_type="market")
    mock_broker.place_order = AsyncMock(return_value="client_order_id: x")
    await test_engine._flatten_sell("AAPL", 3, 99.5, "user", "test")
    assert mock_broker.place_order.await_args.kwargs["order_type"] == "LIMIT"


# --- broker: payload, session rule, guard ----------------------------------------------------------
@pytest.mark.asyncio
async def test_market_payload_omits_limit_price(mock_broker):
    sent = {}

    async def fake_submit(tool, **args):
        sent.update(args)
        return "ok"
    mock_broker._submit = fake_submit
    await mock_broker.place_order("AAPL", "SELL", 2, 150.0, "c1", order_type="MARKET")
    assert sent["order_type"] == "MARKET" and sent["time_in_force"] == "DAY"
    assert "limit_price" not in sent and sent["trading_session"] == "CORE"


@pytest.mark.asyncio
async def test_market_refused_outside_core(mock_broker, monkeypatch):
    from app.broker import orders
    monkeypatch.setattr(orders.market, "order_session", lambda at=None: "ALL")
    mock_broker._submit = AsyncMock()
    with pytest.raises(BrokerError, match="only accepted in the CORE"):
        await mock_broker.place_order("AAPL", "SELL", 2, 150.0, "c1", order_type="MARKET")
    mock_broker._submit.assert_not_called()


@pytest.mark.asyncio
async def test_mock_market_order_fills(mock_broker):
    last = await mock_broker.last_price("AAPL")
    await mock_broker.place_order("AAPL", "BUY", 1, last, "mk-1", order_type="MARKET")
    assert (await mock_broker.positions()).get("AAPL") == 1
    text = await mock_broker.order_history_text()
    assert "mk-1" in text and "MARKET" in text and "FILLED" in text


def test_guard_market_notional_uses_buffer(test_settings):
    s = dataclasses.replace(test_settings, max_order_usd=1000.0,
                            market_notional_buffer_pct=2.0)
    g = OrderGuard(s)
    # 9 x 100 x 1.02 = 918 <= 1000
    g.check({"kind": "stock", "symbol": "AAPL", "side": "BUY", "quantity": 9, "price": 100.0,
             "order_type": "MARKET", "opening": True})
    # 10 x 100 = 1000 passes as a limit but 1020 with the buffer is refused
    g.check({"kind": "stock", "symbol": "AAPL", "side": "BUY", "quantity": 10, "price": 100.0,
             "opening": True})
    with pytest.raises(OrderGuardError, match="notional"):
        g.check({"kind": "stock", "symbol": "AAPL", "side": "BUY", "quantity": 10, "price": 100.0,
                 "order_type": "MARKET", "opening": True})
    with pytest.raises(OrderGuardError, match="reference price"):
        g.check({"kind": "stock", "symbol": "AAPL", "side": "BUY", "quantity": 1, "price": None,
                 "order_type": "MARKET", "opening": True})


def test_guard_closing_market_always_passes_even_when_killed(test_settings):
    s = dataclasses.replace(test_settings, max_order_usd=10.0)
    g = OrderGuard(s)
    g.set_context(kill_switch=lambda: True)
    g.check({"kind": "stock", "symbol": "AAPL", "side": "SELL", "quantity": 500, "price": 100.0,
             "order_type": "MARKET", "opening": False})
    with pytest.raises(OrderGuardError, match="Kill switch"):
        g.check({"kind": "stock", "symbol": "AAPL", "side": "BUY", "quantity": 1, "price": 1.0,
                 "order_type": "MARKET", "opening": True})


# --- marketable price math -------------------------------------------------------------------------
def test_marketable_uses_ask_plus_offset_and_ticks(test_settings):
    s = dataclasses.replace(test_settings, marketable_offset_bps=10.0, price_band_pct=1.0)
    assert order_style.marketable_buy_price(s, last=100.0, ask=100.20, ref_price=100.0) == 100.30
    assert order_style.marketable_buy_price(s, last=100.0, ask=None, ref_price=100.0) == 100.10
    assert order_style.marketable_buy_price(s, last=None, ask=None, ref_price=100.0) is None


def test_marketable_is_capped_at_the_price_band(test_settings):
    s = dataclasses.replace(test_settings, marketable_offset_bps=200.0, price_band_pct=1.0)
    # 100.50 x 1.02 = 102.51 but the cap is 100 x 1.01 = 101.00
    assert order_style.marketable_buy_price(s, last=100.5, ask=100.5, ref_price=100.0) == 101.0
    # the cap is floored to the tick, never rounded up past the band
    assert order_style.marketable_buy_price(s, last=120, ask=120, ref_price=33.333) == 33.66


def test_marketable_tick_below_one_dollar(test_settings):
    s = dataclasses.replace(test_settings, marketable_offset_bps=10.0, price_band_pct=5.0)
    assert order_style.marketable_buy_price(s, last=0.5, ask=0.5123, ref_price=0.5) == 0.5128
    assert order_style.tick_for(0.99) == 0.0001 and order_style.tick_for(1.0) == 0.01


# --- marketable entries in _send --------------------------------------------------------------------
def _entry_stubs(mock_broker, last=100.0, ask=100.2):
    mock_broker.quote = AsyncMock(return_value=Quote(symbol="AAPL", last=last, ask=ask,
                                                     as_of=now_utc().timestamp()))
    mock_broker.preview_order = AsyncMock(return_value="ok")
    mock_broker.place_order = AsyncMock(return_value="client_order_id: x")
    mock_broker.place_bracket = AsyncMock(return_value="client_order_id: x")


@pytest.mark.asyncio
async def test_marketable_single_entry(test_engine, mock_broker):
    _set(test_engine, entry_order_type="marketable", bracket_orders=False, max_order_usd=10_000.0)
    _entry_stubs(mock_broker)
    db = test_engine.db
    pid = _proposal(db)
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    args = mock_broker.place_order.await_args
    assert args.args[3] == 100.30 and args.kwargs["order_type"] == "LIMIT"
    row = db.get_proposal(pid)
    assert row["order_type"] == "marketable" and row["limit_price"] == 100.30
    assert db.get_trade(pid)["limit_price"] == 100.30


@pytest.mark.asyncio
async def test_marketable_bracket_master_leg(test_engine, mock_broker):
    _set(test_engine, entry_order_type="marketable", bracket_orders=True, max_order_usd=10_000.0)
    _entry_stubs(mock_broker)
    db = test_engine.db
    pid = _proposal(db)
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    assert mock_broker.place_bracket.await_args.args[2] == 100.30
    assert db.get_proposal(pid)["order_type"] == "marketable"


@pytest.mark.asyncio
async def test_marketable_shrinks_qty_when_it_would_break_the_order_cap(test_engine, mock_broker):
    # C2 (2026-11-26): instead of dropping the marketable price, the quantity shrinks to fit.
    _set(test_engine, entry_order_type="marketable", bracket_orders=False, max_order_usd=400.0)
    _entry_stubs(mock_broker)
    db = test_engine.db
    pid = _proposal(db)                                  # 4 x 100.30 = 401.2 > 400 -> 3 shares
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    args = mock_broker.place_order.await_args.args
    assert args[3] > 100.0 and float(args[2]) == 3.0
    assert float(db.get_proposal(pid)["quantity"]) == 3.0
    assert db.get_proposal(pid)["order_type"] == "marketable"


@pytest.mark.asyncio
async def test_default_entry_is_unchanged(test_engine, mock_broker):
    _set(test_engine, bracket_orders=False)
    _entry_stubs(mock_broker)
    db = test_engine.db
    pid = _proposal(db)
    await test_engine._send(pid, db.get_proposal(pid), "auto", "w")
    mock_broker.quote.assert_not_called()
    assert mock_broker.place_order.await_args.args[3] == 100.0
    assert db.get_proposal(pid)["order_type"] == "limit"


# --- entry chase -------------------------------------------------------------------------------------
class Book:
    """A tiny order book the broker text calls read from."""

    def __init__(self):
        self.orders: dict[str, tuple[str, float]] = {}

    def text(self) -> str:
        out = []
        for coid, (status, filled) in self.orders.items():
            out += [f"Client Order ID: g-{coid}", "Combo Type: NORMAL",
                    f"  Client Order ID: {coid}", "  Symbol: AAPL", "  Side: BUY",
                    f"  Status: {status}", f"  Filled Quantity: {filled}"]
        return "\n".join(out)


def _chase_setup(engine, broker, monkeypatch, *, age=120, bracket=False, last=100.0,
                 confirm=True, **settings):
    monkeypatch.setattr(chase, "CONFIRM_DELAY_SECONDS", 0)
    _set(engine, entry_chase_seconds=30.0, bracket_orders=bracket, max_order_usd=10_000.0,
         **settings)
    db = engine.db
    pid = _proposal(db, status="placed", client_order_id="old-1",
                    placed_at=(now_utc() - timedelta(seconds=age)).isoformat(timespec="seconds"))
    p = db.get_proposal(pid)
    db.add_trade_entry(pid, p, None)
    book = Book()
    book.orders["old-1"] = ("SUBMITTED", 0)
    broker.open_orders_text = AsyncMock(side_effect=lambda: book.text())
    broker.order_history_text = AsyncMock(side_effect=lambda: book.text())

    async def cancel(coid):
        if confirm:
            book.orders[coid] = ("CANCELLED", book.orders.get(coid, ("", 0))[1])
        return "ok"
    broker.cancel_order = AsyncMock(side_effect=cancel)
    broker.quote = AsyncMock(return_value=Quote(symbol="AAPL", last=last, ask=last + 0.1,
                                                as_of=now_utc().timestamp()))
    broker.place_order = AsyncMock(return_value="client_order_id: new")
    broker.place_bracket = AsyncMock(return_value="client_order_id: new")
    return pid, book


@pytest.mark.asyncio
async def test_chase_fires_once(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    ch = chase.EntryChaser(test_engine)
    assert await ch.run_once() == [pid]
    mock_broker.cancel_order.assert_awaited_once_with("old-1")
    args = mock_broker.place_order.await_args
    assert args.args[:3] == ("AAPL", "BUY", 4) and args.args[3] == 100.20
    assert args.args[4] != "old-1" and args.kwargs["trading_session"] == "CORE"
    row = test_engine.db.get_proposal(pid)
    assert row["order_type"] == "chase" and row["client_order_id"] == args.args[4]
    assert row["status"] == "placed" and test_engine.db.get_trade(pid)["limit_price"] == 100.20
    # a second pass (even from a fresh chaser = restart) never chases the same proposal again
    book.orders[args.args[4]] = ("SUBMITTED", 0)
    assert await chase.EntryChaser(test_engine).run_once() == []
    assert mock_broker.place_order.await_count == 1
    assert test_engine.db.get_kv(f"chase:{pid}")


@pytest.mark.asyncio
async def test_chase_resends_bracket(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch, bracket=True)
    db = test_engine.db
    old = test_engine._record_bracket(pid, db.get_proposal(pid),
                                      {"tp": "old-tp", "sl": "old-sl"}, "waiting")
    assert await chase.EntryChaser(test_engine).run_once() == [pid]
    mock_broker.place_bracket.assert_awaited_once()
    assert mock_broker.place_bracket.await_args.args[2] == 100.20
    rows = {r["client_order_id"]: r["status"] for r in db.protective("AAPL", open_only=False)}
    assert rows["old-tp"] == "closed" and rows["old-sl"] == "closed"
    assert sum(1 for s in rows.values() if s == "waiting") == 2


@pytest.mark.asyncio
async def test_chase_off_by_default_and_before_the_delay(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch, age=5)
    assert await chase.EntryChaser(test_engine).run_once() == []          # only 5 s old
    _set(test_engine, entry_chase_seconds=0.0)
    test_engine.db.update_proposal(
        pid, placed_at=(now_utc() - timedelta(seconds=999)).isoformat(timespec="seconds"))
    assert await chase.EntryChaser(test_engine).run_once() == []          # feature off
    mock_broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_only_in_core(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    monkeypatch.setattr(chase, "order_session", lambda *a, **k: "ALL")
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_never_on_partial_fill(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    book.orders["old-1"] = ("PARTIAL_FILLED", 1)
    assert await chase.EntryChaser(test_engine).run_once() == []
    book.orders["old-1"] = ("SUBMITTED", 1)                    # any fill at all
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.cancel_order.assert_not_called()
    mock_broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_never_with_kill_switch(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    test_engine.kill_switch = True
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_never_when_price_left_the_band(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch, last=103.0)   # +3% vs band 1%
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.cancel_order.assert_not_called()
    assert not test_engine.db.get_kv(f"chase:{pid}")


@pytest.mark.asyncio
async def test_chase_never_when_stop_is_above_the_price(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    test_engine.db.update_proposal(pid, sl=100.5)
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_never_when_cancel_is_not_confirmed(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch, confirm=False)
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_called()
    assert not test_engine.db.get_kv(f"chase:{pid}")
    # the cancel call itself failing is also "not confirmed"
    mock_broker.cancel_order = AsyncMock(side_effect=BrokerError("no reply", transport=True))
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_does_not_rebuy_an_order_someone_else_cancelled(test_engine, mock_broker,
                                                                    monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    book.orders["old-1"] = ("CANCELLED", 0)                    # no chase_cancel marker
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_does_not_resume_after_unconfirmed_cancel(test_engine, mock_broker,
                                                              monkeypatch):
    # C1 (2026-11-26): an unconfirmed cancel clears the marker; a later CANCELLED status (which
    # this chase never confirmed, e.g. an expiry or a manual cancel) must NOT trigger a re-buy.
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch, confirm=False)
    assert await chase.EntryChaser(test_engine).run_once() == []       # cancel sent, not confirmed
    assert not test_engine.db.get_kv(f"chase_cancel:{pid}")
    book.orders["old-1"] = ("CANCELLED", 0)
    assert await chase.EntryChaser(test_engine).run_once() == []
    mock_broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_chase_rejection_closes_the_trade(test_engine, mock_broker, monkeypatch):
    pid, book = _chase_setup(test_engine, mock_broker, monkeypatch)
    mock_broker.place_order = AsyncMock(side_effect=BrokerError("insufficient buying power"))
    assert await chase.EntryChaser(test_engine).run_once() == [pid]
    assert test_engine.db.get_proposal(pid)["status"] == "failed"
    assert test_engine.db.get_trade(pid)["status"] == "cancelled"
    assert await chase.EntryChaser(test_engine).run_once() == []        # still only once


# --- recording --------------------------------------------------------------------------------------
def test_execution_quality_groups_by_order_type():
    from app.execution_quality import summarize
    def prop(i, ot):
        return {"id": i, "symbol": "AAPL", "side": "BUY", "status": "placed", "ref_price": 100.0,
                "limit_price": 100.0, "order_type": ot, "run_id": None, "signal_id": None,
                "created_at": "2026-11-20T15:00:00+00:00", "placed_at": "2026-11-20T15:00:01+00:00"}
    props = [prop(1, "limit"), prop(2, "marketable"), prop(3, None), prop(4, "chase")]
    trades = {i: {"quantity_filled": 4, "entry_fill_price": 100.0 + i / 10,
                  "entry_fill_time": "2026-11-20T15:00:05+00:00"} for i in (2, 3)}
    out = summarize(props, trades, {}, {})
    by = out["by_order_type"]
    assert set(by) == {"limit", "marketable", "chase"}            # NULL counts as limit
    assert by["limit"]["orders"] == 2 and by["limit"]["filled"] == 1
    assert by["marketable"]["filled"] == 1 and by["chase"]["filled"] == 0


def test_migration_adds_order_type_column(test_db):
    cols = [r[1] for r in test_db.conn.execute("PRAGMA table_info(proposals)").fetchall()]
    assert "order_type" in cols
