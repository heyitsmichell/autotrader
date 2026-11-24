"""Broker-level order guard, negative cache, coid reuse, router selection, mode warning."""
import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from app.broker import (Broker, BrokerError, OrderGuardError, OrderRouter, SdkTransport,
                        is_mutating_tool, mode_warning_for, select_execution)
from app.config import load_settings


def _result(text, error=False):
    return SimpleNamespace(content=[TextContent(type="text", text=text)], is_error=error)


class FakeSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _broker(session=None, killed=False, **kw):
    s = replace(load_settings(), broker_backoff_seconds=0.01, broker_mode="mock",
                webull_account_id="A1", **kw)
    b = Broker(s)
    if session is not None:
        b.session = session
    b.set_guard_context(kill_switch=lambda: killed)
    return b


@pytest.fixture(autouse=True)
def _session_open(monkeypatch):
    monkeypatch.setattr("app.broker.orders.market.order_session", lambda at=None: "CORE")
    monkeypatch.setattr("app.broker.core.random", None, raising=False)


@pytest.mark.asyncio
async def test_kill_blocks_opening_but_not_closing():
    sess = FakeSession([_result("client_order_id: x")])
    b = _broker(sess, killed=True)
    with pytest.raises(OrderGuardError):
        await b.place_order("AAPL", "BUY", 1, 100.0, "c1")
    assert sess.calls == []
    await b.place_order("AAPL", "SELL", 1, 100.0, "c2")          # closing: allowed
    await b.place_stop("AAPL", 1, 90.0, "c3")                    # protective stop: allowed
    await b.cancel_order("c1")
    assert [c[0] for c in sess.calls] == ["place_stock_order", "place_stock_order", "cancel_order"]


@pytest.mark.asyncio
async def test_kill_blocks_bracket_and_options_open_only():
    sess = FakeSession([_result("ok")])
    b = _broker(sess, killed=True)
    with pytest.raises(OrderGuardError):
        await b.place_bracket("AAPL", 1, 100.0, 110.0, 95.0,
                              {"entry": "e", "tp": "t", "sl": "s"}, "combo")
    with pytest.raises(OrderGuardError):
        await b.place_option_single_order("AAPL", "BUY", 1, "CALL", 150, "2030-01-18", 1.0, "o1")
    await b.place_option_single_order("AAPL", "SELL", 1, "CALL", 150, "2030-01-18", 1.0, "o2",
                                      position_intent="SELL_TO_CLOSE")
    assert [c[1].get("client_order_id") for c in sess.calls] == ["o2"]


@pytest.mark.asyncio
async def test_flatten_mode_exempts_kill():
    sess = FakeSession([_result("ok")])
    b = _broker(sess, killed=True)
    async with b.flatten_mode():
        await b.place_order("AAPL", "BUY", 1, 100.0, "c1")
    assert len(sess.calls) == 1
    with pytest.raises(OrderGuardError):                          # exemption ended with the block
        await b.place_order("AAPL", "BUY", 1, 100.0, "c2")
    await b.place_order("AAPL", "BUY", 1, 100.0, "c3", allow_when_killed=True)
    assert len(sess.calls) == 2


@pytest.mark.asyncio
async def test_caps_and_whitelist():
    sess = FakeSession([_result("ok")])
    b = _broker(sess, max_order_usd=500.0, max_order_qty=5, symbol_whitelist=["AAPL"],
                options_max_contracts_per_order=2)
    with pytest.raises(OrderGuardError, match="notional"):
        await b.place_order("AAPL", "BUY", 4, 200.0, "c1")       # $800 > $500
    with pytest.raises(OrderGuardError, match="max_order_qty"):
        await b.place_order("AAPL", "BUY", 6, 10.0, "c2")
    with pytest.raises(OrderGuardError, match="whitelist"):
        await b.place_order("MSFT", "BUY", 1, 10.0, "c3")
    with pytest.raises(OrderGuardError, match="contracts"):
        await b.place_option_single_order("AAPL", "BUY", 3, "CALL", 150, "2030-01-18", 0.1, "o1")
    # option notional = price x qty x 100: 2 x 3.0 x 100 = $600 > $500
    with pytest.raises(OrderGuardError, match="notional"):
        await b.place_option_single_order("AAPL", "BUY", 2, "CALL", 150, "2030-01-18", 3.0, "o2")
    await b.place_order("AAPL", "BUY", 2, 100.0, "ok1")
    await b.place_option_single_order("AAPL", "BUY", 2, "CALL", 150, "2030-01-18", 2.0, "ok2")
    # exits are never capped or whitelisted
    await b.place_order("MSFT", "SELL", 99, 1000.0, "exit")
    assert [c[1]["client_order_id"] for c in sess.calls] == ["ok1", "ok2", "exit"]


