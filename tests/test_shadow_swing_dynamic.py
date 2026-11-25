"""swing_dynamic shadow: daily forward replay, no network, no broker."""
from __future__ import annotations

import dataclasses
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.config import load_settings
from app.db import DB
from app.market import set_test_time
from app.shadows import manager as mgr_mod
from app.shadows import swing_dynamic as sd
from app.shadows.manager import ShadowManager
from app.sleeves import data
from tests.test_sleeve_manager import FakeEngine, at

SYMS = [f"S{i:02d}" for i in range(14)] + ["SPY"]
TODAY = "2026-12-01"          # Tuesday
START = "2026-11-02"


class DeadBroker:
    """Every attribute access that is called raises."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(f"shadow touched broker.{name}")
        return boom


def panel(n=400, end=TODAY):
    idx = pd.bdate_range(end=end, periods=n)
    rng = np.random.default_rng(3)
    close, vol = {}, {}
    for i, s in enumerate(SYMS):
        r = rng.normal(0.0008, 0.015, n)
        close[s] = 50 * (i + 1) * np.exp(np.cumsum(r))
        vol[s] = np.full(n, 1e6 * (i + 1))
    c = pd.DataFrame(close, index=idx)
    o = c.shift(1).fillna(c) * 1.002
    return {"open": o, "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99, "close": c,
            "volume": pd.DataFrame(vol, index=idx)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    at(TODAY, "17:00")
    fr = panel()
    calls = []

    def fake(syms, period="2y"):
        calls.append(period)
        return {k: v.copy() for k, v in fr.items()}
    monkeypatch.setattr(data, "daily_ohlcv", fake)
    monkeypatch.setattr(sd, "pool_symbols", lambda s: [x for x in SYMS if x != "SPY"])
    monkeypatch.setattr(sd, "TOP_N", 5)
    db = DB(tmp_path / "t.db", "mock", None, None)
    s = dataclasses.replace(load_settings(), shadows_enabled=True, shadow_start_capital=100000.0)
    eng = FakeEngine(s, db, DeadBroker())
    m = ShadowManager(eng)
    m._ensure_states()
    db.upsert_shadow_state("swing_dynamic", start_date=START)
    yield SimpleNamespace(db=db, mgr=m, calls=calls, fr=fr, mp=monkeypatch)
    set_test_time(None)
    db.close()


def rows(db):
    return ([(r["day"], r.get("swing_dynamic")) for r in db.shadow_equity_series(3650) if r.get("swing_dynamic")],
            [(h["symbol"], h["qty"], h["avg_cost"]) for h in db.shadow_holdings("swing_dynamic")],
            [(t["symbol"], t["side"], t["qty"], t["price"], t["reason"]) for t in db.shadow_trades("swing_dynamic", 999)])


async def test_replay_writes_rows_from_start_only_and_is_idempotent(env):
    await env.mgr.tick()
    assert env.mgr.last_error is None
    eq, hold, tr = rows(env.db)
    assert eq and eq[0][0] == START and eq[-1][0] == TODAY
    assert eq[0][1] == pytest.approx(100000.0, rel=0.03)   # nothing held before the start day
    first_buy = min((t for t in env.db.shadow_trades("swing_dynamic", 999) if t["side"] == "BUY"),
                    key=lambda t: t["ts"], default=None)
    assert first_buy is None or first_buy["ts"][:10] >= START
    st = env.db.shadow_state("swing_dynamic")
    assert st["peak_equity"] >= 100000.0
    # rerun the same day: force by clearing the day marker
    env.db.upsert_shadow_state("swing_dynamic", meta={})
    env.mgr._last_tick = 0
    await env.mgr.tick()
    assert rows(env.db) == (eq, hold, tr)


def test_no_entries_before_start_and_topn_uses_previous_day(env):
    fr = env.fr
    close, vol = fr["close"][SYMS[:-1]], fr["volume"][SYMS[:-1]]
    hook = sd.make_entry_hook(close, vol, date.fromisoformat(START), top_n=5)
    days = [d.date() for d in close.index]
    assert not any(hook(d, s) for d in days if d < date.fromisoformat(START) for s in SYMS)
    d = date.fromisoformat(TODAY)
    top = {s for s in SYMS[:-1] if hook(d, s)}
    assert top == {"S09", "S10", "S11", "S12", "S13"}
    # rewrite volume on the evaluated day and later: the ranking must not move
    v2 = vol.copy()
    v2.loc[v2.index >= pd.Timestamp(d)] = v2.loc[v2.index >= pd.Timestamp(d)].iloc[:, ::-1].values
    hook2 = sd.make_entry_hook(close, v2, date.fromisoformat(START), top_n=5)
    assert {s for s in SYMS[:-1] if hook2(d, s)} == top
    # but yesterday's volumes do count
    v3 = vol.copy()
    v3.loc[v3.index < pd.Timestamp(d)] = v3.loc[v3.index < pd.Timestamp(d)].iloc[:, ::-1].values
    hook3 = sd.make_entry_hook(close, v3, date.fromisoformat(START), top_n=5)
    prev = close.index[close.index < pd.Timestamp(d)][-1]
    exp = ((close * v3).rolling(20).mean().loc[prev]).sort_values(ascending=False).index[:5]
    assert {s for s in SYMS[:-1] if hook3(d, s)} == set(exp) != top


async def test_trades_not_before_start_and_broker_untouched(env):
    await env.mgr.tick()
    assert env.mgr.last_error is None
    for t in env.db.shadow_trades("swing_dynamic", 999):
        assert t["ts"][:10] >= START
    assert env.db.shadow_state("swing_dynamic")["meta_json"]


async def test_skipped_before_1630_and_runs_once_per_day(env):
    at(TODAY, "16:29")
    await env.mgr.tick()
    assert env.calls == [] and rows(env.db)[0] == []
    at(TODAY, "16:31")
    env.mgr._last_tick = 0
    await env.mgr.tick()
    assert len(env.calls) == 1
    env.mgr._last_tick = 0
    await env.mgr.tick()
    assert len(env.calls) == 1
    at("2026-12-05", "17:00")          # Saturday: not a trading day
    env.mgr._last_tick = 0
    await env.mgr.tick()
    assert len(env.calls) == 1


async def test_download_failure_keeps_rows_and_warns_once(env):
    await env.mgr.tick()
    before = rows(env.db)
    env.db.upsert_shadow_state("swing_dynamic", meta={})
    warns = []
    orig = env.db.log
    env.mp.setattr(env.db, "log", lambda lvl, msg, *a, **k: (warns.append(msg) if lvl == "warn" else None,
                                                             orig(lvl, msg, *a, **k))[1])

    def boom(*a, **k):
        raise RuntimeError("yahoo down")
    env.mp.setattr(data, "daily_ohlcv", boom)
    for _ in range(3):
        env.mgr._last_tick = 0
        env.mgr._replay_retry = 0.0
        await env.mgr.tick()
    assert rows(env.db) == before
    assert len([w for w in warns if "replay failed" in w]) == 1


def test_registry_and_views(env):
    from app.shadows import registry
    d = registry.get("swing_dynamic")
    assert not d.monthly and d.symbols() == []
    v = env.mgr.summary()["shadows"][-1]
    assert v["id"] == "swing_dynamic" and v["equity"] == 100000.0
    assert env.mgr.detail("swing_dynamic")["holdings"] == []
