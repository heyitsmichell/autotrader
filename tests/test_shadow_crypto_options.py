"""crypto_trend shadow (daily target weights with a no-trade band) and one-leg option shadows."""
from __future__ import annotations

import dataclasses
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.config import load_settings
from app.db import DB
from app.market import set_test_time
from app.shadows import options, registry, strategies
from app.shadows.manager import ShadowManager
from app.sleeves import data
from tests.test_shadows import NoOrderBroker
from tests.test_sleeve_manager import FakeEngine, at


def _series(n=320, drift=0.002, start="2025-06-02", seed=0, vol=0.02):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.exp(np.cumsum(drift + vol * rng.standard_normal(n))), index=idx)


# -------------------------------------------------------------------- pure signal functions
def test_trend_signal_extremes_and_history():
    up = pd.Series(np.linspace(100, 200, 300))
    assert strategies.trend_signal(up) == 1.0
    assert strategies.trend_signal(up[::-1].reset_index(drop=True)) == 0.0
    assert strategies.trend_signal(up.iloc[:252]) is None          # needs 253 rows
    with pytest.raises(ValueError):
        strategies.trend_signal(up, "nope")


def test_trend_signal_matches_the_study():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import crypto_trend_study as study
    for seed in range(5):
        p = _series(seed=seed, drift=0.0)
        assert strategies.trend_signal(p) == pytest.approx(float(study.signal(p, "ens_all").iloc[-1]))


def test_crypto_targets_scale_by_volatility():
    btc = _series(seed=1, vol=0.05)
    eth = _series(seed=2, vol=0.01)
    tg = strategies.crypto_trend_targets(pd.DataFrame({"BTC-USD": btc, "ETH-USD": eth}))
    assert set(tg) == {"IBIT", "ETHA"}
    for sym, s in (("IBIT", btc), ("ETHA", eth)):
        vol = s.pct_change().dropna().iloc[-21:].std(ddof=1) * math.sqrt(252)
        assert tg[sym] == pytest.approx(0.5 * strategies.trend_signal(s) * min(1, 0.40 / vol), abs=1e-4)
    assert strategies.crypto_trend_targets(pd.DataFrame({"BTC-USD": btc})) == {}


def test_nyse_rows_drops_weekends_and_holidays():
    idx = pd.date_range("2026-11-25", "2026-11-30")          # Wed..Mon, Thanksgiving Thu
    df = pd.DataFrame({"x": range(len(idx))}, index=idx)
    kept = [d.date().isoformat() for d in strategies.nyse_rows(df).index]
    assert kept == ["2026-11-25", "2026-11-27", "2026-11-30"]


def test_option_spec_refuses_naked_calls():
    with pytest.raises(ValueError):
        registry.OptionSpec("IBIT", "CALL", "short")
    assert registry.get("ibit_put_trend").mode == "option" and registry.get("crypto_trend").daily


# -------------------------------------------------------------------- manager fixture
@pytest.fixture
def env(tmp_path, monkeypatch):
    at("2026-12-01", "11:00")
    db = DB(tmp_path / "t.db", "mock", None, None)
    broker = NoOrderBroker()
    s = dataclasses.replace(load_settings(), shadows_enabled=True, shadow_start_capital=100000.0,
                            sleeve_rebalance_after_et="10:00")
    eng = FakeEngine(s, db, broker)
    st = SimpleNamespace(close=None, calls=0)

    def fake_ohlc(syms, period="18mo"):
        st.calls += 1
        c = st.close[[x for x in syms if x in st.close.columns]]
        return {"open": c, "close": c}

    monkeypatch.setattr(data, "daily_ohlc", fake_ohlc)
    yield SimpleNamespace(db=db, broker=broker, mgr=ShadowManager(eng), st=st, mp=monkeypatch)
    set_test_time(None)
    db.close()


def _crypto(env, btc_drift=0.004, eth_drift=0.004, end="2026-12-01"):
    idx = pd.date_range(end=end, periods=500, freq="D")
    env.st.close = pd.DataFrame({"BTC-USD": _series(500, btc_drift, seed=3).values,
                                 "ETH-USD": _series(500, eth_drift, seed=4).values}, index=idx)


def only(env, *defs):
    env.mp.setattr(registry, "SHADOWS", tuple(defs))


# -------------------------------------------------------------------- crypto_trend
async def test_crypto_closes_drop_today_and_cache(env):
    _crypto(env)
    c = await env.mgr.crypto_closes(["BTC-USD"])
    assert c.index[-1].date().isoformat() == "2026-11-30"         # today's running UTC bar dropped
    assert all(d.weekday() < 5 for d in c.index)
    await env.mgr.crypto_closes(["BTC-USD"])
    assert env.st.calls == 1