@pytest.mark.asyncio
async def test_negative_cache_applies_to_reads_not_mutations():
    sess = FakeSession([_result("Server error: 404 Not Found", True)])
    b = _broker(sess)
    for _ in range(2):
        with pytest.raises(BrokerError):
            await b.call("get_order_detail", order="x")
    assert len(sess.calls) == 1                                   # second read: negative cache
    for _ in range(2):
        with pytest.raises(BrokerError):
            await b.call("cancel_order", account_id="A1", client_order_id="x")
    assert len(sess.calls) == 3                                   # cancel hit the server both times
    assert is_mutating_tool("place_stock_order") and not is_mutating_tool("get_open_orders")


@pytest.mark.asyncio
async def test_coid_reused_when_rate_limit_retry(monkeypatch):
    monkeypatch.setattr("app.broker.transport.random.uniform", lambda a, b: 0.0)
    sess = FakeSession([_result("Error: 429 too many requests", True), _result("client_order_id: keep")])
    b = _broker(sess)
    await b.place_order("AAPL", "BUY", 1, 100.0, "my-coid")
    assert len(sess.calls) == 2
    assert {c[1]["client_order_id"] for c in sess.calls} == {"my-coid"}


def test_router_selection():
    base = load_settings()
    assert select_execution(replace(base, broker_mode="mock", broker_execution="sdk")) == "mock"
    assert select_execution(replace(base, broker_mode="webull", broker_execution="mcp")) == "mcp"
    assert select_execution(replace(base, broker_mode="webull", broker_execution="sdk")) == "sdk"
    assert Broker(replace(base, broker_mode="mock")).router.transport_name.startswith("McpTransport")


@pytest.mark.asyncio
async def test_sdk_transport_fails_closed_on_unmapped():
    s = replace(load_settings(), broker_mode="webull", broker_execution="sdk")
    called = []

    async def mcp_call(name, **a):
        called.append(name)
        return "mcp"

    r = OrderRouter(s, mcp_call, sdk=SdkTransport(s, trade_client=SimpleNamespace()))
    with pytest.raises(BrokerError, match="not supported"):
        await r.submit("place_option_single_order", account_id="A1")
    with pytest.raises(BrokerError):
        await r.submit("place_stock_order", account_id="A1", symbol="AAPL", side="BUY",
                       order_type="MARKET", time_in_force="DAY", quantity=1,
                       trading_session="CORE", client_order_id="c")   # MARKET unmapped
    with pytest.raises(BrokerError):
        await r.submit("place_stock_order", account_id="A1", symbol="AAPL", side="BUY",
                       order_type="LIMIT", time_in_force="DAY", quantity=1, limit_price=1,
                       trading_session="CORE", client_order_id="c", surprise=1)
    assert called == []                                           # never falls back to MCP


@pytest.mark.asyncio
async def test_sdk_transport_maps_limit_order_with_caller_coid():
    sent = {}

    class Order:
        def place_order(self, account_id, new_orders, client_combo_order_id=None):
            sent.update(account_id=account_id, orders=new_orders)
            return SimpleNamespace(status_code=200, json=lambda: {"client_order_id": new_orders[0]["client_order_id"]})

    s = replace(load_settings(), broker_mode="webull", broker_execution="sdk")
    t = SdkTransport(s, trade_client=SimpleNamespace(order_v3=Order()))
    out = await t.call("place_stock_order", account_id="A1", symbol="AAPL", side="BUY",
                       order_type="LIMIT", time_in_force="DAY", quantity=2, limit_price=10.5,
                       trading_session="CORE", client_order_id="mine")
    assert "client_order_id: mine" in out
    o = sent["orders"][0]
    assert o["client_order_id"] == "mine" and o["quantity"] == "2" and o["limit_price"] == "10.5"


def test_mode_warning(monkeypatch, caplog, tmp_path):
    s = replace(load_settings(), broker_mode="mock")
    monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
    monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)
    assert mode_warning_for(s) is None and Broker(s).mode_warning is None
    monkeypatch.setenv("WEBULL_APP_KEY", "abc")
    with caplog.at_level(logging.WARNING):
        b = Broker(s)
    assert b.mode_warning and "BROKER_MODE=mock" in b.mode_warning
    assert any("BROKER_MODE=mock" in r.message for r in caplog.records)
    logged = []
    b.set_guard_context(db=SimpleNamespace(log=lambda level, msg: logged.append((level, msg))))
    assert logged and logged[0][0] == "warn"
    assert mode_warning_for(replace(s, broker_mode="webull")) is None


