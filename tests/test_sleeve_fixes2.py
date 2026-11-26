from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.sleeves import data, manager
from app.sleeves.strategies import momentum, putwrite, vaa

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
