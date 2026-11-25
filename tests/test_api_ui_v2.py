"""UI v2 backend fields: protection states, experiments API, status settings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import experiments
from app.api.portfolio import protection_states
from app.api.status import build_status
from tests.test_api_support import HDR, make_app, make_client

S = SimpleNamespace(extended_hours_soft_stop=True, overnight_trading=True, overnight_symbols=["AAPL"])
POS = [{"symbol": "AAPL"}]
ROW = [{"symbol": "AAPL", "kind": "sl", "price": 9.0}]   # protective_orders kind for a stop leg


def trade(**kw):
    t = {"id": 1, "symbol": "AAPL", "side": "BUY", "entry_fill_price": 10.0,
         "exit_fill_price": None, "stop_price": 9.0}
    t.update(kw)
    return t


def state(trades, rows, session, s=S, kv=lambda k: None):
    return protection_states(POS, trades, rows, session, s, kv)["AAPL"]


def test_protection_states():
    assert state([trade()], ROW, "CORE")["state"] == "broker"
    assert state([trade()], [], "CORE")["state"] == "missing"
    assert state([trade(stop_price=None)], [], "ALL")["state"] == "missing"
    assert state([trade()], ROW, "ALL")["state"] == "soft"
    off = SimpleNamespace(extended_hours_soft_stop=False, overnight_trading=True,
                          overnight_symbols=["AAPL"])
    assert state([trade()], ROW, "ALL", off)["state"] == "inactive"
    assert state([trade()], ROW, "NIGHT")["state"] == "soft"
    night_no = SimpleNamespace(extended_hours_soft_stop=True, overnight_trading=False,
                               overnight_symbols=[])
    assert state([trade()], ROW, "NIGHT", night_no)["state"] == "closed"
    r = state([trade()], ROW, None)
    assert r["state"] == "closed" and r["stop"] == 9.0
    assert state([trade()], ROW, "ALL", kv=lambda k: k == "soft_stop:1")["state"] == "soft_fired"
    assert state([], ROW, "CORE")["stop"] == 9.0


@pytest.fixture
def exp_root(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    root.mkdir()
    monkeypatch.setattr(experiments, "_root", lambda: root.resolve())
    return root


async def _get(tmp_path, url):
    async with make_client(make_app(tmp_path)) as c:
        return await c.get(url, headers=HDR)


async def test_experiments_missing(tmp_path, exp_root):
    r = await _get(tmp_path, "/api/experiments")
    assert r.status_code == 200
    j = r.json()
    assert j["leaderboard"] == [] and j["reports"] == [] and j["registry"]["n_trials"] == 0
    assert j["intraday_entry"] is None


async def test_experiments_data_and_traversal(tmp_path, exp_root):
    (exp_root / "rotation").mkdir()
    (exp_root / "rotation" / "leaderboard.csv").write_text(
        "name,kind,cagr,sharpe,maxdd,dsr,universe\n"
        "a,sweep,0.2,1.0,-0.3,0.9,sp500\nb,sweep,0.1,0.8,-0.2,0.95,sector\n"
        "SPY_buyhold,benchmark,0.13,0.85,-0.32,,\n")
    (exp_root / "rotation" / "REPORT.md").write_text("# Title here\nbody")
    (tmp_path / "secret.md").write_text("x")
    j = (await _get(tmp_path, "/api/experiments")).json()
    assert [r["name"] for r in j["leaderboard"]] == ["b", "a"]
    assert j["leaderboard"][1]["survivorship_biased"] and not j["leaderboard"][0]["survivorship_biased"]
    assert j["benchmarks"][0]["name"] == "SPY_buyhold"
    assert j["reports"][0]["path"] == "rotation/REPORT.md" and j["reports"][0]["title"] == "Title here"
    ok = await _get(tmp_path, "/api/experiments/report?path=rotation/REPORT.md")
    assert ok.json()["text"].startswith("# Title")
    for bad in ("../secret.md", "..%2Fsecret.md", "/etc/x.md", "rotation/leaderboard.csv"):
        assert (await _get(tmp_path, "/api/experiments/report?path=" + bad)).status_code == 400
    assert (await _get(tmp_path, "/api/experiments/report?path=nope.md")).status_code == 404


async def test_status_settings_keys(tmp_path):
    app = make_app(tmp_path)
    st = await build_status(app.state)
    for k in ("eligibility_earnings_blackout_days", "extended_hours_soft_stop",
              "exit_max_hold_days", "overnight_trading", "overnight_symbols"):
        assert k in st["settings"]



def test_target_leg_alone_is_not_a_stop():
    """Regression (2026-11-25): a resting take-profit ("tp") must not count as protection, and
    the real stop kind "sl" must (the first version only matched "stop")."""
    from app.api.portfolio import protection_states
    from types import SimpleNamespace
    st = SimpleNamespace(extended_hours_soft_stop=True, overnight_trading=False, overnight_symbols=[])
    pos = [{"symbol": "AAPL"}]
    tp_only = protection_states(pos, [], [{"symbol": "AAPL", "kind": "tp", "price": 12.0}],
                                "CORE", st, lambda k: None)
    assert tp_only["AAPL"]["state"] == "missing"
    sl = protection_states(pos, [], [{"symbol": "AAPL", "kind": "sl", "price": 9.0}],
                           "CORE", st, lambda k: None)
    assert sl["AAPL"]["state"] == "broker" and sl["AAPL"]["stop"] == 9.0
