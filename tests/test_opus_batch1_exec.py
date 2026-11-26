"""Opus review batch 1: S4, S16, C3, C4, C5."""
import asyncio
from types import SimpleNamespace

import pytest

from app.broker import BrokerError
from app.engine.execution import ExecutionMixin
from app.engine.util import classify_order_error


def _orders(*rows):
    out = ""
    for coid, status in rows:
        out += (f"Client Order ID: g{coid}\n  Client Order ID: {coid}\n  Symbol: AAPL\n"
                f"  Side: SELL\n  Status: {status}\n  Total Quantity: 5\n")
    return out


class Eng(ExecutionMixin):
    def __init__(self, n=3):
        self.s = SimpleNamespace(kill_max_consecutive_errors=n)
        self.order_failures = 0
        self.run_failures = 0
        self.kill_switch = False
        self.last_failure = None
        self.logs = []
        self.tripped = []
        self.error_trip_cb = self.tripped.append
        self.db = SimpleNamespace(log=lambda *a, **k: self.logs.append(a))

    def _failed(self, kind, message):
        self.order_failures += 1
        self.last_failure = message


def test_s4_classification():
    assert classify_order_error(BrokerError("x timed out", transport=True)) == "transport"
    assert classify_order_error(BrokerError("place: Invalid trading_session for region")) == "session"
    assert classify_order_error(BrokerError("place: 401 Unauthorized")) == "auth"
    assert classify_order_error(BrokerError("place: 429 Too Many Requests")) == "rate_limit"
    assert classify_order_error(BrokerError("place: insufficient buying power")) == "order_rejected"


def test_s4_only_rejections_count_and_session_blocks():
    e = Eng(n=99)
    e._order_failed(BrokerError("Invalid trading_session"), "o1", "c1")
    e._order_failed(BrokerError("401 Unauthorized"), "o2", "c2")
    e._order_failed(BrokerError("429 Too Many Requests"), "o3", "c3")
    assert e.order_failures == 0
    assert e.order_send_blocked() is True
    e._order_failed(BrokerError("insufficient buying power"), "o4", "c4")
    assert e.order_failures == 1


def test_s4_uncount_transport():
    e = Eng(n=99)
    e._order_failed(BrokerError("timed out", transport=True), "o", "cx")
    assert e.order_failures == 1
    e.uncount_failure("cx")
    assert e.order_failures == 0
    e.uncount_failure("cx")
    assert e.order_failures == 0


def test_s16_threshold_trips_immediately():
    e = Eng(n=2)
    e._order_failed(BrokerError("bad price"), "o", "a")
    assert e.tripped == []
    e._order_failed(BrokerError("bad size"), "o", "b")
    assert len(e.tripped) == 1


def _ago(sec):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=sec)).isoformat()


def _cancel_eng(rows, orders, cancel, history=""):
    e = Eng()
    marked = []

    async def open_orders_text():
        return orders

    async def order_history_text():
        return history

    e.broker = SimpleNamespace(cancel_order=cancel, open_orders_text=open_orders_text,
                               order_history_text=order_history_text)
    e.db = SimpleNamespace(protective=lambda sym: rows, log=lambda *a, **k: None,
                           set_protective=lambda *a, **k: marked.append((a, k)))
    return e, marked


@pytest.mark.asyncio
async def test_c3_unknown_row_absent_is_closed_and_skipped():
    rows = [{"id": 1, "status": "unknown", "client_order_id": "ghost", "kind": "sl", "price": 90.0,
             "created_at": _ago(3600)}]

    async def cancel(coid):
        raise AssertionError("must not cancel an order the broker never had")

    e, marked = _cancel_eng(rows, "", cancel)
    assert await e._cancel_protective("AAPL", "s1") == []
    assert marked[0][0][:2] == (1, "closed")


@pytest.mark.asyncio
async def test_c3_young_unknown_row_blocks_sell():
    rows = [{"id": 1, "status": "unknown", "client_order_id": "ghost", "kind": "sl", "price": 90.0,
             "created_at": _ago(30)}]

    async def cancel(coid):
        raise AssertionError("no cancel")

    e, marked = _cancel_eng(rows, "", cancel)
    assert await e._cancel_protective("AAPL", "s1") is None
    assert marked == []