async def test_crypto_trend_buys_then_band_holds_then_exits(env):
    only(env, registry.CRYPTO_TREND)
    _crypto(env)
    env.broker.prices = {"IBIT": 50.0, "ETHA": 20.0}
    m, db = env.mgr, env.db
    await m.tick()
    assert m.last_error is None
    tg = (await m._targets(registry.CRYPTO_TREND))
    held = {h["symbol"]: float(h["qty"]) for h in db.shadow_holdings("crypto_trend")}
    assert set(held) == {"IBIT", "ETHA"}
    for sym, px in env.broker.prices.items():
        assert held[sym] == math.floor(100000 * tg[sym] / (px * 1.001))
    n0 = len(db.shadow_trades("crypto_trend", 100))
    m._last_tick = 0
    await m.tick()                                               # same day: not due again
    assert len(db.shadow_trades("crypto_trend", 100)) == n0
    at("2026-12-02", "11:00")                                    # next day, prices drift 3%: inside the band
    env.broker.prices = {"IBIT": 51.5, "ETHA": 20.6}
    _crypto(env, end="2026-12-02")
    m._last_tick = 0
    m._px.clear()
    await m.tick()
    assert len(db.shadow_trades("crypto_trend", 100)) == n0
    assert db.shadow_state("crypto_trend")["last_rebalance"].startswith("2026-12-02")
    at("2026-12-03", "11:00")                                    # trend breaks: weights go to 0, sell everything
    _crypto(env, btc_drift=-0.006, eth_drift=-0.006, end="2026-12-03")
    m._last_tick = 0
    await m.tick()
    assert db.shadow_holdings("crypto_trend") == []
    assert db.shadow_state("crypto_trend")["cash"] > 99000
    assert env.broker.placed == [] and env.broker.opt_placed == []


# -------------------------------------------------------------------- option shadows
SPOT = 50.0
EXPS = {"261218": 17, "261231": 30, "270115": 45}               # DTE from 2026-12-01


def _chain(env, spot=SPOT):
    toks, snaps = [], {}
    for e, dte in EXPS.items():
        for k in range(38, 56):
            sym = f"IBIT{e}P{k * 1000:08d}"
            toks.append(sym)
            z = (math.log(spot / k)) / (0.5 * math.sqrt(dte / 365))
            delta = -(1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))   # crude put delta
            mid = max(k - spot, 0.0) + 2.0 * math.exp(-abs(k - spot) / 4)
            snaps[sym] = {"bid": round(mid - 0.05, 2), "ask": round(mid + 0.05, 2), "delta": delta}
    toks.append("IBIT261231P00045500")                             # half strike is standard
    toks.append("IBIT261231P00045330")                             # adjusted contract: skipped
    env.broker.contracts_text = "\n".join(toks)
    env.broker.snaps = snaps
    env.broker.prices["IBIT"] = spot


def _gate(env, value):
    async def g(gate):
        return 1.0 if gate is None else value
    env.mgr.gate_value = g


def test_candidates_filter():
    spec = registry.IBIT_PUT_TREND.option
    text = "IBIT261231P00045000 IBIT261231C00045000 IBIT261218P00045000 QQQ261231P00500000 IBIT261231P00045330"
    rows = options.candidates(text, spec, pd.Timestamp("2026-12-01").date())
    assert [r["symbol"] for r in rows] == ["IBIT261231P00045000"]


async def test_option_shadow_opens_cash_secured_put_without_orders(env):
    only(env, registry.IBIT_PUT_TREND)
    _chain(env)
    _gate(env, 0.75)
    m, db = env.mgr, env.db
    await m.tick()
    assert m.last_error is None
    [h] = db.shadow_holdings("ibit_put_trend")
    assert h["symbol"].startswith("IBIT261231P")                   # nearest 30 DTE
    occ = options.parse_occ(h["symbol"])
    deltas = {s: abs(v["delta"]) for s, v in env.broker.snaps.items() if s.startswith("IBIT261231P")}
    assert abs(deltas[h["symbol"]] - 0.30) == pytest.approx(min(abs(x - 0.30) for x in deltas.values()))
    n = math.floor(100000 / (occ["strike"] * 100))
    assert float(h["qty"]) == -n
    q = env.broker.snaps[h["symbol"]]
    px = options.sell_limit(q["bid"], q["ask"])
    assert db.shadow_state("ibit_put_trend")["cash"] == pytest.approx(100000 + px * 100 * n - 0.65 * n, abs=0.01)
    [t] = db.shadow_trades("ibit_put_trend")
    assert t["side"] == "SELL" and t["price"] == px
    eq = db.shadow_equity_series(5)[-1]["ibit_put_trend"]
    mid = (q["bid"] + q["ask"]) / 2
    assert eq == pytest.approx(100000 + px * 100 * n - 0.65 * n - n * 100 * mid, abs=0.02)
    det = m.detail("ibit_put_trend")
    assert det["holdings"][0]["market_value"] == pytest.approx(-n * 100 * mid, abs=0.01)
    assert env.broker.placed == [] and env.broker.opt_placed == []


