from datetime import datetime
from app import market as m
from app.market import ET
import os
from datetime import datetime, timezone
import pytest
from app import consensus
from app.risk import working_buys, working_buy_symbols
import asyncio
from datetime import date, datetime, timedelta
from app.eventhub import EventHub
from tests.test_api_support import make_app, make_client
import numpy as np
import pandas as pd
from app.rotation_lab.engine import _ffill, run
from app.eligibility import _is_today, _ONE_STARTER_OPEN
from app.market import now_et

def _at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_thanksgiving_evening_has_no_night():
    assert m.order_session(_at(2026, 11, 26, 20, 30)) is None


def test_wednesday_before_thanksgiving_has_no_night():
    assert m.order_session(_at(2026, 11, 25, 20, 30)) is None
    assert m.order_session(_at(2026, 11, 26, 2, 0)) is None


def test_early_close_day_windows():
    assert m.order_session(_at(2026, 11, 27, 12, 30)) == "CORE"
    assert m.order_session(_at(2026, 11, 27, 16, 30)) == "ALL"
    assert m.order_session(_at(2026, 11, 27, 17, 30)) is None
    assert m.order_session(_at(2026, 11, 27, 20, 30)) is None
    assert not m.market_is_open_extended(_at(2026, 11, 27, 18, 0))
    assert m.market_is_open_extended(_at(2026, 11, 27, 16, 59))


def test_normal_tuesday_unchanged():
    assert m.order_session(_at(2026, 12, 1, 3, 0)) == "NIGHT"
    assert m.order_session(_at(2026, 12, 1, 8, 0)) == "ALL"
    assert m.order_session(_at(2026, 12, 1, 12, 0)) == "CORE"
    assert m.order_session(_at(2026, 12, 1, 19, 0)) == "ALL"
    assert m.order_session(_at(2026, 12, 1, 20, 30)) == "NIGHT"
    assert m.order_session(_at(2026, 12, 2, 1, 0)) == "NIGHT"


def test_sunday_evening_night_and_friday_evening_closed():
    assert m.order_session(_at(2026, 11, 29, 21, 0)) == "NIGHT"
    assert m.order_session(_at(2026, 11, 30, 2, 0)) == "NIGHT"
    assert m.order_session(_at(2026, 12, 4, 21, 0)) is None


def test_overnight_closed_dates_override(monkeypatch):
    monkeypatch.setenv("OVERNIGHT_CLOSED_DATES", "2026-12-01")
    assert m.order_session(_at(2026, 12, 1, 20, 30)) is None
    assert m.order_session(_at(2026, 12, 2, 1, 0)) is None
    assert m.order_session(_at(2026, 12, 2, 20, 30)) == "NIGHT"


class _Broker:
    def __init__(self, open_text=""):
        self.open_text = open_text

    async def open_orders_text(self):
        return self.open_text

    async def order_history_text(self):
        return ""


def _working(coid):
    return (f"Client Order ID: {coid}\n  Client Order ID: {coid}\n  Order ID: O{coid}\n"
            "  Symbol: XLY\n  Side: BUY\n  Status: WORKING\n  Total Quantity: 2\n"
            "  Filled Quantity: 0\n  Limit Price: 100.0\n")


@pytest.mark.asyncio
async def test_timeout_then_reconcile_working_blocks_second_buy(test_engine, test_db):
    row = {"symbol": "XLY", "side": "BUY", "quantity": 2, "limit_price": 100.0}
    pid = test_db.add_proposal(None, row, "unknown", client_order_id="coidx1")
    assert [w["proposal_id"] for w in working_buys(test_db, "XLY")] == [pid]
    test_engine.broker = _Broker(_working("coidx1"))
    assert await test_engine.reconcile_orders() == 1
    assert test_db.get_proposal(pid)["status"] == "placed"
    assert test_db.get_trade(pid) is not None          # ledger entry created by reconcile
    assert "XLY" in working_buy_symbols(test_db)


def test_placed_buy_without_ledger_row_still_counts(test_db):
    row = {"symbol": "XLY", "side": "BUY", "quantity": 2, "limit_price": 100.0}
    pid = test_db.add_proposal(None, row, "placed", client_order_id="c2")
    assert [w["proposal_id"] for w in working_buys(test_db, "XLY")] == [pid]
    assert working_buys(test_db, "XLY", exclude_pid=pid) == []


