"""Pure analytics (app/sleeves/analytics.py), reference data, migration, events repo, manager events."""
from __future__ import annotations

import gzip
import json
from datetime import date, timedelta

import pytest

from app.db import DB
from app.sleeves import analytics as an
from tests.test_sleeve_manager import env  # noqa: F401  (fixture)


def days(n, start=date(2026, 1, 5)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def mk(vals):
    return list(zip(days(len(vals)), vals))


def test_returns_and_drawdown_known():
    pts = list(zip(days(5), [100, 110, 99, 99, 110]))
    rets = an.returns_from_equity(pts)
    assert len(rets) == 4 and rets[0][1] == pytest.approx(0.1)
    dd = an.drawdown_series(rets)
    assert dd[1][1] == pytest.approx(-0.10) and dd[-1][1] == pytest.approx(0.0)
    assert all(v <= 0 for _, v in dd)


def test_returns_skip_none():
    pts = list(zip(days(4), [100, None, 110, 121]))
    assert [round(r, 4) for _, r in an.returns_from_equity(pts)] == [0.1, 0.1]


def test_worst_drawdowns_episodes():
    rets = mk([0.1, -0.2, 0.1, 0.2, -0.1, -0.1, 0.0])
    w = an.worst_drawdowns(rets, 5)
    assert len(w) == 2 and w[0]["depth"] <= w[1]["depth"]
    deep = next(e for e in w if e["depth"] == pytest.approx(-0.2))
    assert deep["end"] is not None and deep["trough"] == rets[1][0] and deep["days"] == 3
    open_ep = next(e for e in w if e["end"] is None)
    assert open_ep["depth"] == pytest.approx(0.81 - 1) and open_ep["days"] == 3


def test_monthly_yearly_sums():
    rets = [("2026-01-05", 0.01), ("2026-01-06", 0.02), ("2026-02-02", -0.5), ("2027-01-04", 0.1)]
    m = an.monthly_returns(rets)
    assert m[0] == {"year": 2026, "month": 1, "ret": pytest.approx(1.01 * 1.02 - 1)}
    assert m[1]["ret"] == pytest.approx(-0.5) and len(m) == 3
    y = an.yearly_returns(rets)
    assert y[0]["ret"] == pytest.approx(1.01 * 1.02 * 0.5 - 1) and y[1] == {"year": 2027, "ret": pytest.approx(0.1)}


def test_rolling_sharpe_lengths():
    rets = mk([0.01, -0.005] * 100)
    assert len(an.rolling_sharpe(rets, 126)) == 200 - 126 + 1
    assert an.rolling_sharpe(rets[:50], 126) == []


def test_metrics():
    assert an.metrics(mk([0.01] * 10))["sharpe"] is None
    m = an.metrics(mk([0.01, -0.004] * 50))
    assert m["cagr"] > 0 and m["max_dd"] < 0 and m["sharpe"] > 0 and m["sortino"] > 0 and m["vol"] > 0
    assert m["best_day"] == 0.01 and m["worst_day"] == -0.004 and m["pct_positive_days"] == 0.5
    assert m["calmar"] == pytest.approx(m["cagr"] / abs(m["max_dd"]))


def _pts(n, step=0.0):
    return list(zip(days(n), [100 * (1 + step) ** i for i in range(n)]))


def test_band_index_mapping():
    b = an.band_for("momentum", _pts(10, 0.001))
    ref = an.bands()["momentum"]
    assert len(b["points"]) == 10 and b["start"] == b["points"][0]["day"]
    assert b["points"][0]["live"] == 0 and b["points"][0]["p5"] == 0 == b["points"][0]["p95"]
    assert b["points"][3]["p50"] == ref["p50"][2]            # point i uses horizon i (index i-1)
    big = an.band_for("momentum", _pts(300, 0.0))
    assert big["points"][299]["p95"] == ref["p95"][251]      # capped at 252


def test_band_empty_and_none():
    assert an.band_for("vaa", [])["status"] == "insufficient"
    assert len(an.band_for("vaa", [("2026-01-05", None), ("2026-01-06", 100.0)])["points"]) == 1
    assert an.band_for("nope", _pts(10))["points"] == []


def test_drift_states():
    assert an.drift_status(an.band_for("momentum", _pts(4)))[0] == "insufficient"
    assert an.drift_status(an.band_for("momentum", _pts(10, 0.0)))[0] == "inside"
    below = an.band_for("momentum", [(d, 100 * (1 - 0.02 * i)) for i, d in enumerate(days(30))])
    st, n = an.drift_status(below)
    assert st == "below" and n >= 5 and below["status"] == "below"
    above = an.band_for("putwrite", [(d, 100 * 1.02 ** i) for i, d in enumerate(days(30))])
    assert an.drift_status(above) == ("above", 0)


def test_reference_files():
    bands = an.bands()
    assert set(bands) == {"swing", "momentum", "putwrite", "vaa"}
    for sid, b in bands.items():
        assert all(len(b[q]) == 252 for q in ("p5", "p25", "p50", "p75", "p95")) and "backtest" in b["window"]
        assert b["p5"][20] < b["p50"][20] < b["p95"][20]
        last = None
        with gzip.open(an.REF_DIR / f"{sid}.csv.gz", "rt") as f:
            assert f.readline().strip() == "date,ret"
            for line in f:
                last = line.split(",")[0]
        assert last <= "2025-11-21", (sid, last)
        assert last[:7] in b["window"]
    json.dumps(bands)


def test_migration_and_events_repo(tmp_path):
    a = DB(tmp_path / "a.db", "mock", None, None)
    b = DB(tmp_path / "b.db", "live", "prod", "ACC1")
    try:
        assert a.conn.execute("SELECT name FROM sqlite_master WHERE name='sleeve_events'").fetchone()
        a.add_sleeve_event("momentum", "rebalance", "x")
        a.add_sleeve_event(None, "kill_on", "y", ts="2020-01-01T00:00:00")
        assert [(e["sleeve"], e["kind"]) for e in a.sleeve_events(365)] == [("momentum", "rebalance")]
        assert len(a.sleeve_events(100000)) == 2
        assert a.sleeve_events(365, "vaa") == []
        a.conn.execute("INSERT INTO sleeve_events (ns, ts, day, kind, text) VALUES ('other', 't', '2099-01-01', 'k', 't')")
        assert all(e["kind"] != "k" for e in a.sleeve_events(100000))
        assert b.sleeve_events(365) == []
        b.add_sleeve_event(None, "settings", "z")
        assert len(b.sleeve_events(365)) == 1 and len(a.sleeve_events(365)) == 1
    finally:
        a.close()
        b.close()


async def test_manager_records_pause_resume(env):  # noqa: F811
    eng, mgr = env.build()
    await mgr.set_paused("momentum", True, "test")
    await mgr.set_paused("momentum", False, "ok")
    assert [e["kind"] for e in env.db.sleeve_events(365, "momentum")] == ["pause", "resume"]


async def test_manager_kill_transitions(env):  # noqa: F811
    eng, mgr = env.build()
    await mgr.tick()
    assert env.db.sleeve_events(365) == []            # first sight: no event
    eng.kill_switch = True
    await mgr.tick()
    eng.kill_switch = False
    await mgr.tick()
    assert [e["kind"] for e in env.db.sleeve_events(365) if e["sleeve"] is None] == ["kill_on", "kill_off"]


async def test_manager_settings_change(env):  # noqa: F811
    eng, mgr = env.build(max_order_usd=500.0)
    await mgr.tick()
    assert [e for e in env.db.sleeve_events(365) if e["kind"] == "settings"] == []
    eng2, mgr2 = env.build(max_order_usd=50000.0, max_order_qty=10)
    await mgr2.tick()
    ev = [e for e in env.db.sleeve_events(365) if e["kind"] == "settings"]
    assert len(ev) == 1 and "MAX_ORDER_USD 500 -> 50000" in ev[0]["text"] and "; " in ev[0]["text"]
    eng3, mgr3 = env.build(max_order_usd=50000.0, max_order_qty=10)
    await mgr3.tick()
    assert len([e for e in env.db.sleeve_events(365) if e["kind"] == "settings"]) == 1
