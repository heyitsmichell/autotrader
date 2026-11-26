from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.sleeves import manager
from app.sleeves.strategies import putwrite

ET = ZoneInfo("America/New_York")


def _put(day):
    return {"strike": 500.0, "expiration": day}


def _at(day, h, m):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


def test_early_close_day():
    d = date(2026, 11, 27)
    assert putwrite.should_close(_put(d), 490, d, _at(d, 12, 35))
    assert not putwrite.should_close(_put(d), 490, d, _at(d, 12, 20))
    assert not putwrite.should_close(_put(d), 510, d, _at(d, 12, 35))
    assert not putwrite.is_expired(_put(d), d, _at(d, 12, 59))
    assert putwrite.is_expired(_put(d), d, _at(d, 13, 0))


def test_regular_day_unchanged():
    d = date(2026, 11, 25)
    assert not putwrite.should_close(_put(d), 490, d, _at(d, 15, 29))
    assert putwrite.should_close(_put(d), 490, d, _at(d, 15, 30))
    assert not putwrite.is_expired(_put(d), d, _at(d, 15, 59))
    assert putwrite.is_expired(_put(d), d, _at(d, 16, 0))


def test_empty_snapshot_does_not_settle():
    logs, writes = [], []
    db = SimpleNamespace(log=lambda *a, **k: logs.append(a),
                         set_sleeve_holding=lambda *a, **k: writes.append(a))
    me = SimpleNamespace(db=db)
    h = {"symbol": "SPY261127P00500000", "qty": -1}
    occ = {"underlying": "SPY", "strike": 500.0, "expiration": "2026-11-27"}
    manager.SleeveManager._settle_expired(me, h, occ, {"raw_qty": {}, "options": {}, "last": {}})
    assert not writes and logs


def test_open_sell_counts_as_pending_proceeds():
    db = SimpleNamespace(
        sleeve_state=lambda sid: {"cash": 1000.0} if sid == "momentum" else None,
        sleeve_holdings=lambda sid: [],
        sleeve_orders=lambda sid, n, st=None: [
            {"side": "SELL", "kind": "stock", "qty": 10, "filled_qty": 4, "limit_price": 50.0},
            {"side": "SELL", "kind": "option", "qty": 1, "filled_qty": 0, "limit_price": 2.0,
             "position_intent": "SELL_TO_OPEN"}] if sid == "momentum" else [])
    me = SimpleNamespace(db=db)
    v = manager.SleeveManager._value(me, {"options": {}, "last": {}, "net_liq": 5000.0})
    assert v["momentum"]["equity"] == 1000.0 + 6 * 50.0
    assert v["momentum"]["cash"] == 1000.0
