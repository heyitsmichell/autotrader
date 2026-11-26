"""amihud_quality shadow: target math on a tiny synthetic universe."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.shadows import registry, strategies


def _frames(n=300):
    idx = pd.bdate_range("2024-01-01", periods=n)
    syms = ["A", "B", "C", "D", "E", "F"]
    rng = np.random.default_rng(0)
    close = pd.DataFrame({s: 100 * np.exp(np.cumsum(rng.normal(0.001 * (i + 1), 0.01, n)))
                          for i, s in enumerate(syms)}, index=idx)
    vol = pd.DataFrame({s: 1e6 * (10 ** (i / 2)) for i, s in enumerate(syms)}, index=idx)  # A least liquid
    return close, vol


def test_registered_and_symbols():
    d = registry.get("amihud_quality")
    assert d.kind == "amihud_quality" and d.monthly
    assert "survivorship" in d.description.lower()


def test_top_n_equal_weight_and_illiquid_preferred():
    close, vol = _frames()
    tg = strategies.amihud_quality_targets(close, vol, n=2, q=0.0)
    assert len(tg) == 2 and all(abs(w - 0.5) < 1e-9 for w in tg.values())
    assert set(tg) == {"A", "B"}                      # lowest dollar volume = highest Amihud


def test_quality_gate_excludes_losers():
    close, vol = _frames()
    close["A"] = close["A"].iloc[0] * np.linspace(1, 0.5, len(close))   # A: negative 12m return, least liquid
    tg = strategies.amihud_quality_targets(close, vol, n=2, q=0.5)
    assert "A" not in tg


def test_insufficient_history_and_no_lookahead():
    close, vol = _frames()
    assert strategies.amihud_quality_targets(close.iloc[:100], vol.iloc[:100], n=2) == {}
    base = strategies.amihud_quality_targets(close, vol, n=3, q=0.0)
    c2 = close.copy()
    c2.iloc[-1] *= 5                                   # only the last row differs: result may change...
    again = strategies.amihud_quality_targets(close.copy(), vol, n=3, q=0.0)
    assert base == again
    assert strategies.compute("amihud_quality", {"close": close, "volume": vol}, 20) is not None


def test_first_rebalance_is_next_trading_day(tmp_path):
    import dataclasses
    from types import SimpleNamespace
    from app.config import load_settings
    from app.db import DB
    from app.market import set_test_time
    from app.shadows.manager import ShadowManager
    from tests.test_shadows import NoOrderBroker
    from tests.test_sleeve_manager import FakeEngine, at
    at("2026-12-01", "11:00")
    try:
        db = DB(tmp_path / "t.db", "mock", None, None)
        s = dataclasses.replace(load_settings(), shadows_enabled=True, shadow_start_capital=500000.0)
        m = ShadowManager(FakeEngine(s, db, NoOrderBroker()))
        m._ensure_states()
        assert not m._due_month("amihud_quality")          # created today -> not due today
        at("2026-12-02", "11:00")
        assert m._due_month("amihud_quality")
        db.upsert_shadow_state("amihud_quality", last_rebalance="2026-12-02T10:30:00")
        assert not m._due_month("amihud_quality")
        at("2026-12-31", "11:00")                          # 20 trading days later: not yet
        assert not m._due_month("amihud_quality")
        at("2027-01-04", "11:00")                          # 21+ trading days
        assert m._due_month("amihud_quality")
    finally:
        set_test_time(None)
