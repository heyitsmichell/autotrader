"""Read-only per-strategy research views (app/research_views.py, app/api/research_views.py)."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app import research_views as rv
from app.config import load_settings
from app.market import now_et
from tests.test_api_support import HDR, make_app, make_client

ENDPOINTS = ["momentum", "vaa", "putwrite", "overlap", "regime"]


def _frame(rates: dict[str, float], rows: int = 300) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-11-24", periods=rows)
    return pd.DataFrame({s: 100.0 * np.power(1.0 + g, np.arange(rows)) for s, g in rates.items()}, index=idx)


class Boom(Exception):
    pass


class FakeBroker:
    """Read-only fake: any order/cancel call fails the test."""

    def __init__(self, prices=None, snaps=None, positions=""):
        self.prices, self.snaps, self.positions = prices or {}, snaps or {}, positions

    async def last_price(self, sym):
        return self.prices.get(sym)

    async def parsed_option_snapshots(self, syms):
        return {s: self.snaps[s] for s in syms if s in self.snaps}

    async def positions_text(self, fresh=False):
        return self.positions or "No data"

    async def place_order(self, *a, **k):
        raise Boom("order placed")

    async def place_option_single_order(self, *a, **k):
        raise Boom("option order placed")

    async def cancel_order(self, *a, **k):
        raise Boom("cancel")


class FakePortfolio:
    def __init__(self, positions=None, net_liq=100000.0):
        self.positions, self.net_liq = positions or [], net_liq

    async def snapshot(self, max_age=5.0):
        return {"balance": {"net_liq": self.net_liq}, "positions": self.positions}


@pytest.fixture(autouse=True)
def _fresh_cache():
    rv.clear_cache()
    yield
    rv.clear_cache()


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path)
    a.state.settings = dataclasses.replace(load_settings(), sleeve_momentum_n=10)
    a.state.engine = SimpleNamespace(db=a.state.db, broker=FakeBroker(), portfolio=FakePortfolio(),
                                     kill_switch=False)
    yield a
    a.state.db.close()


async def get(app, name):
    async with make_client(app) as c:
        r = await c.get(f"/api/research/{name}", headers=HDR)
    assert r.status_code == 200
    return r.json()


def patch(monkeypatch, **frames):
    calls = {k: 0 for k in frames}
    for k, df in frames.items():
        def make(k=k, df=df):
            def loader():
                calls[k] += 1
                if isinstance(df, Exception):
                    raise df
                return df
            return loader
        monkeypatch.setattr(rv, f"load_{k}_closes", make())
    return calls


async def test_momentum_ranks_gap_and_held(app, monkeypatch):
    rates = {f"S{i:02d}": 0.0001 * (12 - i) for i in range(12)}      # S00 strongest ... S11 weakest
    patch(monkeypatch, sp500=_frame(rates))
    app.state.db.set_sleeve_holding("momentum", "S00", "stock", 7, 10.0)
    app.state.db.set_sleeve_holding("momentum", "S11", "stock", 3, 10.0)
    j = await get(app, "momentum")
    assert j["n_target"] == 10 and j["universe_n"] == 12 and len(j["rows"]) == 12
    r = j["rows"]
    assert [x["symbol"] for x in r[:3]] == ["S00", "S01", "S02"] and r[0]["rank"] == 1
    cl = _frame(rates)
    exp = (cl.shift(21) / cl.shift(252) - 1).iloc[-1]
    assert r[0]["score"] == pytest.approx(exp["S00"])
    assert r[9]["gap_to_cutoff"] == pytest.approx(0.0)
    assert r[0]["gap_to_cutoff"] == pytest.approx(exp["S00"] - exp["S09"])
    assert r[10]["gap_to_cutoff"] < 0 and r[10]["in_target"] is False and r[9]["in_target"] is True
    assert r[0]["held"] is True and r[0]["held_qty"] == 7 and r[1]["held"] is False
    assert r[11]["held"] is True
    assert j["next_rebalance"] and j["as_of"]


async def test_vaa_bad_count_cash_and_allocation(app, monkeypatch):
    rates = {s: 0.001 for s in ["VNQ", "DBC", "GLD", "TLT", "HYG", "LQD"]}
    rates.update({s: -0.001 for s in ["SPY", "IWM", "QQQ", "VGK", "EWJ"]})
    rates.update({"VWO": 0.002, "IEF": 0.0015, "SHY": 0.0001})
    patch(monkeypatch, vaa=_frame(rates))
    app.state.db.set_sleeve_holding("vaa", "SHY", "stock", 5, 80.0)
    j = await get(app, "vaa")
    assert j["B"] == 4 and j["bad"] == 5 and j["cash_fraction"] == 1.0
    assert {o["symbol"] for o in j["offensive"] if o["bad"]} == {"SPY", "IWM", "QQQ", "VGK", "EWJ"}
    assert len(j["offensive"]) == 12 and j["offensive"][0]["symbol"] == "VWO" and j["offensive"][0]["rank"] == 1
    assert j["best_defensive"] == "IEF" and j["allocation_if_today"] == {"IEF": 1.0}
    assert [d["symbol"] for d in j["defensive"]] == ["LQD", "IEF", "SHY"]
    assert j["held"] == {"SHY": 5.0}


async def test_vaa_partial_cash(app, monkeypatch):
    rates = {s: 0.001 + 0.0001 * i for i, s in enumerate(
        ["SPY", "IWM", "QQQ", "VGK", "EWJ", "VWO", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD", "IEF", "SHY"])}
    rates.update({"SPY": -0.001, "IWM": -0.001})
    patch(monkeypatch, vaa=_frame(rates))
    j = await get(app, "vaa")
    assert j["bad"] == 2 and j["cash_fraction"] == 0.5
    assert sum(j["allocation_if_today"].values()) == pytest.approx(1.0)


async def test_putwrite_pnl_distance_collateral(app, monkeypatch):
    sym = "QQQ261218P00580000"
    app.state.db.set_sleeve_holding("putwrite", sym, "option", -2, 5.0)
    app.state.db.upsert_sleeve_state("putwrite", cash=1234.0, start_capital=1.0)
    app.state.engine.broker = FakeBroker(prices={"QQQ": 600.0}, snaps={sym: {
        "bid": 3.8, "ask": 4.2, "last": 4.5, "delta": -0.2, "iv": 0.25}})
    j = await get(app, "putwrite")
    assert j["underlying"] == "QQQ" and j["spot"] == 600.0 and j["cash"] == 1234.0 and j["rule"]
    p = j["puts"][0]
    assert p["strike"] == 580 and p["contracts"] == 2 and p["expiration"] == "2026-12-18"
    assert p["premium_received"] == 1000.0 and p["mark"] == pytest.approx(4.0)
    assert p["unrealized_pnl"] == pytest.approx(1000.0 - 4.0 * 100 * 2)
    assert p["distance_pct"] == pytest.approx(20 / 600) and p["collateral"] == 116000
    assert p["prob_itm"] == 0.2 and p["delta"] == -0.2 and p["iv"] == 0.25
    assert p["dte"] == (pd.Timestamp("2026-12-18").date() - now_et().date()).days
    assert "2026-12-18" in p["next_action"] and "580" in p["next_action"]


async def test_overlap_lists_only_multi_holder_symbols(app, monkeypatch):
    db = app.state.db
    db.set_sleeve_holding("momentum", "AAPL", "stock", 5, 100.0)
    db.set_sleeve_holding("momentum", "MSFT", "stock", 3, 100.0)
    db.set_sleeve_holding("vaa", "SPY", "stock", 2, 400.0)
    db.set_shadow_holding("mom_skip1w", "MSFT", 4, 90.0)
    db.set_shadow_holding("mom_skip1w", "NVDA", 2, 90.0)
    app.state.engine.broker = FakeBroker(positions="Symbol: AAPL Qty: 15 Last: 200 Type: EQUITY\n"
                                                    "Symbol: MSFT Qty: 3 Last: 300 Type: EQUITY")
    app.state.engine.portfolio = FakePortfolio(
        positions=[{"symbol": "AAPL", "qty": 10, "market_value": 2000.0, "last": 200.0, "cost": 150.0}],
        net_liq=100000.0)
    j = await get(app, "overlap")
    assert j["net_liq"] == 100000.0
    assert [r["symbol"] for r in j["rows"]] == ["AAPL"]
    a = j["rows"][0]
    assert {h["strategy"] for h in a["holders"]} == {"swing", "momentum"} and all(h["live"] for h in a["holders"])
    assert a["total_value"] == pytest.approx(2000 + 5 * 200) and a["pct_of_net"] == pytest.approx(0.03)
    assert [r["symbol"] for r in j["shadow_overlap"]] == ["MSFT"]
    m = j["shadow_overlap"][0]
    assert {(h["strategy"], h["live"]) for h in m["holders"]} == {("momentum", True), ("mom_skip1w", False)}
    assert m["total_value"] == pytest.approx(7 * 300)


async def test_regime_spy_breadth_vix_and_gate(app, monkeypatch):
    mk = _frame({"SPY": 0.002, "^VIX": 0.0})
    mk["^VIX"] = np.linspace(30, 15, len(mk))
    sp = _frame({"UP1": 0.001, "UP2": 0.002, "UP3": 0.001, "DN": -0.002}, rows=250)
    vaa_rates = {s: 0.001 for s in ["SPY", "IWM", "QQQ", "VGK", "EWJ", "VWO", "VNQ", "DBC", "GLD", "TLT", "HYG",
                                     "LQD", "IEF", "SHY"]}
    patch(monkeypatch, market=mk, sp500=sp, vaa=_frame(vaa_rates))
    db = app.state.db
    ok, bad = {"name": "trend", "ok": True}, {"name": "volume", "ok": False}
    db.add_signal(None, {"symbol": "AAA", "consensus_checks": [ok, ok]})
    db.add_signal(None, {"symbol": "BBB", "consensus_checks": [ok, bad]})
    db.add_signal(None, {"symbol": "CCC", "consensus_checks": [{"name": "trend", "ok": False}, bad]})
    db.add_signal(None, {"symbol": "DDD", "consensus_checks": [ok, bad]})
    j = await get(app, "regime")
    spy = mk["SPY"]
    assert j["spy"]["above_200"] is True and j["spy"]["last"] == pytest.approx(spy.iloc[-1])
    assert j["spy"]["sma200"] == pytest.approx(spy.tail(200).mean())
    assert j["spy"]["ret_1m"] == pytest.approx(spy.iloc[-1] / spy.iloc[-22] - 1)
    assert j["breadth"] == {"pct_above_200": 0.75, "n": 4}
    assert j["vix"]["last"] == 15 and j["vix"]["pct_rank_1y"] == pytest.approx(1 / 252)
    assert j["vaa_bad"] == 0
    g = j["swing_gate"]
    assert (g["watched"], g["eligible"], g["pass"]) == (4, 3, 1)
    assert g["top_blockers"][0] == {"name": "volume", "n": 2}


async def test_cache_hit_and_shared_sp500(app, monkeypatch):
    rates = {f"S{i:02d}": 0.0001 * (12 - i) for i in range(12)}
    calls = patch(monkeypatch, sp500=_frame(rates), market=_frame({"SPY": 0.001, "^VIX": 0.0}),
                  vaa=_frame({"SPY": 0.001}))
    await get(app, "momentum")
    await get(app, "momentum")
    await get(app, "regime")                      # breadth reuses the same S&P 500 download
    assert calls["sp500"] == 1 and calls["market"] == 1


async def test_errors_are_200_with_error_key(app, monkeypatch):
    patch(monkeypatch, sp500=pd.DataFrame(), vaa=Exception("yahoo down"), market=pd.DataFrame())
    for name in ("momentum", "vaa", "regime"):
        j = await get(app, name)
        assert set(j) == {"as_of", "error"} and j["error"]


async def test_all_routes_resolve_and_never_order(app, monkeypatch):
    patch(monkeypatch, sp500=pd.DataFrame(), vaa=pd.DataFrame(), market=pd.DataFrame())
    for name in ENDPOINTS:                       # not captured by /api/research or the /api catch-all
        j = await get(app, name)
        assert "as_of" in j
    async with make_client(app) as c:
        assert (await c.get("/api/research", headers=HDR)).status_code != 404
