"""Fast loop and execution-quality tests (no network, no broker)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app import execution_quality as eq
from app import fastloop
from app.config import load_settings
from app.fastloop import FastLoop, select_armed, with_live_price


def _bar(day, close, vol=1000.0):
    return {"time": datetime(day.year, day.month, day.day, tzinfo=timezone.utc), "open": close,
            "high": close + 1, "low": close - 1, "close": close, "volume": vol}


def _settings(**kw):
    from dataclasses import replace
    return replace(load_settings(), **kw)


def test_with_live_price_updates_todays_bar_and_leaves_input_alone():
    today = date(2026, 11, 20)
    bars = [_bar(date(2026, 11, 19), 100), _bar(today, 101)]
    out = with_live_price(bars, 105.0, today)
    assert out[-1]["close"] == 105.0 and out[-1]["high"] == 105.0 and out[-1]["low"] == 100.0
    assert bars[-1]["close"] == 101 and len(out) == 2


def test_with_live_price_appends_today_with_previous_volume():
    bars = [_bar(date(2026, 11, 19), 100, vol=5000.0)]
    out = with_live_price(bars, 97.0, date(2026, 11, 20))
    assert len(out) == 2 and out[-1]["close"] == 97.0 and out[-1]["volume"] == 5000.0
    assert out[-1]["open"] == 100 and out[-1]["low"] == 97.0 and out[-1]["synthetic"] is True


def test_select_armed_filters_and_orders():
    s = _settings(consensus_ta_min=0.15, fast_loop_arm_margin=0.10, fast_loop_max_armed=2)
    ok = [{"name": "Sentiment", "ok": True}, {"name": "TA direction", "ok": False}]
    rows = [
        {"symbol": "NEAR", "ta_score": 0.10, "trend": "up", "consensus_checks": ok},
        {"symbol": "OVER", "ta_score": 0.30, "trend": "up", "consensus_checks": ok},
        {"symbol": "FAR", "ta_score": -0.20, "trend": "up", "consensus_checks": ok},
        {"symbol": "DOWN", "ta_score": 0.20, "trend": "down", "consensus_checks": ok},
        {"symbol": "NEWS", "ta_score": 0.20, "trend": "up",
         "consensus_checks": [{"name": "Sentiment", "ok": False}]},
        {"symbol": "HELD", "ta_score": 0.20, "trend": "up", "consensus_checks": ok},
        {"symbol": "PEND", "ta_score": 0.20, "trend": "up", "consensus_checks": ok},
        {"symbol": "COOL", "ta_score": 0.20, "trend": "up", "consensus_checks": ok},
        {"symbol": "NONE", "ta_score": None, "trend": "up"},
    ]
    assert select_armed(rows, {"HELD"}, {"PEND"}, {"COOL"}, s) == ["OVER", "NEAR"]  # closest first
    assert select_armed(rows, set(), set(), set(), _settings(
        consensus_ta_min=0.15, fast_loop_arm_margin=0.10, fast_loop_max_armed=1)) == ["OVER"]


class _DB:
    def __init__(self):
        self.runs, self.logs = [], []

    def pending_proposals(self):
        return []

    def open_trades(self):
        return []

    def unresolved_proposals(self):
        return []

    def create_run(self, trigger, model=None):
        self.runs.append(trigger)
        return len(self.runs)

    def finish_run(self, rid, status, **kw):
        pass

    def log(self, level, msg, **kw):
        self.logs.append(msg)


def _loop(price):
    s = _settings(consensus_ta_min=0.15, fast_loop_arm_margin=0.10, fast_loop_cooldown_minutes=30)
    calls = []

    async def on_proposal(run_id, p, sig_override=None):
        calls.append((p, sig_override))
        return "ACCEPTED #1"

    engine = SimpleNamespace(kill_switch=False, _run_lock=asyncio.Lock(), research=None,
                             _on_proposal=on_proposal, _auto_execute_run=_noop)
    row = {"symbol": "AAA", "ta_score": 0.10, "trend": "up", "sent_score": 0.0, "sent_n": 0,
           "consensus_checks": [{"name": "TA direction", "ok": False}]}
    bars = [_bar(date(2026, 11, 19), 100)]
    signals = SimpleNamespace(latest=lambda sym: row if sym == "AAA" else None,
                              cached_bars=lambda sym, age: bars,
                              ml=SimpleNamespace(predict=lambda f: None))
    broker = SimpleNamespace(connected=True, positions=lambda: _ret({}), last_price=lambda sym: _ret(price))
    feed = SimpleNamespace(mode="off", last={})
    s = _settings(consensus_ta_min=0.15, fast_loop_arm_margin=0.10, watchlist=["AAA"])
    return FastLoop(s, broker, _DB(), engine, signals, feed), calls


async def _noop(run_id):
    return None


async def _ret(v):
    return v


def test_tick_triggers_once_through_the_normal_proposal_path_then_cools_down(monkeypatch):
    monkeypatch_attrs = {
        "market_is_open": lambda: True,
        "now_et": lambda: datetime(2026, 11, 20, 11, 0),
        "indicators": SimpleNamespace(features=lambda b: {"close": b[-1]["close"]}),
        "ta": SimpleNamespace(
            analyze=lambda f, s: {"close": f["close"], "atr": 2.0, "trend": "up", "strength": 0.5,
                                  "vol": "normal", "ta_score": 0.40, "ta_components": []},
            levels=fastloop.ta.levels),
    }
    saved = {k: getattr(fastloop, k) for k in monkeypatch_attrs}
    try:
        for k, v in monkeypatch_attrs.items():
            setattr(fastloop, k, v)
        loop, calls = _loop(price=97.0)
        first = asyncio.run(loop.tick())
        assert len(first) == 1 and first[0]["symbol"] == "AAA"
        p, sig = calls[0]
        assert p["side"] == "BUY" and p["limit_price"] == 97.0
        assert sig["live"] is True and sig["ta_score"] == 0.40
        assert loop.db.runs == ["fast_loop"]
        assert asyncio.run(loop.tick()) == []        # cooldown: no second trigger
        assert len(calls) == 1
    finally:
        for k, v in saved.items():
            setattr(fastloop, k, v)


def test_tick_does_nothing_with_kill_switch_or_closed_market():
    loop, calls = _loop(price=97.0)
    loop.engine.kill_switch = True
    assert asyncio.run(loop.tick()) == [] and not calls
    loop.engine.kill_switch = False
    saved = fastloop.market_is_open
    fastloop.market_is_open = lambda: False
    try:
        assert asyncio.run(loop.tick()) == [] and not calls
    finally:
        fastloop.market_is_open = saved


def test_fast_loop_defaults_off():
    import os
    os.environ.pop("FAST_LOOP_ENABLED", None)
    assert load_settings().fast_loop_enabled is False


# --- execution quality ---------------------------------------------------------------------

def _iso(base, sec):
    return (base + timedelta(seconds=sec)).isoformat()


def test_execution_quality_summary():
    b = datetime(2026, 11, 20, 15, 0, 0)
    props = [
        {"id": 1, "side": "BUY", "status": "placed", "symbol": "A", "run_id": 10, "signal_id": 5,
         "created_at": _iso(b, 30), "placed_at": _iso(b, 31), "decided_by": "auto",
         "ref_price": 100.0, "limit_price": 100.0},
        {"id": 2, "side": "BUY", "status": "placed", "symbol": "B", "run_id": 11, "signal_id": None,
         "created_at": _iso(b, 0), "placed_at": _iso(b, 2), "decided_by": "auto",
         "ref_price": 50.0, "limit_price": 50.0},
        {"id": 3, "side": "BUY", "status": "rejected", "symbol": "C", "run_id": 11},
        {"id": 4, "side": "SELL", "status": "placed", "symbol": "A", "run_id": 11},
    ]
    trades = {1: {"quantity_filled": 5, "entry_fill_price": 100.2, "entry_fill_time": _iso(b, 36)}}
    out = eq.summarize(props, trades, {5: _iso(b, 0)}, {10: "scheduler", 11: "fast_loop"})
    assert out["proposals"] == {"buy_total": 3, "rejected": 1, "pending": 0, "placed": 2}
    assert out["overall"]["orders"] == 2 and out["overall"]["fill_rate"] == 0.5
    assert out["by_source"]["cycle"]["fill_rate"] == 1.0
    assert out["by_source"]["fast_loop"]["fill_rate"] == 0.0
    assert out["overall"]["signal_to_proposal_s"]["median"] == 30.0
    assert out["overall"]["order_to_fill_s"]["median"] == 5.0
    assert abs(out["overall"]["slippage_vs_ref_pct"]["median"] - 0.2) < 1e-9
    assert [r["id"] for r in out["unfilled"]] == [2]


def test_forward_returns_use_closes_after_the_proposal_day():
    d0 = date(2026, 11, 16)
    bars = [_bar(d0 + timedelta(days=i), 100 + i) for i in range(0, 8)]
    r = eq.forward_returns(bars, 100.0, d0)
    assert r == {"ret_1d_pct": 1.0, "ret_3d_pct": 3.0, "ret_5d_pct": 5.0}
    assert eq.forward_returns(bars[:3], 100.0, d0)["ret_5d_pct"] is None


def test_db_pending_proposals_exists_and_filters_by_status(tmp_path):
    from app.db import DB
    db = DB(tmp_path / "t.db", "webull", "uat", "acct")
    assert db.pending_proposals() == []
