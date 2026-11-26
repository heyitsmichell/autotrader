import asyncio
from types import SimpleNamespace

import pytest

from app.broker import BrokerError
from app.broker.guard import OrderGuard
from app.broker.parsers import parse_snapshot
from app.broker import transport as tr
from app.engine import flatten as fl
from app.engine.reconcile import ReconcileMixin
from app.engine.execution import ExecutionMixin  # noqa: F401  (import check)


def _settings(**kw):
    base = dict(max_order_qty=100, max_order_usd=1000.0, options_max_contracts_per_order=10,
                market_notional_buffer_pct=1.0, symbol_whitelist=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_guard_missing_setting_raises():
    with pytest.raises(AttributeError):
        OrderGuard(SimpleNamespace(max_order_qty=100))


def test_guard_negative_cap_rejected():
    with pytest.raises(ValueError):
        OrderGuard(_settings(max_order_usd=-1))


def test_parse_snapshot_ignores_foreign_price_line():
    text = "AAPL Price: 10 PreClose: 9\nFoo Price: 5\nVol: 100\n"
    assert set(parse_snapshot(text)) == {"AAPL"}
    assert set(parse_snapshot("Foo Price: 5\n", ["FOO"])) == {"FOO"}
    assert parse_snapshot("Foo Price: 5\n") == {}


def test_not_found_regex_and_cap():
    assert tr._NOT_FOUND_RX.search("HTTP 404")
    assert tr._NOT_FOUND_RX.search("Order Not Found")
    assert not tr._NOT_FOUND_RX.search("price 1404.5 error")
    assert tr.NEGATIVE_CACHE_MAX == 512


class _Eng(ReconcileMixin):
    def __init__(self, orders_text, lock=None):
        self.broker = SimpleNamespace(open_orders_text=self._open)
        self._t = orders_text
        self._exec_lock = lock or asyncio.Lock()
        self.db = SimpleNamespace(unresolved_proposals=lambda: [])

    async def _open(self):
        if isinstance(self._t, Exception):
            raise self._t
        return self._t


async def test_open_order_exists_false_on_error():
    assert await _Eng(BrokerError("x", transport=True))._open_order_exists("c1") is False


async def test_reconcile_waits_for_lock_then_runs():
    lock = asyncio.Lock()
    eng = _Eng("", lock)
    eng.db = SimpleNamespace(unresolved_proposals=lambda: [{"id": 1}])
    seen = []

    async def _open():
        seen.append(1)
        raise BrokerError("stop here")
    eng.broker.open_orders_text = _open
    await lock.acquire()
    task = asyncio.create_task(eng.reconcile_orders())
    await asyncio.sleep(0.05)
    assert not seen
    lock.release()
    await task
    assert seen == [1]


def test_flatten_extended_constant():
    assert fl.FLATTEN_EXTENDED_SLIPPAGE_PCT > 0


async def test_flatten_symbol_extended_uses_session_price(monkeypatch):
    monkeypatch.setattr(fl, "order_session", lambda: "ALL")
    sold = {}

    class B:
        async def get_snapshot(self, s):
            return {"regular_price": 100.0, "ext_price": 90.0}

        async def last_price(self, s):
            return 100.0

    eng = SimpleNamespace(
        broker=B(), db=SimpleNamespace(log=lambda *a, **k: None),
        s=SimpleNamespace(fractional_enabled=False, flatten_slippage_pct=0.5,
                          max_order_qty=100, max_order_usd=100000))

    async def _sell(sym, n, limit, actor, reason):
        sold["limit"] = limit
        return "placed"
    eng._flatten_sell = _sell
    res = {"orders": [], "errors": []}
    await fl.FlattenMixin._flatten_symbol(eng, "AAPL", 1, "a", "r", res)
    assert sold["limit"] == round(90.0 * (1 - fl.FLATTEN_EXTENDED_SLIPPAGE_PCT / 100), 2)


async def test_run_guard_blocks_second_call():
    from app.api import controls
    started = []

    class Eng:
        kill_switch = False
        running = False

        def run_cycle(self, why):
            async def c():
                await asyncio.sleep(0.05)
            started.append(1)
            return c()

    state = SimpleNamespace(broker=SimpleNamespace(connected=True))
    req = SimpleNamespace(app=SimpleNamespace(state=state))
    import app.api.controls as c
    c.engine = lambda r: Eng()
    c.background = lambda r, coro: asyncio.create_task(coro)
    await controls.run_now(req)
    with pytest.raises(Exception):
        await controls.run_now(req)
    assert len(started) == 1
    await asyncio.sleep(0.1)
    await controls.run_now(req)
    assert len(started) == 2
    await asyncio.sleep(0.1)
