from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from app.sleeves import data, manager
from app.sleeves.strategies import momentum, putwrite, vaa
import asyncio
from datetime import date
import pytest
from app import options_ml
from app.broker import BrokerError
from app.broker.orders import OrdersMixin
from app.sleeves import manager as sm
from app.sleeves.strategies import rebalance
from app.sleeves import manager, registry

ET = ZoneInfo("America/New_York")


def test_tick_rounding():
    assert putwrite.round_tick(3.12, "nearest") == 3.10
    assert putwrite.round_tick(3.12, "up") == 3.15
    assert putwrite.round_tick(2.123, "up") == 2.13
    assert putwrite.round_tick(2.123, "up", 0.05) == 2.15
    assert putwrite.sell_open_limit(3.00, 3.40) >= 3.00
    assert putwrite.sell_open_limit(1.00, 1.04) in (1.0, 1.01, 1.02, 1.03, 1.04)


def test_drop_today_before_close():
    idx = pd.to_datetime(["2026-11-24", "2026-11-25"])
    df = pd.DataFrame({"A": [1.0, 2.0]}, index=idx)
    mid = datetime(2026, 11, 25, 11, 0, tzinfo=ET)
    aft = datetime(2026, 11, 25, 16, 30, tzinfo=ET)
    assert len(data.drop_incomplete_today(df, mid)) == 1
    assert len(data.drop_incomplete_today(df, aft)) == 2


def test_momentum_excludes_missing_symbol():
    n = 300
    c = pd.DataFrame({f"S{i}": np.linspace(10, 10 + i + 1, n) for i in range(4)})
    c["BAD"] = np.nan
    c.loc[0:5, "BAD"] = 1.0
    t = momentum.targets(c, n=2)
    assert len(t) == 2 and "BAD" not in t


def test_vaa_ffill_one_gap():
    n = 300
    syms = vaa.G12 + ["IEF", "SHY"]
    c = pd.DataFrame({s: np.linspace(10, 20, n) for s in syms})
    c.loc[n - 1, "SPY"] = np.nan
    assert vaa.targets(c)


def _me(cash_prev=None):
    logs, writes = [], []
    tx = SimpleNamespace(__enter__=None)

    class T:
        def __enter__(s): return s
        def __exit__(s, *a): return False
    db = SimpleNamespace(log=lambda *a, **k: logs.append((a, k)),
                         sleeve_reserved=lambda: {}, open_trades=lambda: [],
                         transaction=lambda: T(),
                         set_sleeve_holding=lambda *a, **k: writes.append(a),
                         sleeve_state=lambda sid: {}, upsert_sleeve_state=lambda *a, **k: None)
    me = SimpleNamespace(db=db, _event=lambda *a, **k: None, _add_cash=lambda *a: None)
    me._assignment_verdict = lambda *a: manager.SleeveManager._assignment_verdict(me, *a)
    if cash_prev is not None:
        me._putwrite_prev_cash = cash_prev
    return me, logs, writes


H = {"symbol": "SPY261127P00500000", "qty": -1}


OCC = {"underlying": "SPY", "strike": 500.0, "expiration": "2026-11-27"}


def test_assigned_needs_cash_match():
    me, logs, writes = _me(cash_prev=100000)
    snap = {"raw_qty": {"SPY": 100}, "options": {}, "cash": 50000}
    manager.SleeveManager._settle_expired(me, H, OCC, snap)
    assert any(w[1] == "SPY" for w in writes)


def test_cash_mismatch_is_ambiguous():
    me, logs, writes = _me(cash_prev=100000)
    snap = {"raw_qty": {"SPY": 100}, "options": {}, "cash": 99990}
    manager.SleeveManager._settle_expired(me, H, OCC, snap)
    assert not writes and logs[-1][1].get("alert")


def test_partial_shares_ambiguous_and_expired_clean():
    me, logs, writes = _me()
    manager.SleeveManager._settle_expired(me, H, OCC, {"raw_qty": {"SPY": 40}, "options": {}})
    assert not writes
    me, logs, writes = _me()
    manager.SleeveManager._settle_expired(me, H, OCC, {"raw_qty": {"QQQ": 1}, "options": {}})
    assert writes and not any(w[1] == "SPY" and w[3] for w in writes if len(w) > 3 and w[3])


def test_broker_assign_text_without_shares_ambiguous():
    me, logs, writes = _me()
    snap = {"raw_qty": {"QQQ": 1}, "options": {}, "fills_text": "SPY261127P00500000 ASSIGNED"}
    manager.SleeveManager._settle_expired(me, H, OCC, snap)
    assert not writes


