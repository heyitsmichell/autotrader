"""Hooks that hide sleeve holdings from swing code."""
from __future__ import annotations

from types import SimpleNamespace

from app.broker.market_data import subtract_reserved
from app.config import load_settings
from app.db import DB
from app.engine.option_exits import OptionExitManager
from app.portfolio import Portfolio, swing_rows


def _raises():
    raise RuntimeError("db down")


def test_subtract_partial_and_full():
    assert subtract_reserved({"A": 10, "B": 5}, lambda: {"A": 4, "B": 5}) == {"A": 6}


def test_subtract_provider_none_empty_or_raising():
    raw = {"A": 10}
    assert subtract_reserved(raw, None) is raw
    assert subtract_reserved(raw, lambda: {}) is raw
    assert subtract_reserved(raw, _raises) is raw


def test_subtract_short_and_negative_untouched():
    assert subtract_reserved({"A": -3, "B": 10}, lambda: {"A": 2, "B": 1}) == {"A": -3, "B": 9}


def test_subtract_reserved_more_than_held_drops():
    assert subtract_reserved({"A": 5}, lambda: {"A": 8}) == {}


def _row(sym, qty, mv, upl):
    return {"symbol": sym, "qty": qty, "market_value": mv, "unrealized": upl}


def test_swing_rows_scales_and_drops():
    rows = [_row("A", 10, 1000.0, 100.0), _row("B", 5, 500.0, 50.0), _row("C", 4, 80.0, None)]
    out = swing_rows(rows, {"A": 4, "B": 5})
    assert [r["symbol"] for r in out] == ["A", "C"]
    a = out[0]
    assert a["qty"] == 6 and a["sleeve_qty"] == 4
    assert a["market_value"] == 600.0 and a["unrealized"] == 60.0
    assert out[1] == rows[2]


def test_swing_rows_none_and_short():
    rows = [_row("A", -5, -500.0, 0.0)]
    assert swing_rows(rows, None) is rows
    assert swing_rows(rows, {"A": 2}) == rows


def test_portfolio_sleeve_reserved_falls_back_on_error():
    p = Portfolio.__new__(Portfolio)
    p.db = SimpleNamespace(sleeve_reserved=_raises)
    assert p._sleeve_reserved() == {}
    p.db = SimpleNamespace(sleeve_reserved=lambda: {"A": 1.0})
    assert p._sleeve_reserved() == {"A": 1.0}


class _Broker:
    async def positions_text(self):
        return ("Symbol: QQQ261231P00385000 Qty: -2 Cost: 5.0 Mark: 4.0 Type: OPTION\n"
                "Symbol: AAPL260116C00200000 Qty: 1 Cost: 3.0 Mark: 4.0 Type: OPTION\n")


async def test_held_options_hides_sleeve_symbols(tmp_path):
    db = DB(tmp_path / "t.db", "mock", None, None)
    try:
        eng = SimpleNamespace(s=load_settings(), db=db, broker=_Broker())
        mgr = OptionExitManager(eng)
        assert {h.symbol for h in await mgr.held_options()} == {"QQQ261231P00385000", "AAPL260116C00200000"}
        db.set_sleeve_holding("putwrite", "QQQ261231P00385000", "option", -2, 5.0)
        assert {h.symbol for h in await mgr.held_options()} == {"AAPL260116C00200000"}
    finally:
        db.close()


async def test_broker_positions_subtract_reserved(test_settings):
    from app.broker import Broker
    b = Broker(test_settings)

    async def raw(fresh=False):
        return {"A": 10.0, "B": 3.0}
    b.positions_raw = raw
    assert await b.positions() == {"A": 10.0, "B": 3.0}
    b.__dict__["reserved_positions"] = lambda: {"A": 4.0, "B": 3.0}
    assert await b.positions() == {"A": 6.0}


async def test_engine_wires_reserved_positions(test_engine):
    assert test_engine.broker.__dict__["reserved_positions"] == test_engine.db.sleeve_reserved
