"""Risk report: pricing, scenarios, historical simulation and the two GET endpoints (no network)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.api import risk as risk_api
from app.risk_report import pricing, report, scenarios
from tests.test_api_support import HDR, make_app, make_client
from tests.test_sleeve_manager import FakeBroker


def build(tmp_path):
    import dataclasses
    from types import SimpleNamespace

    from app.config import load_settings
    from app.sleeves.manager import SleeveManager
    tmp_path.mkdir(exist_ok=True)
    app = make_app(tmp_path)
    s = dataclasses.replace(load_settings(), sleeves_enabled=True)
    app.state.settings = s
    eng = SimpleNamespace(s=s, db=app.state.db, broker=FakeBroker(), portfolio=None, kill_switch=False,
                          scheduler_stopped_by_user=False)
    eng.sleeves = SleeveManager(eng)
    app.state.engine = eng
    return app


# ---------------------------------------------------------------- pricing
def test_put_call_parity():
    S, K, T, sig, r = 100.0, 95.0, 0.5, 0.3, pricing.RATE
    c = pricing.bs_price(S, K, T, sig, "C")
    p = pricing.bs_price(S, K, T, sig, "P")
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-9)


def test_known_bs_value():
    # S=100 K=100 T=1 sigma=0.2 r=0.05 call = 10.4506
    assert pricing.bs_price(100, 100, 1.0, 0.2, "C", r=0.05) == pytest.approx(10.4506, abs=1e-3)


def test_greeks_signs():
    c = pricing.greeks(100, 100, 0.25, 0.25, "C")
    p = pricing.greeks(100, 100, 0.25, 0.25, "P")
    assert 0 < c["delta"] < 1 and -1 < p["delta"] < 0
    assert c["gamma"] > 0 and c["vega"] > 0 and c["theta"] < 0 and p["theta"] < 0


# ---------------------------------------------------------------- VaR / ES
def test_var_es_known_distribution():
    pnl = np.arange(-100.0, 100.0)          # 200 evenly spaced outcomes
    v95, e95 = report.var_es(pnl, 0.95)
    assert v95 == pytest.approx(90.05, abs=0.1)
    assert e95 == pytest.approx(95.0, abs=1.0)
    v99, e99 = report.var_es(pnl, 0.99)
    assert e99 >= v99 > v95


# ---------------------------------------------------------------- report
def _closes(n=253, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-11-19", periods=n)
    spy = rng.normal(0.0004, 0.01, n)
    a = spy * 1.2 + rng.normal(0, 0.008, n)
    b = rng.normal(0, 0.01, n)
    px = lambda r, s0: s0 * np.cumprod(1 + r)  # noqa: E731
    return pd.DataFrame({"SPY": px(spy, 400), "AAA": px(a, 100), "BBB": px(b, 50), "QQQ": px(spy * 1.1, 380)}, index=idx)


def _holdings():
    return [
        {"symbol": "AAA", "kind": "stock", "sleeve": "momentum", "qty": 100, "spot": 100.0},
        {"symbol": "AAA", "kind": "stock", "sleeve": "swing", "qty": 50, "spot": 100.0},
        {"symbol": "BBB", "kind": "stock", "sleeve": "vaa", "qty": 200, "spot": 50.0},
        {"symbol": "QQQ261218P00370000", "kind": "option", "sleeve": "putwrite", "qty": -2, "underlying": "QQQ",
         "right": "P", "strike": 370.0, "expiry": "2026-12-18", "dte": 28, "spot": 380.0, "mark": 8.0,
         "iv": 0.22, "iv_source": "snapshot"},
    ]


SLEEVES = {"swing": {"name": "Swing", "equity": 20000}, "momentum": {"name": "Momentum", "equity": 20000},
           "putwrite": {"name": "Put-write", "equity": 20000}, "vaa": {"name": "VAA", "equity": 20000}}


def test_report_contributions_and_shape():
    w: list = []
    hs = report.prepare(_holdings(), _closes(), w)
    body = report.build_report(hs, _closes(), SLEEVES, 80000.0, [], w)
    assert sum(s["contribution_pct"] for s in body["sleeves"]) == pytest.approx(100, abs=0.5)
    assert body["account"]["var95"] > 0 and body["account"]["es95"] >= body["account"]["var95"]
    assert body["account"]["var99"] >= body["account"]["var95"]
    assert body["correlation"]["live"] is None and body["correlation"]["live_days"] == 0
    assert {"AAA"} == {x["symbol"] for x in body["concentration"]["shared_symbols"]}
    assert [s["name"] for s in body["stress"]][:3] == ["Market -5%", "Market -10%", "Market -20%"]
    assert len(body["stress"]) == 8 and body["stress"][1]["pnl"] < 0
    assert np.isclose(sum(x["contribution_pct"] for x in body["holdings"]), 100, atol=0.5)


def test_missing_iv_uses_realized_and_warns():
    h = _holdings()
    h[3]["iv"], h[3]["iv_source"] = None, None
    h[3]["mark"] = None
    w: list = []
    out = report.prepare(h, _closes(), w)
    assert out[3]["iv_source"] == "realized_est" and out[3]["iv"] > 0
    assert any("IV missing" in x for x in w)


def test_missing_history_degrades():
    w: list = []
    body = report.build_report(report.prepare(_holdings(), pd.DataFrame(), w), pd.DataFrame(), SLEEVES, 80000.0, [], w)
    assert body["account"]["var95"] == 0 and body["warnings"]


# ---------------------------------------------------------------- scenarios
POS = {"spot": 400.0, "strike": 380.0, "right": "P", "dte": 21, "iv": 0.2, "qty": -3, "mark": 5.0}


def test_grid_shape():
    g = scenarios.position_grid(POS)
    assert (len(g), len(g[0]), len(g[0][0])) == (3, 4, 13)
    t = scenarios.total_grid([g, g])
    assert t[0][1][8] == pytest.approx(0.0, abs=0.01)      # today, no IV shift, no move
    assert t[1][1][8] > 0                                   # short option decays in 7 days


def test_expiry_horizon_is_intrinsic():
    g = scenarios.position_grid(POS)
    base = pricing.bs_price(400, 380, 21 / 365, 0.2, "P")
    for ci, sp in enumerate(scenarios.SPOT_PCT):
        S = 400 * (1 + sp / 100)
        expect = -3 * 100 * (max(380 - S, 0) - base)
        assert g[2][1][ci] == pytest.approx(expect, abs=0.01)
        assert g[2][0][ci] == g[2][3][ci]                   # IV is irrelevant at expiry


def test_assignment_cash_and_max_loss():
    st = scenarios.position_stats(POS, 500000.0)
    assert st["assignment_cash"] == 380 * 100 * 3 and st["sleeve_cash"] == 500000.0
    assert st["max_loss"] == pytest.approx((380 - 5.0) * 300) and st["breakeven"] == 375.0
    long_call = {**POS, "right": "C", "qty": 2}
    s2 = scenarios.position_stats(long_call, None)
    assert s2["assignment_cash"] is None and s2["max_loss"] == 1000.0


# ---------------------------------------------------------------- endpoints
@pytest.fixture
def app(tmp_path, monkeypatch):
    a = build(tmp_path)
    eng = a.state.engine
    eng.broker.prices.update({"AAA": 100.0, "QQQ": 380.0})
    eng.db.set_sleeve_holding("momentum", "AAA", "stock", 100, 100.0)
    eng.db.set_sleeve_holding("putwrite", "QQQ261218P00370000", "option", -2, 6.0)
    eng.broker.options_text = "Symbol: QQQ261218P00370000 Qty: -2 Mark: 8.0 Type: OPTION"
    cl = _closes()
    monkeypatch.setattr("app.sleeves.data.daily_closes", lambda syms, period="18mo": cl)
    yield a
    a.state.db.close()


async def test_risk_endpoint(app):
    async with make_client(app) as c:
        r = await c.get("/api/risk", headers=HDR)
        assert r.status_code == 200
        j = r.json()
        assert j["cached"] is False and j["account"]["var95"] is not None
        assert {s["id"] for s in j["sleeves"]} == {"swing", "momentum", "putwrite", "vaa"}
        assert {h["symbol"] for h in j["holdings"]} == {"AAA", "QQQ261218P00370000"}
        r2 = await c.get("/api/risk", headers=HDR)
        assert r2.json()["cached"] is True
        r3 = await c.get("/api/risk?refresh=1", headers=HDR)       # inside the 60 s gap: still cached
        assert r3.json()["cached"] is True


async def test_risk_options_endpoint(app):
    app.state.engine.broker.snaps["QQQ261218P00370000"] = {"iv": 0.21, "bid": 7.9, "ask": 8.1}
    async with make_client(app) as c:
        j = (await c.get("/api/risk/options", headers=HDR)).json()
    assert j["rate"] == 0.04 and len(j["positions"]) == 1
    p = j["positions"][0]
    assert p["iv_source"] == "implied_from_mark" and p["assignment_cash"] == 370 * 100 * 2 and p["right"] == "P"
    g = j["grid"]
    assert len(g["total"]) == 3 and len(g["total"][0]) == 4 and len(g["total"][0][0]) == 13
    assert "QQQ261218P00370000" in g["pnl"]


async def test_risk_degrades_without_history(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")
    monkeypatch.setattr("app.sleeves.data.daily_closes", boom)
    async with make_client(app) as c:
        r = await c.get("/api/risk", headers=HDR)
    assert r.status_code == 200 and r.json()["warnings"]
    assert risk_api.CACHE_TTL == 900


# ---------------------------------------------------------------- review fixes
def test_short_put_dollar_greek_signs():
    st = scenarios.position_stats(POS, None)
    assert st["delta_dollars"] > 0 and st["theta_dollars"] > 0
    assert st["gamma_dollars_1pct"] < 0 and st["vega_dollars"] < 0


def test_implied_vol_round_trip():
    for right in ("P", "C"):
        px = pricing.bs_price(400, 380, 30 / 365, 0.37, right)
        assert pricing.implied_vol(px, 400, 380, 30 / 365, right) == pytest.approx(0.37, abs=1e-6)
    assert pricing.implied_vol(0.01, 400, 300, 30 / 365, "C") is None      # below intrinsic


def test_prepare_uses_implied_from_mark_and_grid_is_zero():
    w: list = []
    h = report.prepare(_holdings(), _closes(), w)[3]
    assert h["iv_source"] == "implied_from_mark"
    g = scenarios.position_grid(h)
    assert g[0][1][scenarios.SPOT_PCT.index(0.0)] == 0.0


def test_swing_excludes_sleeve_shares_and_options():
    import asyncio
    from types import SimpleNamespace

    from app.risk_report import inputs

    class B:
        async def positions_text(self):
            return ("Symbol: AAA Qty: 150 Last: 100 Type: STOCK\n"
                    "Symbol: QQQ261218P00370000 Qty: -3 Mark: 8.0 Type: OPTION")

        async def parsed_option_snapshots(self, syms):
            return {}

        async def last_price(self, s):
            return 380.0

    class DB:
        def sleeve_holdings(self, sid):
            return {"momentum": [{"symbol": "AAA", "kind": "stock", "qty": 100, "avg_cost": 90}],
                    "putwrite": [{"symbol": "QQQ261218P00370000", "kind": "option", "qty": -2, "avg_cost": 6}]
                    }.get(sid, [])

        def sleeve_equity_series(self, n):
            return []

    mgr = SimpleNamespace(last_values={"total": {"equity": 1000}}, db=DB())
    eng = SimpleNamespace(broker=B(), sleeves=mgr, portfolio=None)
    out = asyncio.run(inputs.gather(eng))
    sw = [h for h in out["holdings"] if h["sleeve"] == "swing"]
    assert [(h["symbol"], h["qty"]) for h in sw if h["kind"] == "stock"] == [("AAA", 50.0)]
    assert [(h["symbol"], h["qty"]) for h in sw if h["kind"] == "option"] == [("QQQ261218P00370000", -1.0)]
    assert all(h["spot"] == 380.0 for h in out["holdings"] if h["kind"] == "option")


def test_negative_zero_and_nonpositive_equity():
    assert str(report._r(-0.0001, 2)) == "0.0"
    body = report.build_report(report.prepare(_holdings(), _closes(), []), _closes(),
                               {**SLEEVES, "vaa": {"name": "VAA", "equity": 0}}, 80000.0, [], [])
    vaa = next(s for s in body["sleeves"] if s["id"] == "vaa")
    assert vaa["var95_pct"] is None and vaa["vol_ann_pct"] is None and vaa["beta_spy"] is None


def test_option_weight_uses_delta_dollars():
    w: list = []
    body = report.build_report(report.prepare(_holdings(), _closes(), w), _closes(), SLEEVES, 80000.0, [], w)
    o = next(h for h in body["holdings"] if h["kind"] == "option")
    hs = report.prepare(_holdings(), _closes(), [])
    expect = report._delta_dollar(hs[3]) / 80000.0 * 100
    assert o["weight_pct"] == pytest.approx(expect, abs=0.01)


def test_correlation_matrix_null_when_history_short():
    body = report.build_report(report.prepare(_holdings(), pd.DataFrame(), []), pd.DataFrame(), SLEEVES, 80000.0, [], [])
    ids = body["correlation"]["ids"]
    assert body["correlation"]["holdings"] == [[1 if i == j else None for j in ids] for i in ids]


async def test_error_skeletons(app, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr("app.risk_report.inputs.gather", boom)
    async with make_client(app) as c:
        j = (await c.get("/api/risk", headers=HDR)).json()
        o = (await c.get("/api/risk/options", headers=HDR)).json()
    assert j["error"] is True and j["account"]["var95"] is None and len(j["account"]) == 11
    assert j["sleeves"] == [] and j["drawdowns"] == {}
    assert j["correlation"] == {"ids": [], "holdings": [], "live": None, "live_days": 0}
    assert j["concentration"] == {"top5_pct": None, "shared_symbols": []}
    assert o["error"] is True and o["positions"] == [] and o["grid"]["pnl"] == {} and o["rate"] == 0.04
    t = o["grid"]["total"]
    assert (len(t), len(t[0]), len(t[0][0])) == (3, 4, 13) and o["grid"]["horizons"]


async def test_refresh_throttle(app, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(risk_api.time, "time", lambda: now[0])
    async with make_client(app) as c:
        assert (await c.get("/api/risk", headers=HDR)).json()["cached"] is False
        now[0] += 30
        assert (await c.get("/api/risk?refresh=1", headers=HDR)).json()["cached"] is True
        now[0] += 31
        assert (await c.get("/api/risk?refresh=1", headers=HDR)).json()["cached"] is False


async def test_same_contract_in_two_sleeves_sums(app):
    sym = "QQQ261218P00370000"
    app.state.engine.db.set_sleeve_holding("vaa", sym, "option", -1, 6.0)
    app.state.engine.broker.options_text = f"Symbol: {sym} Qty: -3 Mark: 8.0 Type: OPTION"
    async with make_client(app) as c:
        j = (await c.get("/api/risk/options", headers=HDR)).json()
    assert len(j["positions"]) == 2
    g = j["grid"]
    assert g["pnl"][sym][1][1][0] == pytest.approx(g["total"][1][1][0], abs=0.01)
    one = scenarios.position_grid({**j["positions"][0], "iv": j["positions"][0]["iv"], "spot": j["positions"][0]["spot"]})
    assert abs(g["total"][1][1][0]) > abs(one[1][1][0]) * 1.2


@pytest.mark.parametrize("S", [60.0, 70.0, 80.0])
def test_deep_itm_put_at_intrinsic_not_implied(S):
    h = {"symbol": "ZZZ261218P00100000", "kind": "option", "sleeve": "putwrite", "qty": -1, "underlying": "ZZZ",
         "right": "P", "strike": 100.0, "expiry": "2026-12-18", "dte": 30, "spot": S, "mark": 100.0 - S,
         "iv": 0.4, "iv_source": "snapshot"}
    out = report.prepare([h], _closes(), [])[0]
    assert out["iv_source"] != "implied_from_mark"
    assert pricing.greeks(S, 100.0, 30 / 365, out["iv"], "P")["delta"] < -0.95 if S <= 70 else True
    assert pricing.greeks(S, 100.0, 30 / 365, out["iv"], "P")["delta"] < -0.85


def test_nonpositive_equity_pcts_null():
    w: list = []
    body = report.build_report(report.prepare(_holdings(), _closes(), w), _closes(), SLEEVES, -5.0, [], w)
    assert body["concentration"]["top5_pct"] is None and all(s["pnl_pct"] is None for s in body["stress"])


async def test_timeout_and_error_cache(app, monkeypatch):
    calls = []

    async def slow(*a, **k):
        calls.append(1)
        await __import__("asyncio").sleep(5)
    monkeypatch.setattr("app.risk_report.inputs.gather", slow)
    monkeypatch.setattr(risk_api, "COMPUTE_TIMEOUT", 0.05)
    async with make_client(app) as c:
        j = (await c.get("/api/risk", headers=HDR)).json()
        j2 = (await c.get("/api/risk", headers=HDR)).json()
    assert j["error"] is True and "unavailable" in j["warnings"][0] and j2["cached"] is True and len(calls) == 1