def _bracket(limit, tp, sl):
    coids = {"entry": "e", "tp": "t", "sl": "s"}
    return asyncio.run(OrdersMixin.place_bracket(SimpleNamespace(), "AAPL", 1, limit, tp, sl, coids, "c"))


@pytest.mark.parametrize("limit,tp,sl", [(100, 110, 0), (0, 110, 90), (100, 99, 90), (100, 110, 101),
                                         (100, 110, 100), (100, -1, 90)])
def test_bracket_rejects_bad_prices(limit, tp, sl):
    with pytest.raises(BrokerError, match="nothing sent"):
        _bracket(limit, tp, sl)


def test_rebalance_missing_prices_and_fractional():
    assert rebalance.missing_prices({"A": 1.5}, {"B": 0.5}, {"A": 10}) == ["B"]
    assert rebalance.missing_prices({"A": 0}, {}, {}) == []
    out = rebalance.diff({"A": 1.5}, {}, 1000, {"A": 10}, min_trade_usd=0)
    assert out == [("A", "SELL", 1.5)]


def test_fee_applied_on_fill():
    cash = []
    fake = SimpleNamespace(_add_cash=lambda sid, x: cash.append(x),
                           _adjust_holding=lambda *a, **k: None)
    fake._apply_fill_trade = lambda r, q, px: None
    opt = {"sleeve": "putwrite", "symbol": "X", "kind": "option", "side": "SELL"}
    sm.SleeveManager._apply_fill(fake, opt, 2, 1.0)
    assert cash == [-2 * sm.OPTION_FEE_PER_CONTRACT]
    cash.clear()
    sm.SleeveManager._apply_fill(fake, {"sleeve": "m", "symbol": "A", "kind": "stock", "side": "SELL"}, 100, 50.0)
    assert cash == [-round(5000 * sm.STOCK_SELL_FEE_RATE, 4)]
    cash.clear()
    sm.SleeveManager._apply_fill(fake, {"sleeve": "m", "symbol": "A", "kind": "stock", "side": "BUY"}, 100, 50.0)
    assert cash == []


def test_expiry_intrinsic():
    sym = "AAPL240119C00150000"
    assert options_ml.expiry_intrinsic(sym, "2024-01-19", lambda u, d: 155.0) == 5.0
    assert options_ml.expiry_intrinsic(sym, "2024-01-19", lambda u, d: 140.0) == 0.0
    assert options_ml.expiry_intrinsic(sym, "2024-01-19", lambda u, d: None) is None
    assert options_ml.expiry_intrinsic("AAPL240119P00150000", "2024-01-19", lambda u, d: 140.0) == 10.0


class _DB:
    def __init__(self):
        self.added = []

    def proposals(self, n):
        return [{"id": 1, "symbol": "AAPL240119C00150000", "side": "BUY", "position_intent": "BUY_TO_OPEN",
                 "status": "placed", "limit_price": 2.0, "created_at": "x", "expiration_date": "2024-01-19",
                 "instrument_json": {"selection": {"a": 1}, "underlying": "AAPL"}}]

    def has_option_outcome(self, i):
        return False

    def get_trade(self, i):
        return {"entry_fill_price": 2.0}

    def add_option_outcome(self, row):
        self.added.append(row)


def test_outcomes_expired_uses_intrinsic_and_skips():
    today = date(2024, 2, 1)
    db = _DB()
    assert options_ml.record_closed_outcomes(db, set(), today, positions_ok=False) == 0
    assert options_ml.record_closed_outcomes(db, set(), today, close_lookup=lambda u, d: None) == 0
    assert options_ml.record_closed_outcomes(db, set(), today, close_lookup=lambda u, d: 155.0) == 1
    assert db.added[0]["exit_premium"] == 5.0 and db.added[0]["exit_reason"] == "expired"


def _me_swing_cash(cash, holds):
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
    me = _me_swing_cash(100.0, [])
    v = manager.SleeveManager._value(me, _snap())
    assert v["swing"]["cash"] == 1100.0


def test_swing_cash_none_without_broker_cash():
    v = manager.SleeveManager._value(_me_swing_cash(100.0, []), _snap(cash=None))
    assert v["swing"]["cash"] is None


def test_sleeve_stock_valued_at_broker_mark():
    holds = [{"symbol": "AAA", "kind": "stock", "qty": 10, "avg_cost": 9.0}]
    v = manager.SleeveManager._value(_me_swing_cash(0.0, holds), _snap(mv_px={"AAA": 11.0}))
    sid = registry.NON_SWING[0]
    assert v[sid]["invested"] == 110.0
    assert v["swing"]["equity"] == 1890.0
