"""Shadow strategies: pure target functions, virtual fills, no broker orders, API."""
from __future__ import annotations

import dataclasses
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.config import load_settings
from app.db import DB
from app.market import set_test_time
from app.shadows import registry, strategies
from app.shadows.manager import ShadowManager
from app.sleeves import data
from tests.test_api_support import HDR, make_app, make_client
from tests.test_sleeve_manager import FakeBroker, FakeEngine, at


class NoOrderBroker(FakeBroker):
    """Read methods work; every order method raises."""

    async def place_order(self, *a, **k):
        raise AssertionError("shadow called place_order")

    async def place_option_single_order(self, *a, **k):
        raise AssertionError("shadow called place_option_single_order")

    async def place_bracket(self, *a, **k):
        raise AssertionError("shadow called place_bracket")

    async def place_stop(self, *a, **k):
        raise AssertionError("shadow called place_stop")

    async def cancel_order(self, *a, **k):
        raise AssertionError("shadow called cancel_order")

    async def preview_order(self, *a, **k):
        raise AssertionError("shadow called preview_order")


def frames(n=300, syms=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "KKK", "LLL")):
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = pd.DataFrame({s: 100 * np.exp(np.linspace(0, 0.1 * (i + 1), n)) for i, s in enumerate(syms)}, index=idx)
    opn = close.shift(1).fillna(close) * 1.001
    return {"open": opn, "close": close}


# -------------------------------------------------------------------- pure strategies
def test_mom_skip1w_uses_t5_over_t252():
    f = frames()["close"]
    tg = strategies.mom_skip1w_targets(f, n=3)
    score = f.iloc[-6] / f.iloc[-253] - 1
    assert set(tg) == set(score.sort_values(ascending=False).index[:3])
    assert all(abs(w - 1 / 3) < 1e-9 for w in tg.values())
    assert strategies.mom_skip1w_targets(f.iloc[:100], n=3) == {}


def test_overnight_score_uses_only_past_rows():
    f = frames()
    base = strategies.overnight_scores(f["open"], f["close"], asof=260)
    o2, c2 = f["open"].copy(), f["close"].copy()
    o2.iloc[261:] *= 7.0
    c2.iloc[261:] *= 0.3                         # future rows are rewritten: the score must not move
    same = strategies.overnight_scores(o2, c2, asof=260)
    pd.testing.assert_series_equal(base, same)
    manual = np.log(f["open"].iloc[9:261] / f["close"].shift(1).iloc[9:261]).sum()
    pd.testing.assert_series_equal(base, manual, check_names=False)


def test_overnight_targets_top_n():
    f = frames()
    f["open"].loc[:, "CCC"] = f["close"]["CCC"].shift(1).bfill() * 1.02     # big gap every night
    tg = strategies.overnight_mom_targets(f["open"], f["close"], n=2)
    assert "CCC" in tg and len(tg) == 2


def _etf(trend: dict[str, float], n=300):
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({s: 100 * np.exp(np.linspace(0, r, n)) for s, r in trend.items()}, index=idx)


def test_vaa_g4_formula():
    up = {"SPY": 0.3, "EFA": 0.1, "EEM": 0.2, "AGG": 0.05, "LQD": 0.02, "IEF": 0.03, "SHY": 0.01}
    assert strategies.vaa_g4_targets(_etf(up)) == {"SPY": 1.0}
    down = {**up, "EFA": -0.2}                    # one bad offensive fund -> B=1 -> full defense
    assert strategies.vaa_g4_targets(_etf(down)) == {"IEF": 1.0}
    assert strategies.vaa_g4_targets(_etf({"SPY": 0.1})) == {}