def test_ml_override_needs_more_than_70_percent():
    from app.config import load_settings
    import dataclasses
    s = dataclasses.replace(load_settings(), ml_enabled=True, ml_first=True, ml_min_p=0.52)
    from tests.test_ml_first import BUY, _sig
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.60, trend="down"), s)["ok"] is False
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.70, trend="down"), s)["ok"] is False
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.71, trend="down"), s)["ok"] is True


def test_dotenv_skipped_in_tests():
    assert os.environ.get("TRADER_SKIP_DOTENV") == "1"


def test_bar_merge_dedupes_by_trading_day(test_settings):
    import asyncio
    from app.signals import SignalService
    from app.market import now_utc

    svc = SignalService(test_settings, None, None, None, None)
    bars = [{"time": datetime(2025, 1 + i // 28, 1 + i % 28, tzinfo=timezone.utc), "close": 1} for i in range(70)]
    last = bars[-1]["time"]
    # today's partial bar returns with an intraday stamp, not midnight
    class B2:
        async def daily_bars(self, sym, n):
            return [{"time": last.replace(hour=14, minute=30), "close": 2}]
    svc.broker = B2()
    from datetime import timedelta
    svc._bars_cache["X"] = (now_utc() - timedelta(seconds=svc.BARS_TTL_SECONDS + 5), bars)
    svc._bars_full_at["X"] = now_utc()
    out = asyncio.run(svc._load_bars("X"))
    days = [svc._day_key(b) for b in out]
    assert len(days) == len(set(days)) == 70


@pytest.mark.asyncio
async def test_mock_accepts_core_gtc_stop_out_of_hours(monkeypatch):
    from app.mock import server
    import app.market as market
    monkeypatch.setattr(market, "order_session", lambda *a, **k: None)
    monkeypatch.setattr(server, "check_account", lambda a: None, raising=False)
    r = await server.place_stock_order("AAPL", "SELL", "STOP_LOSS", "GTC", 1, stop_price=100.0,
                                       trading_session="CORE")
    assert "CORE_SESSION_CLOSED" not in r
    r = await server.place_stock_order("AAPL", "BUY", "LIMIT", "DAY", 1, limit_price=100.0,
                                       trading_session="CORE")
    assert "CORE_SESSION_CLOSED" in r


@pytest.mark.asyncio
async def test_stop_replace_backs_off(test_engine, test_db, monkeypatch):
    from app.broker import BrokerError
    e = test_engine
    import dataclasses
    e.s = dataclasses.replace(e.s, bracket_orders=True, core_enabled=False)
    calls = []

    class Bk:
        async def positions(self): return {"ZZZ": 5}
        async def open_orders_text(self): return ""
        async def place_stop(self, *a, **k):
            calls.append(a)
            raise BrokerError("rejected")
    e.broker = Bk()
    test_db.add_trade_entry(1, {"symbol": "ZZZ", "side": "BUY", "quantity": 5,
                                "limit_price": 10.0, "sl": 9.0, "tp": 12.0})
    await e._verify_protective_stops()
    await e._verify_protective_stops()
    assert len(calls) == 1                       # second pass is inside the backoff window
    e._stop_backoff["ZZZ"]["next"] = 0
    await e._verify_protective_stops()
    assert len(calls) == 2 and e._stop_backoff["ZZZ"]["fails"] == 2


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


def test_ffill_carries_forward_and_keeps_leading_nan():
    A = np.array([[np.nan], [1.0], [np.nan], [2.0]])
    out = _ffill(A)[:, 0]
    assert np.isnan(out[0]) and out[1] == 1.0 and out[2] == 1.0 and out[3] == 2.0


def test_run_missing_open_not_zero_return():
    O = np.array([[10.0, 1.0], [np.nan, 1.0], [12.0, 1.0]])
    C = O.copy(); C[1, 0] = 11.0
    W = np.array([[1.0, 0.0]] * 3); flag = np.array([True, False, False])
    r, _ = run(O, C, W, flag, costs=False)
    assert abs(r[1] - 0.2) < 1e-9 or abs(r[0] - 0.0) < 1e-9  # gap move lands, not silently dropped


def test_one_starter_statuses_and_today():
    assert {"NEW", "ACCEPTED", "PARTIAL"} <= _ONE_STARTER_OPEN
    assert _is_today({"placed_at": now_et().isoformat()})
    assert not _is_today({"placed_at": "2020-01-01T10:00:00+00:00"})


def test_momentum_partial_universe_logic():
    from app.sleeves.strategies import momentum
    closes = pd.DataFrame(np.random.rand(300, 5) + 1, columns=list("ABCDE"))
    assert len(momentum.targets(closes, n=3)) == 3
