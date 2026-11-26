from types import SimpleNamespace

from app.sleeves import manager, registry


def _me(cash, holds):
    sid = registry.NON_SWING[0]
    db = SimpleNamespace(
        sleeve_state=lambda s: {"cash": cash} if s == sid else None,
        sleeve_holdings=lambda s: holds,
        sleeve_orders=lambda *a, **k: [],
    )
    return SimpleNamespace(db=db)


def _snap(**kw):
    base = {"options": {}, "last": {"AAA": 10.0}, "net_liq": 2000.0, "cash": 1200.0}
    base.update(kw)
    return base


def test_swing_cash_is_broker_cash_minus_other_sleeve_cash():
    me = _me(100.0, [])
    v = manager.SleeveManager._value(me, _snap())
    assert v["swing"]["cash"] == 1100.0


def test_swing_cash_none_without_broker_cash():
    v = manager.SleeveManager._value(_me(100.0, []), _snap(cash=None))
    assert v["swing"]["cash"] is None


def test_sleeve_stock_valued_at_broker_mark():
    holds = [{"symbol": "AAA", "kind": "stock", "qty": 10, "avg_cost": 9.0}]
    v = manager.SleeveManager._value(_me(0.0, holds), _snap(mv_px={"AAA": 11.0}))
    sid = registry.NON_SWING[0]
    assert v[sid]["invested"] == 110.0
    assert v["swing"]["equity"] == 1890.0
