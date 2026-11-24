"""Daily benchmark recording and the performance report (Phase 0)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.benchmark import record_daily_benchmark
from app.market import ET, session_close
from app.performance import build_performance
from conftest import db_tmpdir


def _insert_snapshot(db, day: date, net_liq: float, cash: float,
                     market_value: float, day_pnl: float) -> None:
    close = session_close(day)
    ts = datetime.combine(day, close, ET).isoformat()
    db.conn.execute(
        "INSERT INTO equity_snapshots "
        "(ts, net_liq, cash, market_value, day_pnl, broker_mode, webull_environment, account_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, net_liq, cash, market_value, day_pnl, db.broker_mode,
         db.webull_environment, db.account_id))


def _bar(day: date, close: float) -> dict:
    return {"time": datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
            "open": close, "high": close, "low": close, "close": close, "volume": 1_000_000}


@pytest.mark.asyncio
async def test_record_daily_benchmark_stores_closes_and_flows(test_db, test_settings):
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    _insert_snapshot(test_db, days[0], 10_000.0, 5_000.0, 5_000.0, 100.0)
    _insert_snapshot(test_db, days[1], 10_200.0, 5_000.0, 5_200.0, 150.0)
    _insert_snapshot(test_db, days[2], 10_300.0, 5_000.0, 5_300.0, 120.0)
    bars = [_bar(days[0], 470.0), _bar(days[1], 472.0), _bar(days[2], 471.0)]
    now = datetime(2024, 1, 5, 17, 0, tzinfo=ET)

    latest = await record_daily_benchmark(test_db, None, test_settings, bars=bars, now=now)
    assert latest is not None
    rows = test_db.benchmark_daily()
    assert [r["day"] for r in rows] == [d.isoformat() for d in days]
    assert [r["spy_close"] for r in rows] == [470.0, 472.0, 471.0]
    assert [r["net_liq"] for r in rows] == [10_000.0, 10_200.0, 10_300.0]
    # Day 2: +200 net change, +150 market P&L => +50 deposit.
    # Day 3: +100 net change, +120 market P&L => -20 withdrawal.
    assert [r["flow"] for r in rows] == [0.0, 50.0, -20.0]

    # Idempotent: a second run does not duplicate rows.
    await record_daily_benchmark(test_db, None, test_settings, bars=bars, now=now)
    assert len(test_db.benchmark_daily()) == 3


@pytest.mark.asyncio
async def test_record_daily_benchmark_skips_unfinished_session(test_db, test_settings):
    day = date(2024, 1, 4)
    _insert_snapshot(test_db, day, 10_000.0, 5_000.0, 5_000.0, 10.0)
    bars = [_bar(day, 470.0)]
    # 14:00 ET on the same day: the session is not finished.
    now = datetime(2024, 1, 4, 14, 0, tzinfo=ET)
    assert await record_daily_benchmark(test_db, None, test_settings, bars=bars, now=now) is None
    assert test_db.benchmark_daily() == []


def test_performance_reports_no_data():
    from app.db import DB
    import tempfile
    from pathlib import Path
    with db_tmpdir() as tmp:
        db = DB(Path(tmp) / "trader.db", "mock")
        out = build_performance(db, min_days=60)
        assert out["state"] == "no_data"
        assert out["enough_data"] is False
        assert out["significance"]["state"] == "insufficient_data"


def test_performance_not_enough_days_then_enough(test_db):
    start = date(2024, 1, 1)
    account = 10_000.0
    spy = 400.0
    for i in range(61):
        day = start + timedelta(days=i)
        # Add a little noise so the variance is measurable.
        account *= 1.001 + (0.0002 if i % 2 else -0.0001)
        spy *= 1.0005 + (0.0001 if i % 3 else -0.0001)
        test_db.upsert_benchmark_daily(day.isoformat(), net_liq=account, cash=account,
                                       market_value=0.0, spy_close=spy, day_pnl=0.0,
                                       flow=0.0)
    out = build_performance(test_db, min_days=60)
    assert out["state"] == "ok"
    assert out["days"] == 60
    assert out["enough_data"] is True
    assert out["excess_return_pct"] > 0
    assert out["account"]["total_return_pct"] > out["benchmark"]["total_return_pct"]
    assert out["significance"]["state"] == "ok"
    assert out["significance"]["p_value"] is not None
    assert len(out["series"]) == 60


def test_performance_below_min_window_says_not_enough_data(test_db):
    start = date(2024, 1, 1)
    for i in range(10):
        day = start + timedelta(days=i)
        test_db.upsert_benchmark_daily(day.isoformat(), net_liq=10_000.0 + i * 10,
                                       spy_close=400.0 + i, day_pnl=0.0, flow=0.0)
    out = build_performance(test_db, min_days=60)
    assert out["state"] == "ok"
    assert out["enough_data"] is False
    assert out["significance"]["state"] == "insufficient_data"
    assert out["significance"]["days_needed"] == 51


def test_performance_settings_are_loaded_and_frozen(monkeypatch):
    from dataclasses import replace
    from app.config import load_settings

    monkeypatch.setenv("PERFORMANCE_DAILY_ENABLED", "false")
    monkeypatch.setenv("PERFORMANCE_BENCHMARK_SYMBOL", "qqq")
    monkeypatch.setenv("PERFORMANCE_MIN_DAYS", "42")
    monkeypatch.setenv("PERFORMANCE_HISTORY_DAYS", "600")
    monkeypatch.setenv("PERFORMANCE_RISK_FREE_RATE", "0.04")
    s = load_settings()
    assert s.performance_daily_enabled is False
    assert s.performance_benchmark_symbol == "QQQ"
    assert s.performance_min_days == 42
    assert s.performance_history_days == 600
    assert s.performance_risk_free_rate == pytest.approx(0.04)

    # Settings stay frozen; tests and callers use dataclasses.replace.
    s2 = replace(s, performance_min_days=10)
    assert s2.performance_min_days == 10
    with pytest.raises(Exception):
        s.performance_min_days = 10  # type: ignore[misc]
