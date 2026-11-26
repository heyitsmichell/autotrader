"""24/5 trading: entries never use ALL_DAY (invalid for region us), overnight size factor, extended-hours spread guard, N-4."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.broker.parsers import parse_snapshot
from app.engine.execution import ExecutionMixin
from app.order_style import extended_spread_pct
from app.sizing import overnight_scaled_qty
from tests.test_broker_guard import FakeSession, _broker, _result

SYMS = ["AAPL", "TLT"]


def _b(sess, session, **kw):
    import app.broker.orders as o
    o.market.order_session = lambda at=None: session
    return _broker(sess, overnight_trading=True, overnight_symbols=SYMS, **kw)


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    monkeypatch.setattr("app.broker.orders.market.order_session", lambda at=None: "CORE")
    # Some tests here move the global test clock (set_test_time) to the overnight session; put the
    # session-wide frozen time back afterwards, or every later test runs "at night" (this broke
    # tests/test_bracket_orders.py whenever it ran after this file).
    import app.market as m
    saved = m._test_time
    yield
    m.set_test_time(saved)


async def _place(session, symbol="AAPL", side="BUY", order_type="LIMIT", **kw):
    sess = FakeSession([_result("client_order_id: x")])
    b = _b(sess, session, **kw)
    await b.place_order(symbol, side, 1, 100.0, "c1", trading_session="AUTO", order_type=order_type)
    return sess.calls[0][1]["trading_session"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cur", ["ALL", "NIGHT"])
async def test_overnight_buy_limit_uses_clock_session_not_all_day(cur):
    # Webull region 'us' rejects ALL_DAY (valid: ALL, CORE, NIGHT); 2026-11-26 kill-switch incident
    assert await _place(cur) == cur


@pytest.mark.asyncio
async def test_all_day_not_in_core_not_for_other_symbols_or_setting_off():
    assert await _place("CORE") == "CORE"
    assert await _place("ALL", symbol="ZZZZ") == "ALL"


@pytest.mark.asyncio
async def test_all_day_not_for_sell_or_stop():
    assert await _place("ALL", side="SELL") == "ALL"
    sess = FakeSession([_result("client_order_id: x")])
    await _b(sess, "ALL").place_stop("AAPL", 1, 90.0, "c2")
    assert sess.calls[0][1]["trading_session"] == "CORE"


@pytest.mark.asyncio
async def test_market_order_never_all_day():
    from app.broker import BrokerError
    with pytest.raises(BrokerError):
        await _place("ALL", order_type="MARKET")      # refused outside CORE, never ALL_DAY
    assert await _place("CORE", order_type="MARKET") == "CORE"


SNAP = ("AAPL Price: 300 PreClose: 299\n   Bid: 300.00 x 3  Ask: 300.10 x 2\n"
        "OVN Price: 352 Vol: 9\nOVN Bid: 349.33 x 4 OVN Ask: 355.00 x 2\n")


def test_parse_ovn_bid_ask_and_spread_choice():
    a = parse_snapshot(SNAP)["AAPL"]
    assert (a["bid"], a["ask"], a["ovn_bid"], a["ovn_ask"]) == (300.0, 300.1, 349.33, 355.0)
    assert extended_spread_pct(a, "NIGHT") > 1.5           # OVN book is wide
    assert extended_spread_pct(a, "ALL") < 0.1
    assert extended_spread_pct({"price": 1}, "ALL") is None


def test_size_factor_only_in_night():
    s = replace(_broker().settings, overnight_size_factor=0.5)
    assert overnight_scaled_qty(s, 10, "NIGHT") == 5
    assert overnight_scaled_qty(s, 1, "NIGHT") == 1
    assert overnight_scaled_qty(s, 10, "ALL") == 10
    assert overnight_scaled_qty(s, 10, "CORE") == 10


WIDE = """=== Stock Snapshot ===
       AAPL  Price:     300.00  PreClose:     300.00  Change:     0.00  Change%:     0.00%
            Open:     300.00  High:     301.00  Low:     299.00  Close:     300.00  Vol:      1200000
            Bid:     0 x      0  Ask:     0 x      0
            OVN Price:     352.00  High:     355.00  Low:     349.00  Change:   52.0 (17%)  Vol:  900
            OVN Bid:     349.33 x      4  OVN Ask:     355.00 x      2
"""


@pytest.mark.asyncio
async def test_spread_guard_blocks_wide_buy_never_sell(test_settings, test_db):
    import dataclasses
    from app.market import ET, set_test_time
    from app.risk import RiskManager
    from tests.test_extended_hours_prices import FakeRiskBroker, SnapBroker, _p
    set_test_time(datetime(2026, 11, 24, 22, 0, tzinfo=ET))
    st = dataclasses.replace(test_settings, bracket_orders=False, max_spread_pct_extended=1.0,
                             overnight_trading=True, overnight_symbols=["AAPL"])

    async def verdict(side, held=0.0):
        fb = FakeRiskBroker(SnapBroker(test_settings, WIDE), held)
        fb.b.__dict__.setdefault("_x", 1)
        return await RiskManager(st).check(_p("AAPL", side, 352.0), fb, test_db,
                                           kill_switch=False, at_execution=False)

    v = await verdict("BUY")
    assert not v.ok and any("spread" in r.lower() for r in v.reasons)
    v = await verdict("SELL", held=5)
    assert not any("spread" in r.lower() for r in v.reasons)


@pytest.mark.asyncio
async def test_unconfirmed_cancel_keeps_protective_row_tracked():
    rows = [{"id": 1, "status": "armed", "client_order_id": "sl1", "kind": "sl", "price": 90.0}]
    marked = []

    class Eng(ExecutionMixin):
        pass

    e = Eng()

    async def cancel(coid):
        from app.broker import BrokerError
        raise BrokerError("Validation error: order already filled")

    e.broker = SimpleNamespace(cancel_order=cancel)
    e.db = SimpleNamespace(protective=lambda sym: rows, log=lambda *a, **k: None,
                           set_protective=lambda *a, **k: marked.append(a))
    out = await e._cancel_protective("AAPL", "sell1")
    assert out == [] and marked == []                      # refused cancel: row not marked cancelled


def test_mock_rejects_all_day_like_webull_us():
    import inspect
    from app.mock import server
    assert "Invalid trading_session for region 'us'" in inspect.getsource(server)
