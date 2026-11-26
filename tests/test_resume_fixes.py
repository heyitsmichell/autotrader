"""Regression tests for fixes made while resuming the QA programme (2026-11-24, after the pause).

  * EventHub drops a re-publish of an identical snapshot (B3: the DB and the API publisher both
    publish `proposals`).
  * ML scorecard: any model, not only the newest; metrics decoded.
  * Option DTE uses the exchange (ET) date.
  * Lesson auto-apply swaps in a new frozen Settings instead of mutating it.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from app.eventhub import EventHub
from tests.test_api_support import make_app, make_client


async def test_hub_drops_identical_snapshot_republish():
    hub = EventHub()
    q = hub.subscribe()
    hub.publish("proposals", [{"id": 1}])
    hub.publish("proposals", [{"id": 1}])          # identical: dropped
    hub.publish("proposals", [{"id": 1}, {"id": 2}])
    hub.publish("fill", {"id": 1})                  # streams are never deduplicated
    hub.publish("fill", {"id": 1})
    await asyncio.sleep(0)
    seen = []
    while not q.empty():
        ev = q.get_nowait()
        seen.append((ev.type, len(ev.data) if isinstance(ev.data, list) else None))
    assert seen == [("proposals", 1), ("proposals", 2), ("fill", None), ("fill", None)]


async def test_hub_dedupe_resets_when_latest_is_cleared():
    hub = EventHub()
    q = hub.subscribe()
    hub.publish("kill", {"on": True})
    hub._latest.clear()
    hub.publish("kill", {"on": True})
    await asyncio.sleep(0)
    assert q.qsize() == 2


async def test_ml_scorecard_for_an_older_model(tmp_path):
    app = make_app(tmp_path)
    db = app.state.db
    try:
        old = db.add_model(created_at="2026-01-01T00:00:00+00:00", status="retired",
                           metrics={"precision": 0.61, "f1": 0.5, "feature_importance": [
                               {"feature": "rsi", "importance": 0.4}]})
        db.add_model(created_at="2026-02-01T00:00:00+00:00", status="champion", metrics={"precision": 0.7})
        async with make_client(app) as c:
            r = await c.get(f"/api/ml/{old}/scorecard")
            assert r.status_code == 200
            body = r.json()
            assert body["precision"] == pytest.approx(0.61)
            assert body["feature_importance"][0]["feature"] == "rsi"
            assert (await c.get("/api/ml/9999/scorecard")).status_code == 404
    finally:
        db.close()



async def test_ml_scorecard_falls_back_to_walk_forward_metrics(tmp_path):
    app = make_app(tmp_path)
    db = app.state.db
    try:
        mid = db.add_model(created_at="2026-11-25T00:00:00+00:00", status="champion",
                           metrics={"mean_auc": 0.51, "mean_hit_rate": 0.59, "total_test_trades": 300})
        async with make_client(app) as c:
            body = (await c.get(f"/api/ml/{mid}/scorecard")).json()
        assert body["auc"] == pytest.approx(0.51)
        assert body["precision"] == pytest.approx(0.59)
        assert body["test_trades"] == 300
        assert body["hit_rate"] == pytest.approx(0.59)
        assert body["auc_mean_folds"] == pytest.approx(0.51)
        assert body["passed"] is None and body["checks"] == {} and body["recall"] is None
    finally:
        db.close()


async def test_ml_scorecard_exposes_walk_forward_verdict(tmp_path):
    app = make_app(tmp_path)
    db = app.state.db
    try:
        mid = db.add_model(created_at="2026-11-25T00:00:00+00:00", status="rejected",
                           metrics={"mean_auc": 0.5, "mean_avg_return": -0.1, "passed": False,
                                    "rejection_reason": "auc too low",
                                    "total_independent_test_trades": 40,
                                    "checks": {"auc": False}, "thresholds": {"min_auc": 0.55}})
        async with make_client(app) as c:
            body = (await c.get(f"/api/ml/{mid}/scorecard")).json()
        assert body["passed"] is False and body["rejection_reason"] == "auc too low"
        assert body["checks"] == {"auc": False} and body["thresholds"] == {"min_auc": 0.55}
        assert body["total_independent_test_trades"] == 40
        assert body["mean_avg_return"] == pytest.approx(-0.1)
    finally:
        db.close()

def test_option_dte_uses_the_exchange_date(test_settings, monkeypatch):
    import app.risk as risk
    from app.market import ET
    orm = risk.OptionRiskManager(test_settings)
    # 23:30 ET on the 24th is already the 25th in UTC and in many local zones.
    monkeypatch.setattr(risk, "now_et", lambda: datetime(2026, 11, 24, 23, 30, tzinfo=ET))
    assert orm._days_to_expiration("2026-11-27") == 3
    assert orm._days_to_expiration("not-a-date") == -1


async def test_lesson_apply_replaces_frozen_settings(test_engine):
    before = test_engine.s
    test_engine.db.add_lesson(param_name="consensus_min_news", param_value=4.0, param_scope="global",
                              sample_size=50, min_sample_size=30, p_value=0.01, status="active",
                              created_by="human")
    test_engine._apply_active_lessons()
    assert test_engine.s is not before
    assert test_engine.s.consensus_min_news == 4 and isinstance(test_engine.s.consensus_min_news, int)
    assert before.consensus_min_news != 4 or before is not test_engine.s
    assert test_engine.refresh_lessons() == 0          # already in effect: nothing new


async def test_options_train_endpoint(tmp_path, monkeypatch):
    from app import options_ml
    app = make_app(tmp_path)
    monkeypatch.setattr(options_ml, "live_training_rows", lambda db: [])
    monkeypatch.setattr(options_ml, "train", lambda live, live_weight=1.0: None)
    try:
        async with make_client(app) as c:
            from tests.test_api_support import HDR
            r = await c.post("/api/options/train", headers=HDR)
        assert r.status_code == 409
    finally:
        app.state.db.close()
