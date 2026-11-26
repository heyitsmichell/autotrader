import asyncio
from types import SimpleNamespace

import pytest

from app.broker.errors import OrderGuardError
from app.broker.guard import OrderGuard
from app.engine.cycle import CycleMixin


def _g():
    return OrderGuard(SimpleNamespace(max_order_qty=100))


def _sell(qty, **kw):
    return {"kind": "stock", "symbol": "AAPL", "side": "SELL", "quantity": qty,
            "order_type": "LIMIT", "opening": False, **kw}


def test_close_over_max_still_allowed():
    # exits are never capped by max_order_qty (a large holding must stay closable)
    _g().check(_sell(5382))


def test_close_over_held_refused():
    with pytest.raises(OrderGuardError):
        _g().check(_sell(50, held=40))


def test_close_zero_refused():
    with pytest.raises(OrderGuardError):
        _g().check(_sell(0))


def test_normal_close_allowed():
    _g().check(_sell(40, held=40))


def test_stop_allowed_even_over_max():
    _g().check({"kind": "stock", "symbol": "AAPL", "side": "SELL", "quantity": 500,
                "price": 1.0, "opening": False})


def test_option_close_only_needs_qty():
    g = _g()
    g.check({"kind": "option", "symbol": "X", "side": "SELL", "quantity": 500, "opening": False})
    with pytest.raises(OrderGuardError):
        g.check({"kind": "option", "symbol": "X", "side": "SELL", "quantity": 0, "opening": False})


class _Broker:
    async def positions(self):
        raise RuntimeError("boom")

    async def open_orders_text(self):
        return "No data"


class _Eng(CycleMixin):
    def __init__(self, broker):
        self.broker = broker
        self.db = SimpleNamespace(pending_proposals=lambda: [])


def test_positions_failure_assumes_work():
    assert asyncio.run(_Eng(_Broker())._has_actionable_work([])) is True


def test_open_orders_failure_assumes_work():
    class B(_Broker):
        async def positions(self):
            return {}

        async def open_orders_text(self):
            raise RuntimeError("x")
    assert asyncio.run(_Eng(B())._has_actionable_work([])) is True