# -------------------------------------------------------------------- manager
@pytest.fixture
def env(tmp_path, monkeypatch):
    at("2026-12-01", "11:00")
    fr = frames()
    monkeypatch.setattr(data, "daily_ohlc", lambda syms, period="18mo": fr)
    db = DB(tmp_path / "t.db", "mock", None, None)
    broker = NoOrderBroker()
    broker.prices = {s: float(fr["close"][s].iloc[-1]) for s in fr["close"].columns}
    s = dataclasses.replace(load_settings(), shadows_enabled=True, shadow_start_capital=100000.0,
                            sleeve_rebalance_after_et="10:00")
    eng = FakeEngine(s, db, broker)
    # vaa_g4 needs the ETFs: add them to the same frames
    etf = _etf({"SPY": 0.3, "EFA": 0.1, "EEM": 0.2, "AGG": 0.05, "LQD": 0.02, "IEF": 0.03, "SHY": 0.01})
    etf.index = fr["close"].index
    for c in etf.columns:
        fr["close"][c] = etf[c]
        fr["open"][c] = etf[c]
        broker.prices[c] = float(etf[c].iloc[-1])
    yield SimpleNamespace(db=db, broker=broker, mgr=ShadowManager(eng), eng=eng)
    set_test_time(None)
    db.close()


async def test_rebalance_and_valuation_never_touch_order_methods(env):
    m, db = env.mgr, env.db
    await m.tick()
    assert m.last_error is None
    for d in [x for x in registry.SHADOWS if x.monthly]:
        st = db.shadow_state(d.id)
        assert st["last_rebalance"] and st["cash"] >= 0
        hold = db.shadow_holdings(d.id)
        assert hold and all(float(h["qty"]) == int(h["qty"]) for h in hold)
        trades = db.shadow_trades(d.id)
        assert trades and all(t["side"] == "BUY" for t in trades)
        for t in trades:                            # 10 bps over the mid, whole shares
            assert t["price"] == pytest.approx(env.broker.prices[t["symbol"]] * 1.001, rel=1e-4)
            assert t["qty"] == int(t["qty"])
        eq = [r for r in db.shadow_equity_series(5) if r.get(d.id)]
        assert eq and 99000 < eq[-1][d.id] <= 100000
    # idle valuation ticks and next-month rebalance
    m._last_tick = 0
    await m.tick()
    assert db.shadow_trades("vaa_g4") and len(db.shadow_trades("vaa_g4", 50)) == 1
    at("2027-01-04", "11:00")
    m._px.clear()
    await m.tick()
    assert m.last_error is None and env.broker.placed == []


async def test_sells_first_and_cash_never_negative(env):
    m, db = env.mgr, env.db
    db.upsert_shadow_state("mom_skip1w", cash=0.0, start_capital=100000.0, start_date="2026-12-01",
                           peak_equity=100000.0)
    db.set_shadow_holding("mom_skip1w", "ZZZ", 100, 100.0)
    env.broker.prices["ZZZ"] = 100.0
    await m.tick()
    assert db.shadow_state("mom_skip1w")["cash"] >= 0
    trades = db.shadow_trades("mom_skip1w", 100)[::-1]
    assert trades[0]["symbol"] == "ZZZ" and trades[0]["side"] == "SELL"
    assert trades[0]["price"] == pytest.approx(99.9)
    assert all(t["side"] == "BUY" for t in trades[1:]) and len(trades) > 1


async def test_waits_outside_core_session_but_values(env):
    at("2026-12-01", "09:45")
    await env.mgr.tick()
    assert env.db.shadow_state("vaa_g4")["last_rebalance"] is None
    assert env.db.shadow_trades("vaa_g4") == []
    assert any(r.get("vaa_g4") == 100000.0 for r in env.db.shadow_equity_series(5))


async def test_tick_never_raises(env):
    async def boom(*a, **k):
        raise RuntimeError("x")
    env.broker.quote = boom
    env.broker.last_price = boom
    await env.mgr.tick()          # no prices anywhere: logged, no exception


async def test_disabled_does_nothing(env):
    env.eng.s = env.mgr.s = dataclasses.replace(env.mgr.s, shadows_enabled=False)
    assert env.mgr.due() is False
    await env.mgr.tick()
    assert env.db.shadow_state("vaa_g4") is None


