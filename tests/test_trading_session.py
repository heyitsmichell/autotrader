"""Regression: Webull rejected a premarket order with
OPENAPI_OVERNIGHT_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS because the app sent
trading_session=NIGHT (the 20:00-04:00 ET overnight session) for premarket."""
from datetime import datetime

import pytest

from app.broker import BrokerError, resolve_session
from app.market import ET, order_session


def at(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET)


# Mon 2026-11-23 is a normal trading day; Thu 26th is Thanksgiving; Sat 28th weekend.
@pytest.mark.parametrize("when,expected", [
    (at(2026, 11, 23, 3, 0), "NIGHT"),    # overnight after Sunday evening
    (at(2026, 11, 23, 4, 0), "ALL"),      # premarket opens
    (at(2026, 11, 23, 8, 0), "ALL"),      # the failing case: premarket must not be NIGHT
    (at(2026, 11, 23, 9, 30), "CORE"),
    (at(2026, 11, 23, 16, 0), "ALL"),     # after-hours
    (at(2026, 11, 23, 20, 0), "NIGHT"),
    (at(2026, 11, 27, 13, 30), "ALL"),    # early close day: after-hours starts at 13:00
    (at(2026, 11, 28, 10, 0), None),      # Saturday
    (at(2026, 11, 26, 10, 0), None),      # Thanksgiving
])
def test_order_session(when, expected):
    assert order_session(when) == expected


def test_premarket_never_night():
    assert resolve_session("EXTENDED", at=at(2026, 11, 23, 8, 0)) == "ALL"


def test_night_blocked_outside_overnight():
    with pytest.raises(BrokerError) as e:
        resolve_session("NIGHT", at=at(2026, 11, 23, 8, 0))
    assert not e.value.transport


def test_closed_blocked():
    with pytest.raises(BrokerError):
        resolve_session("CORE", at=at(2026, 11, 28, 10, 0))


def test_core_blocked_premarket_and_fractional_needs_core():
    with pytest.raises(BrokerError):
        resolve_session("CORE", at=at(2026, 11, 23, 8, 0))
    with pytest.raises(BrokerError):
        resolve_session("ALL", entrust_type="AMOUNT", at=at(2026, 11, 23, 8, 0))
    assert resolve_session("AUTO", entrust_type="AMOUNT", at=at(2026, 11, 23, 10, 0)) == "CORE"


def test_extended_session_includes_afterhours():
    from app.market import market_is_open_extended, session_phase

    assert market_is_open_extended(at(2026, 11, 23, 17, 0))
    assert not market_is_open_extended(at(2026, 11, 23, 20, 0))
    assert session_phase(at(2026, 11, 23, 8, 0)) == "premarket"
    assert session_phase(at(2026, 11, 23, 12, 0)) == "regular"
    assert session_phase(at(2026, 11, 23, 17, 0)) == "afterhours"
    assert session_phase(at(2026, 11, 23, 21, 0)) == "overnight"
    assert session_phase(at(2026, 11, 28, 10, 0)) == "closed"


@pytest.fixture
def stub_broker():
    """A Broker with only the calls used by the order helpers stubbed out."""
    from unittest.mock import AsyncMock

    from app.broker import Broker

    b = Broker.__new__(Broker)
    b.account_id = AsyncMock(return_value="A1")
    b.call = AsyncMock(return_value="ok")
    return b


@pytest.mark.asyncio
async def test_preview_and_place_use_current_session(stub_broker):
    from app.market import set_test_time

    set_test_time(at(2026, 11, 23, 17, 0))
    try:
        await stub_broker.preview_order("AAPL", "BUY", 1, 100.0)
        assert stub_broker.call.call_args.kwargs["trading_session"] == "ALL"
        await stub_broker.place_order("AAPL", "BUY", 1, 100.0, "c1")
        assert stub_broker.call.call_args.kwargs["trading_session"] == "ALL"
        await stub_broker.place_stop("AAPL", 1, 95.0, "c2")
        assert stub_broker.call.call_args.kwargs["trading_session"] == "CORE"  # protective stops always go to CORE (Webull rejects extended-hours STOP)
    finally:
        set_test_time(None)


@pytest.mark.asyncio
async def test_bracket_keeps_extended_session_on_every_leg(stub_broker):
    from app.market import set_test_time

    set_test_time(at(2026, 11, 23, 17, 0))
    try:
        await stub_broker.place_bracket(
            "AAPL", 1, 100.0, 105.0, 95.0,
            {"entry": "e", "tp": "t", "sl": "s"}, "combo")
    finally:
        set_test_time(None)
    orders = stub_broker.call.call_args.kwargs["orders"]
    assert {o["support_trading_session"] for o in orders} == {"ALL"}