@pytest.mark.asyncio
async def test_c4_partial_failure_exposes_cancelled_prices():
    rows = [{"id": 1, "status": "active", "client_order_id": "a", "kind": "sl", "price": 90.0,
             "quantity": 3},
            {"id": 2, "status": "active", "client_order_id": "b", "kind": "sl", "price": 95.0,
             "quantity": 4}]

    async def cancel(coid):
        if coid == "b":
            raise BrokerError("timed out", transport=True)
        return "Cancelled"

    e, _ = _cancel_eng(rows, _orders(("a", "SUBMITTED"), ("b", "SUBMITTED")), cancel)
    part: list = []
    assert await e._cancel_protective("AAPL", "s1", part) is None
    assert part == [(90.0, 3.0)]         # per-call list (no shared engine state)
    assert not hasattr(e, "_cancel_partial")

    reprot = []

    async def rp(p, shares, stops):
        reprot.append((shares, list(stops)))
    e._reprotect = rp
    await e._reprotect_partial({"id": 1, "symbol": "AAPL"}, 100, part)
    assert reprot == [(3.0, [90.0])]
    reprot.clear()
    await e._reprotect_partial({"id": 1, "symbol": "AAPL"}, 2, [(90.0, 3.0), (95.0, 4.0)])
    assert reprot == [(2, [90.0, 95.0])]


@pytest.mark.asyncio
async def test_c5_refused_cancel_blocks_sell_unless_cancelled():
    rows = [{"id": 1, "status": "active", "client_order_id": "a", "kind": "sl", "price": 90.0}]

    async def cancel(coid):
        raise BrokerError("Validation error: cannot cancel")

    e, marked = _cancel_eng(rows, _orders(("a", "SUBMITTED")), cancel)
    assert await e._cancel_protective("AAPL", "s1") is None
    assert marked == []

    e2, _ = _cancel_eng(rows, "", cancel, history=_orders(("a", "CANCELLED")))
    assert await e2._cancel_protective("AAPL", "s1") == [90.0]


@pytest.mark.asyncio
async def test_c5_refused_cancel_absent_everywhere_is_closed():
    rows = [{"id": 1, "status": "active", "client_order_id": "a", "kind": "sl", "price": 90.0}]

    async def cancel(coid):
        raise BrokerError("Order not found")

    e, marked = _cancel_eng(rows, "", cancel)
    assert await e._cancel_protective("AAPL", "s1") == []
    assert marked[0][0][:2] == (1, "closed")


def test_auth_codes_need_http_context():
    assert classify_order_error(BrokerError("rejected qty 1401 @ 403.25")) == "order_rejected"
    assert classify_order_error(BrokerError("HTTP 403 something")) == "auth"


def test_preview_failure_routes_through_classifier(monkeypatch):
    from app.engine import execution as ex
    e = Eng(n=99)
    monkeypatch.setattr(ex, "session_phase", lambda *a, **k: "premarket")
    e._order_failed(BrokerError("Invalid trading_session"), "o", "")
    assert e.order_failures == 0 and e.order_send_blocked() is True
    monkeypatch.setattr(ex, "session_phase", lambda *a, **k: "afterhours")
    assert e.order_send_blocked() is False
    e._order_failed(BrokerError("preview timed out", transport=True), "o", "")
    assert e.order_failures == 0


def test_auth_classification_covers_login_token():
    from app.broker import BrokerError
    from app.engine.util import classify_order_error
    assert classify_order_error(BrokerError("Webull login token missing or not approved: x")) == "auth"
    assert classify_order_error(BrokerError("Device Not Registered")) == "auth"
    assert classify_order_error(BrokerError("rejected qty 1401 @ 403.25")) == "order_rejected"


def test_2fa_word_boundary():
    from app.broker import BrokerError
    from app.engine.util import classify_order_error
    assert classify_order_error(BrokerError("waiting for 2FA approval")) == "auth"
    assert classify_order_error(BrokerError("duplicate client_order_id a12fa9c0")) != "auth"