async def test_reset_wipes_only_that_shadow(env):
    m, db = env.mgr, env.db
    await m.tick()
    m.reset("vaa_g4")
    assert db.shadow_holdings("vaa_g4") == [] and db.shadow_trades("vaa_g4") == []
    st = db.shadow_state("vaa_g4")
    assert st["cash"] == 100000.0 and st["last_rebalance"] is None
    assert db.shadow_holdings("mom_skip1w")
    with pytest.raises(KeyError):
        m.reset("nope")


def test_namespace_isolation(tmp_path):
    a = DB(tmp_path / "t.db", "live", "prod", "A1")
    a.upsert_shadow_state("vaa_g4", cash=5.0)
    a.set_shadow_holding("vaa_g4", "SPY", 3, 10.0)
    a.record_shadow_equity("2026-12-01", "vaa_g4", 5.0)
    a.close()
    b = DB(tmp_path / "t.db", "live", "prod", "B2")
    assert b.shadow_state("vaa_g4") is None and b.shadow_holdings("vaa_g4") == []
    assert b.shadow_equity_series(3650) == []
    b.close()


async def test_summary_detail_shapes(env):
    m = env.mgr
    await m.tick()
    j = m.summary()
    assert j["enabled"] is True and [s["id"] for s in j["shadows"]] == registry.IDS
    s0 = j["shadows"][0]
    for k in ("id", "name", "description", "rule", "start_date", "start_capital", "equity", "cash",
              "total_return_pct", "drawdown_pct", "positions_n", "last_rebalance", "next_rebalance",
              "live", "expected"):
        assert k in s0
    assert s0["live"] == {"days": 0, "sharpe": None} and s0["expected"] is None
    assert s0["next_rebalance"] == "2027-01-04"
    assert -1.0 < s0["total_return_pct"] <= 0 and s0["drawdown_pct"] <= 0
    d = m.detail("vaa_g4")
    assert d["holdings"][0]["symbol"] == "SPY" and d["holdings"][0]["weight_pct"] > 90
    assert set(d["trades"][0]) == {"ts", "symbol", "side", "qty", "price", "reason"}
    assert math.isclose(sum(h["market_value"] for h in d["holdings"]) + d["cash"], d["equity"], abs_tol=1.0)


# -------------------------------------------------------------------- API
def _app(tmp_path, enabled=True):
    tmp_path.mkdir(exist_ok=True)
    app = make_app(tmp_path)
    s = dataclasses.replace(load_settings(), shadows_enabled=enabled)
    app.state.settings = s
    eng = SimpleNamespace(s=s, db=app.state.db, broker=NoOrderBroker(), portfolio=None, kill_switch=False)
    eng.shadows = ShadowManager(eng)
    app.state.engine = eng
    return app


async def test_api(tmp_path):
    app = _app(tmp_path)
    try:
        app.state.db.record_shadow_equity("2099-01-01", "vaa_g4", 1.0)   # outside the window
        from datetime import date
        app.state.db.record_shadow_equity(date.today().isoformat(), "vaa_g4", 123.0)
        async with make_client(app) as c:
            j = (await c.get("/api/shadows", headers=HDR)).json()
            assert j["enabled"] is True and len(j["shadows"]) == len(registry.IDS)
            r = await c.get("/api/shadows/equity?days=30", headers=HDR)
            assert r.status_code == 200 and r.json()["series"][0]["vaa_g4"] == 123.0
            assert (await c.get("/api/shadows/vaa_g4", headers=HDR)).json()["holdings"] == []
            assert (await c.get("/api/shadows/nope", headers=HDR)).status_code == 404
            assert (await c.post("/api/shadows/vaa_g4/reset", headers=HDR)).json() == {"ok": True}
            assert (await c.post("/api/shadows/nope/reset", headers=HDR)).status_code == 404
    finally:
        app.state.db.close()
