"""Flatten race / oversell and broker call timeout."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.broker import Broker, BrokerError
from app.config import load_settings
from app.engine import flatten as flatten_mod


_real_sleep = asyncio.sleep


def _orders(*rows):
    out = ""
    for coid, sym, side, qty in rows:
        out += (f"Client Order ID: g{coid}\n  Client Order ID: {coid}\n  Symbol: {sym}\n"
                f"  Side: {side}\n  Status: SUBMITTED\n  Total Quantity: {qty}\n")
    return out


def _patch(eng, monkeypatch, orders_after):
    sells = []
    state = {"cancelled": False}

    async def open_orders_text():
        return orders_after if state["cancelled"] else _orders(
            ("s1", "AAPL", "SELL", 10), ("s2", "MSFT", "SELL", 4))

    async def cancel_order(coid):
        if coid == "s1":
            state["cancelled"] = True       # first cancel "takes" the snapshot flip, s2 sticks
        return "Cancelled" if coid != "s2" else "pending"

    async def positions(_b):
        return {"AAPL": 10, "MSFT": 10}

    async def fake_sell(sym, qty, limit, actor, reason):
        await asyncio.sleep(0.01)
        sells.append((sym, qty))
        return "placed"

    async def last_price(sym):
        return 100.0

    async def ks(*a, **k):
        await asyncio.sleep(0.01)

    monkeypatch.setattr(eng.broker, "open_orders_text", open_orders_text)
    monkeypatch.setattr(eng.broker, "cancel_order", cancel_order)
    monkeypatch.setattr(eng.broker, "last_price", last_price)
    monkeypatch.setattr(flatten_mod, "fresh_positions", positions)
    monkeypatch.setattr(flatten_mod, "order_session", lambda *a, **k: "CORE")
    monkeypatch.setattr(flatten_mod, "cancel_reply_confirmed", lambda r: r == "Cancelled")
    monkeypatch.setattr(eng, "set_kill_switch", ks)
    monkeypatch.setattr(eng, "_flatten_sell", fake_sell)

    async def noopt(*a, **k):
        return []
    monkeypatch.setattr(eng.option_exits, "close_all", noopt, raising=False)
    monkeypatch.setattr(flatten_mod, "asyncio", SimpleNamespace(sleep=lambda *_: _real_sleep(0)))
    return sells


@pytest.mark.asyncio
async def test_concurrent_flatten_refused(test_engine, monkeypatch):
    sells = _patch(test_engine, monkeypatch, _orders(("s2", "MSFT", "SELL", 4)))
    r = await asyncio.gather(test_engine.flatten("u", "t"), test_engine.flatten("u", "t"),
                             return_exceptions=True)
    assert sum(isinstance(x, ValueError) for x in r) == 1
    assert sorted(s[0] for s in sells) == ["AAPL", "MSFT"]
    assert not test_engine.flattening


@pytest.mark.asyncio
async def test_flatten_skips_shares_in_uncancelled_sell(test_engine, monkeypatch):
    sells = _patch(test_engine, monkeypatch, _orders(("s2", "MSFT", "SELL", 4)))
    res = await test_engine.flatten("u", "t")
    assert dict(sells) == {"AAPL": 10, "MSFT": 6}
    assert any("MSFT" in e for e in res["errors"])


# -- broker call timeout ---------------------------------------------------
class SlowSession:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, arguments=None):
        self.calls += 1
        await asyncio.sleep(5)


def _broker(sess):
    b = Broker(replace(load_settings(), broker_call_timeout_seconds=0.05))
    b.session = sess
    return b


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_stock_snapshot", "place_order"])
async def test_call_timeout_is_transport_error_without_retry(tool):
    sess = SlowSession()
    with pytest.raises(BrokerError) as ei:
        await _broker(sess).call(tool, symbol="AAPL")
    assert ei.value.transport and "timed out" in str(ei.value)
    assert sess.calls == 1
