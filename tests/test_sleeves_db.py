"""SleevesRepo (app/db/sleeves.py): state, holdings, reserved shares, option symbols, equity."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.db import DB


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.db", "mock", None, "acct1")
    yield d
    d.close()


def test_state_upsert_and_update(db):
    assert db.sleeve_state("momentum") is None
    db.upsert_sleeve_state("momentum", cash=500000.0, start_capital=500000.0, paused=False)
    st = db.sleeve_state("momentum")
    assert st["cash"] == 500000.0 and st["paused"] == 0
    db.upsert_sleeve_state("momentum", cash=1.5, paused=True, pause_reason="dd", meta={"a": 1})
    st = db.sleeve_state("momentum")
    assert st["cash"] == 1.5 and st["paused"] == 1 and st["pause_reason"] == "dd"
    assert st["start_capital"] == 500000.0            # untouched field survives
    assert '"a": 1' in st["meta_json"]
    assert set(db.sleeve_states()) == {"momentum"}
    with pytest.raises(ValueError):
        db.upsert_sleeve_state("momentum", bogus=1)


def test_holdings_set_and_delete(db):
    db.set_sleeve_holding("momentum", "aapl", "stock", 10, 100.0)
    db.set_sleeve_holding("momentum", "AAPL", "stock", 15, 101.0)      # set, not add
    rows = db.sleeve_holdings("momentum")
    assert len(rows) == 1 and rows[0]["symbol"] == "AAPL" and rows[0]["qty"] == 15
    db.set_sleeve_holding("vaa", "SPY", "stock", 3, 1.0)
    assert len(db.sleeve_holdings()) == 2
    db.set_sleeve_holding("momentum", "AAPL", "stock", 0, None)
    assert db.sleeve_holdings("momentum") == []
    assert len(db.sleeve_holdings()) == 1


def test_reserved_sums_non_swing_stock_only(db):
    db.set_sleeve_holding("momentum", "AAPL", "stock", 10, 1.0)
    db.set_sleeve_holding("vaa", "AAPL", "stock", 5, 1.0)
    db.set_sleeve_holding("swing", "AAPL", "stock", 99, 1.0)
    db.set_sleeve_holding("putwrite", "QQQ261231P00385000", "option", -2, 5.0)
    db.set_sleeve_holding("putwrite", "QQQ", "stock", 200, 385.0)
    assert db.sleeve_reserved() == {"AAPL": 15.0, "QQQ": 200.0}


def test_option_symbols_held_and_working_not_filled(db):
    db.set_sleeve_holding("putwrite", "qqq261231p00385000", "option", -2, 5.0)
    db.add_sleeve_order("putwrite", "QQQ270115P00380000", "option", "SELL", 1, 5.0, "spaaa", status="working")
    db.add_sleeve_order("putwrite", "QQQ270116P00380000", "option", "SELL", 1, 5.0, "spbbb", status="submitting")
    db.add_sleeve_order("putwrite", "QQQ270117P00380000", "option", "SELL", 1, 5.0, "spccc", status="filled")
    db.add_sleeve_order("putwrite", "QQQ270118P00380000", "option", "SELL", 1, 5.0, "spddd", status="cancelled")
    db.add_sleeve_order("momentum", "AAPL", "stock", "BUY", 1, 5.0, "smeee", status="working")
    assert db.sleeve_option_symbols() == {"QQQ261231P00385000", "QQQ270115P00380000", "QQQ270116P00380000"}


def test_orders_update_and_coids_in_known_ids(db):
    oid = db.add_sleeve_order("momentum", "AAPL", "stock", "buy", 5, 10.0, "sm" + "a" * 30)
    o = db.sleeve_orders("momentum")[0]
    assert o["side"] == "BUY" and o["status"] == "submitting"
    db.update_sleeve_order(oid, status="filled", filled_qty=5, avg_fill_price=9.9)
    assert db.sleeve_orders(statuses=["filled"])[0]["filled_qty"] == 5
    assert db.sleeve_orders(statuses=["working"]) == []
    with pytest.raises(ValueError):
        db.update_sleeve_order(oid, symbol="X")
    assert "sm" + "a" * 30 in db.sleeve_coids()
    assert "sm" + "a" * 30 in db.known_client_order_ids()


def test_equity_series_pivot(db):
    d1 = (date.today() - timedelta(days=2)).isoformat()
    d2 = (date.today() - timedelta(days=1)).isoformat()
    old = (date.today() - timedelta(days=900)).isoformat()
    db.record_sleeve_equity(d1, "momentum", 100.0, 10.0, 90.0)
    db.record_sleeve_equity(d1, "total", 300.0)
    db.record_sleeve_equity(d2, "momentum", 110.0)
    db.record_sleeve_equity(d2, "momentum", 111.0)         # upsert
    db.record_sleeve_equity(d2, "spy", 450.0)
    db.record_sleeve_equity(old, "momentum", 1.0)
    assert db.sleeve_equity_series(365) == [
        {"day": d1, "momentum": 100.0, "total": 300.0},
        {"day": d2, "momentum": 111.0, "spy": 450.0}]


def test_namespace_isolation(tmp_path):
    a = DB(tmp_path / "t.db", "mock", None, "A")
    b = DB(tmp_path / "t.db", "mock", None, "B")
    try:
        a.upsert_sleeve_state("momentum", cash=1.0)
        a.set_sleeve_holding("momentum", "AAPL", "stock", 5, 1.0)
        a.set_sleeve_holding("putwrite", "QQQ261231P00385000", "option", -1, 1.0)
        a.add_sleeve_order("momentum", "AAPL", "stock", "BUY", 1, 1.0, "sm" + "z" * 30, status="working")
        a.record_sleeve_equity(date.today().isoformat(), "momentum", 5.0)
        assert b.sleeve_state("momentum") is None and b.sleeve_states() == {}
        assert b.sleeve_holdings() == [] and b.sleeve_reserved() == {}
        assert b.sleeve_option_symbols() == set() and b.sleeve_orders() == []
        assert b.sleeve_coids() == set() and b.sleeve_equity_series() == []
        assert b.known_client_order_ids() == set()
        b.set_sleeve_holding("momentum", "AAPL", "stock", 0, None)    # delete in B must not touch A
        assert a.sleeve_reserved() == {"AAPL": 5.0}
    finally:
        a.close()
        b.close()