@pytest.mark.asyncio
async def test_extended_buy_skips_combo_and_sends_single_all(test_engine, monkeypatch):
    """After-hours: the combo is skipped up front (Webull rejects the STOP_LOSS leg outside
    regular hours) and the entry goes out as a single ALL order."""
    from app.broker import BrokerError
    from app.market import set_test_time

    calls: dict = {}

    async def preview_order(*a, **kw):
        calls["preview"] = kw
        return "preview"

    async def place_bracket(*a, **kw):
        calls["bracket"] = kw
        raise BrokerError("Order error: session not supported for combo outside regular hours")

    async def place_order(*a, **kw):
        calls["order"] = kw
        return "client_order_id: e\norder_id: 1"

    b = test_engine.broker
    monkeypatch.setattr(b, "preview_order", preview_order)
    monkeypatch.setattr(b, "place_bracket", place_bracket)
    monkeypatch.setattr(b, "place_order", place_order)

    p = {"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": 100.0,
         "sl": 95.0, "tp": 105.0, "confidence": 0.5, "rationale": "test", "ref_price": 100.0}
    pid = test_engine.db.add_proposal(None, p, "submitting", "test")

    # Earlier tests toggle BRACKET_ORDERS in os.environ; pin it for this check.
    object.__setattr__(test_engine.s, "bracket_orders", True)
    set_test_time(at(2026, 11, 23, 17, 0))
    try:
        await test_engine._send(pid, p, "user", "test")
    finally:
        set_test_time(None)

    assert calls["preview"]["trading_session"] == "ALL"
    assert "bracket" not in calls
    assert calls["order"]["trading_session"] == "ALL"
    assert test_engine.db.get_proposal(pid)["status"] == "placed"


@pytest.mark.asyncio
async def test_closed_market_refuses_before_preview(test_engine, monkeypatch):
    from app.market import set_test_time

    async def preview_order(*a, **kw):
        raise AssertionError("preview must not be called when every session is closed")

    monkeypatch.setattr(test_engine.broker, "preview_order", preview_order)
    p = {"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": 100.0,
         "sl": 95.0, "tp": 105.0, "confidence": 0.5, "rationale": "test", "ref_price": 100.0}
    pid = test_engine.db.add_proposal(None, p, "submitting", "test")

    set_test_time(at(2026, 11, 28, 10, 0))
    try:
        await test_engine._send(pid, p, "user", "test")
    finally:
        set_test_time(None)

    assert test_engine.db.get_proposal(pid)["status"] == "failed"


@pytest.mark.asyncio
async def test_overnight_symbol_requires_opt_in(test_engine, monkeypatch):
    """NIGHT orders are refused (without counting as an order failure) unless the symbol is
    on OVERNIGHT_SYMBOLS and OVERNIGHT_TRADING is on."""
    from app.market import set_test_time

    async def preview_order(*a, **kw):
        raise AssertionError("preview must not be called for a non-overnight symbol")

    monkeypatch.setattr(test_engine.broker, "preview_order", preview_order)
    p = {"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": 100.0,
         "sl": 95.0, "tp": 105.0, "confidence": 0.5, "rationale": "test", "ref_price": 100.0}
    pid = test_engine.db.add_proposal(None, p, "submitting", "test")

    set_test_time(at(2026, 11, 23, 21, 0))
    try:
        await test_engine._send(pid, p, "user", "test")
    finally:
        set_test_time(None)

    assert test_engine.db.get_proposal(pid)["status"] == "failed"
    assert test_engine.order_failures == 0


@pytest.mark.asyncio
async def test_overnight_uses_night_and_weekends_are_refused(stub_broker):
    from app.market import set_test_time

    set_test_time(at(2026, 11, 23, 21, 0))
    try:
        await stub_broker.place_order("AAPL", "BUY", 1, 100.0, "c1")
        assert stub_broker.call.call_args.kwargs["trading_session"] == "NIGHT"
    finally:
        set_test_time(None)

    set_test_time(at(2026, 11, 28, 10, 0))
    try:
        with pytest.raises(BrokerError):
            await stub_broker.place_order("AAPL", "BUY", 1, 100.0, "c2")
    finally:
        set_test_time(None)