async def test_gate_off_blocks_open_and_closes_leg(env):
    only(env, registry.IBIT_PUT_TREND, registry.IBIT_PUT_ALWAYS)
    _chain(env)
    _gate(env, 0.25)
    m, db = env.mgr, env.db
    await m.tick()
    assert db.shadow_holdings("ibit_put_trend") == []              # gated: no open
    assert len(db.shadow_holdings("ibit_put_always")) == 1         # control: always opens
    _gate(env, 0.75)
    m._last_tick = 0
    await m.tick()
    [h] = db.shadow_holdings("ibit_put_trend")
    cash_open = db.shadow_state("ibit_put_trend")["cash"]
    _gate(env, 0.375)
    m._last_tick = 0
    await m.tick()
    assert db.shadow_holdings("ibit_put_trend") == []
    q = env.broker.snaps[h["symbol"]]
    px = options.buy_limit(q["bid"], q["ask"])
    n = -float(h["qty"])
    assert db.shadow_state("ibit_put_trend")["cash"] == pytest.approx(cash_open - px * 100 * n - 0.65 * n, abs=0.01)
    assert db.shadow_trades("ibit_put_trend")[0]["side"] == "BUY"
    assert len(db.shadow_holdings("ibit_put_always")) == 1         # no gate: never closed early


async def test_expiry_settles_at_intrinsic_and_waits_a_day(env):
    only(env, registry.IBIT_PUT_ALWAYS)
    _chain(env)
    m, db = env.mgr, env.db
    await m.tick()
    [h] = db.shadow_holdings("ibit_put_always")
    occ = options.parse_occ(h["symbol"])
    n = -float(h["qty"])
    cash = db.shadow_state("ibit_put_always")["cash"]
    at(occ["expiration"], "15:50")
    env.broker.prices["IBIT"] = occ["strike"] - 2.0                  # 2 dollars in the money
    m._last_tick = 0
    await m.tick()
    assert db.shadow_holdings("ibit_put_always") == []
    assert db.shadow_state("ibit_put_always")["cash"] == pytest.approx(cash - n * 100 * 2.0, abs=0.01)
    t = db.shadow_trades("ibit_put_always")[0]
    assert t["side"] == "BUY" and t["price"] == pytest.approx(2.0) and "expired" in t["reason"]
    from app import results
    trips, _ = results.shadow_trips(db)
    [trip] = [x for x in trips if x["strategy"] == "ibit_put_always"]
    assert trip["kind"] == "option" and trip["direction"] == "short" and trip["bucket"] == "expiry"
    sold = db.shadow_trades("ibit_put_always")[1]["price"]
    assert trip["pnl"] == pytest.approx((sold - 2.0) * n * 100, abs=0.01)
    at(occ["expiration"], "15:52")
    m._last_tick = 0
    await m.tick()
    assert db.shadow_holdings("ibit_put_always") == []             # no new leg the day a cycle ended
    assert env.broker.opt_placed == []


async def test_no_chain_or_quotes_warns_and_never_raises(env):
    only(env, registry.IBIT_PUT_ALWAYS)
    env.broker.prices["IBIT"] = SPOT
    m, db = env.mgr, env.db
    await m.tick()                                                  # empty chain text
    assert m.last_error is None and db.shadow_holdings("ibit_put_always") == []

    async def boom(*a, **k):
        raise RuntimeError("down")
    env.broker.option_contracts = boom
    m._last_tick = 0
    m._price_warned.clear()
    await m.tick()
    assert m.last_error is None
    assert any("option chain unavailable" in (r.get("message") or "") for r in db.events(20))


async def test_gate_value_from_yahoo(env):
    _crypto(env)
    assert await env.mgr.gate_value(None) == 1.0
    g = await env.mgr.gate_value(("BTC-USD", "ens_all"))
    c = await env.mgr.crypto_closes(["BTC-USD"])
    assert g == strategies.trend_signal(c["BTC-USD"])
    env.st.close = pd.DataFrame()
    env.mgr.__dict__.pop("_daily_closes", None)
    assert await env.mgr.gate_value(("BTC-USD", "ens_all")) is None
