"""Backtests tab API (docs/BACKTESTS_TAB.md)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import backtests_view as bv
from app.config import env
from app.pipeline import PIPELINE
from tests.test_api_support import HDR, make_app, make_client

LB_HEAD = "name,kind,universe,N,start,cagr,vol,sharpe,maxdd,n_days\n"


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(env, "DATA_DIR", d)
    bv._pcache.update(t=0.0, v=None)
    return d


@pytest.fixture
def app(tmp_path, data):
    a = make_app(tmp_path)
    yield a
    a.state.db.close()


def seed_files(d: Path):
    ex = d / "experiments"
    (ex / "momentum_variants").mkdir(parents=True)
    (ex / "momentum_variants" / "leaderboard.csv").write_text(
        LB_HEAD + "sp500|N10|mom_skip1w,variant,sp500,10,2011-01-03,0.44,0.34,1.23,-0.45,3746\n")
    (ex / "overnight_momentum").mkdir()
    (ex / "overnight_momentum" / "leaderboard.csv").write_text(
        LB_HEAD + "sp500|N10|overnight_12m,variant,sp500,10,2011-01-03,0.40,0.34,1.16,-0.48,3746\n")
    (ex / "rotation").mkdir()
    (ex / "rotation" / "catalog_leaderboard.csv").write_text(
        "name,group,cagr,vol,sharpe,maxdd,n_days\nvaa_g4,taa,0.06,0.1,0.64,-0.17,3746\n")
    (ex / "trials_registry.csv").write_text(
        "ts,lab,strategy,config_hash,universe,start,end,cagr,sharpe,maxdd,turnover,notes\n"
        "t,momentum_variants,a,h,u,s,e,0,0,0,0,\nt,momentum_variants,b,h,u,s,e,0,0,0,0,\n")
    (ex / "putwrite_mgmt").mkdir()
    (ex / "putwrite_mgmt" / "REPORT.md").write_text("# Put-write management\nbody\n")
    bt = d / "backtests" / "swing_universe_2026-11-25"
    bt.mkdir(parents=True)
    (bt / "REPORT.md").write_text("# Swing universe study\nbody\n")
    (bt / "results.json").write_text(json.dumps({"windows": {bv.SWING_WINDOW: {"C": {
        "from": "2022-11-21", "to": "2025-11-21", "cagr": 0.16, "sharpe": 0.89, "max_dd": 0.24}}}}))


def seed_equity(db):
    base = date.today() - timedelta(days=10)
    for i in range(6):
        day = (base + timedelta(days=i)).isoformat()
        db.record_sleeve_equity(day, "momentum", 100.0 * (1 + 0.01 * i))
        db.record_sleeve_equity(day, "total", 400.0 * (1 + 0.005 * i))
        db.record_shadow_equity(day, "mom_skip1w", 100.0 * (1 + 0.02 * i))
    db.conn.commit()


async def get(app, url):
    async with make_client(app) as c:
        return await c.get(url, headers=HDR)


async def test_strategies_with_files_and_live(app, data):
    seed_files(data)
    seed_equity(app.state.db)
    r = await get(app, "/api/backtests/strategies")
    assert r.status_code == 200
    cards = r.json()["cards"]
    assert [c["id"] for c in cards] == ["swing", "momentum", "putwrite", "vaa",
                                         "mom_skip1w", "overnight_mom", "vaa_g4", "swing_dynamic"]
    by = {c["id"]: c for c in cards}
    assert by["momentum"]["backtest"]["cagr"] is not None and by["momentum"]["backtest"]["biased"] is True
    assert by["momentum"]["live"]["days"] == 5 and by["momentum"]["live"]["return_pct"] == pytest.approx(5.0)
    assert by["mom_skip1w"]["backtest"]["sharpe"] == 1.23 and by["mom_skip1w"]["backtest"]["biased"] is True
    assert by["mom_skip1w"]["trials"] == {"lab": "momentum_variants", "n": 2}
    assert by["mom_skip1w"]["live"]["return_pct"] == pytest.approx(10.0)
    assert by["vaa_g4"]["backtest"]["cagr"] == 0.06
    assert by["swing_dynamic"]["backtest"]["max_dd"] == -0.24
    assert by["swing_dynamic"]["compare_to"] == "swing"
    assert by["putwrite"]["evidence"][0]["exists"] is True


async def test_strategies_missing_files_never_500(app, data):
    r = await get(app, "/api/backtests/strategies")
    assert r.status_code == 200
    by = {c["id"]: c for c in r.json()["cards"]}
    assert by["mom_skip1w"]["backtest"] is None and by["mom_skip1w"]["note"]
    assert by["swing_dynamic"]["backtest"] is None and by["swing_dynamic"]["note"]
    assert by["swing"]["live"] is None
    assert by["putwrite"]["evidence"][0]["exists"] is False


async def test_portfolio_shape_and_cache(app, data):
    seed_equity(app.state.db)
    r = await get(app, "/api/backtests/portfolio")
    assert r.status_code == 200
    j = r.json()
    assert j["window"] and ".." in j["window"]
    assert [s["name"] for s in j["schemes"]] == ["Equal 25%", "Inverse volatility", "Equal risk"]
    assert all(s["cagr"] is not None for s in j["schemes"])
    assert {"day", "equal", "invvol"} <= set(j["curve"][0])
    ids = j["correlation"]["ids"]
    assert ids == ["swing", "momentum", "putwrite", "vaa"] and len(j["correlation"]["matrix"]) == 4
    assert j["correlation"]["matrix"][0][0] == pytest.approx(1.0)
    assert j["live"]["days"] == 5
    assert bv._pcache["v"] is not None


async def test_pipeline_stages_and_shadow_vs_live(app, data):
    seed_files(data)
    seed_equity(app.state.db)
    r = await get(app, "/api/backtests/pipeline")
    st = r.json()["stages"]
    assert set(st) == {"live", "shadow", "rejected"}
    assert [e["id"] for e in st["live"]] == ["swing", "momentum", "putwrite", "vaa"]
    assert len(st["shadow"]) == 4 and len(st["rejected"]) >= 8
    sh = next(e for e in st["shadow"] if e["id"] == "mom_skip1w")
    assert sh["compare_to"] == "momentum"
    assert sh["live_stats"]["return_pct"] == pytest.approx(10.0)
    assert sh["compare_stats"]["return_pct"] == pytest.approx(5.0)


async def test_ml_empty_and_with_model(app, data):
    r = await get(app, "/api/backtests/ml")
    j = r.json()
    assert r.status_code == 200 and j["models"] == [] and j["summary"]
    assert all(e["kind"] == "ml" for e in j["experiments"]) and j["experiments"]
    app.state.db.add_model(status="champion", metrics={"auc": 0.61})
    j = (await get(app, "/api/backtests/ml")).json()
    assert j["models"][0]["status"] == "champion" and j["models"][0]["auc"] == 0.61
    assert j["models"][0]["kind"] == "entry"


async def test_report_serves_backtests_and_rejects_traversal(app, data):
    seed_files(data)
    ok = await get(app, "/api/experiments/report?path=backtests/swing_universe_2026-11-25/REPORT.md")
    assert ok.status_code == 200 and "Swing universe study" in ok.json()["text"]
    old = await get(app, "/api/experiments/report?path=putwrite_mgmt/REPORT.md")
    assert old.status_code == 200
    for bad in ("backtests/../experiments/putwrite_mgmt/REPORT.md", "backtests/../../x.md",
                "../secret.md", "backtests/%2e%2e/x.md", "backtests/nope.md"):
        r = await get(app, "/api/experiments/report?path=" + bad)
        assert r.status_code in (400, 404), bad
    lst = (await get(app, "/api/experiments")).json()["reports"]
    paths = [x["path"] for x in lst]
    assert "backtests/swing_universe_2026-11-25/REPORT.md" in paths and "putwrite_mgmt/REPORT.md" in paths


def test_real_pipeline_evidence_exists():
    real = Path(__file__).resolve().parents[1] / "data"
    if not (real / "experiments").is_dir():
        pytest.skip("data/experiments absent")
    missing = [p for e in PIPELINE for p in e["evidence"] if not (real / p).is_file()]
    assert not missing