@pytest.mark.asyncio
async def test_option_snapshot_as_of_and_quotes():
    text = ("=== Option Snapshot ===\n  AAPL300119C00150000  Bid: 1.00  Ask: 1.10  Last: 1.05  Vol: 5  "
            "OI: 9  IV: 0.30  Delta: 0.500  Gamma: 0.020  Theta: -0.050  Vega: 0.100  AsOf: 1700000000.500\n"
            "  AAPL300119P00150000  Bid: 2.00  Ask: 2.20  Last: 2.10  Vol: 5  OI: 9  IV: 0.30  "
            "Delta: -0.500  Gamma: 0.020  Theta: -0.050  Vega: 0.100")
    sess = FakeSession([_result(text)])
    b = _broker(sess)
    snaps = await b.parsed_option_snapshots(["AAPL300119C00150000", "AAPL300119P00150000"])
    assert snaps["AAPL300119C00150000"]["as_of"] == 1700000000.5
    assert snaps["AAPL300119P00150000"]["bid"] == 2.0                # second mock line is its own symbol
    assert snaps["AAPL300119P00150000"]["as_of"] > 1.7e9              # fetch time when API has none
    q = await b.option_quote("AAPL300119C00150000")
    assert q.two_sided and q.delta == 0.5 and q.as_of == 1700000000.5


def test_sdk_transport_uat_endpoint_injection():
    """UAT routes the SDK to the sandbox trading host; prod uses the SDK defaults; anything else
    fails closed before a client is built."""
    pytest.importorskip("webull_openapi_mcp")
    added = []
    api = SimpleNamespace(add_endpoint=lambda region, host, api_type=None: added.append((region, host)))
    SdkTransport._configure_endpoints(api, "us", "uat")
    assert added and added[0][0] == "us" and "sandbox" in added[0][1]
    added.clear()
    SdkTransport._configure_endpoints(api, "us", "prod")
    assert added == []
    with pytest.raises(BrokerError, match="unknown WEBULL_ENVIRONMENT"):
        SdkTransport._configure_endpoints(api, "us", "staging")
    with pytest.raises(BrokerError, match="no UAT trading endpoint"):
        SdkTransport._configure_endpoints(api, "xx", "uat")


@pytest.mark.asyncio
async def test_sdk_combo_legs_match_webull_shape():
    """Regression (UAT 2026-11-24): Webull rejected bracket legs without entrust_type."""
    sent = {}

    class Order:
        def place_order(self, account_id, new_orders, client_combo_order_id=None):
            sent.update(orders=new_orders, combo=client_combo_order_id)
            return SimpleNamespace(status_code=200, json=lambda: {"client_order_id": new_orders[0]["client_order_id"]})

    s = replace(load_settings(), broker_mode="webull", broker_execution="sdk")
    t = SdkTransport(s, trade_client=SimpleNamespace(order_v3=Order()))
    leg = {"symbol": "SPYM", "quantity": 1, "support_trading_session": "ALL", "market": "US",
           "instrument_type": "EQUITY"}
    legs = [{**leg, "combo_type": "MASTER", "side": "BUY", "order_type": "LIMIT", "limit_price": 85.47,
             "time_in_force": "DAY", "client_order_id": "e"},
            {**leg, "combo_type": "STOP_PROFIT", "side": "SELL", "order_type": "LIMIT", "limit_price": 89.91,
             "time_in_force": "GTC", "client_order_id": "t"},
            {**leg, "combo_type": "STOP_LOSS", "side": "SELL", "order_type": "STOP_LOSS", "stop_price": 81.91,
             "time_in_force": "GTC", "client_order_id": "s"}]
    await t.call("place_stock_combo_order", account_id="A1", orders=legs, client_combo_order_id="c")
    assert sent["combo"] == "c" and len(sent["orders"]) == 3
    assert all(o["entrust_type"] == "QTY" and o["quantity"] == "1" for o in sent["orders"])
    assert sent["orders"][2]["stop_price"] == "81.91" and "limit_price" not in sent["orders"][2]
    with pytest.raises(BrokerError, match="unmapped combo leg"):
        await t.call("place_stock_combo_order", account_id="A1", client_combo_order_id="c",
                     orders=[{**legs[0], "surprise": 1}])
