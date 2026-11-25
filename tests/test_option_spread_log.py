"""Option spread logger: dedupe, session gate, summary maths, never orders."""

import dataclasses
from datetime import datetime

import pytest

from app.engine.spread_log import SpreadLogger, due_times, parse_times, summarize
from app.market import ET, set_test_time
from tests.test_option_entries import FakeBroker, TODAY


@pytest.fixture
def eng(test_engine):
    test_engine.s = dataclasses.replace(
        test_engine.s, options_enabled=True, options_spread_log=True,
        options_allowed_underlyings=["AAPL", "MSFT"])
    fb = FakeBroker()
    fb.orders = []
    for name in ("place_order", "place_option_order", "submit_order"):
        setattr(fb, name, lambda *a, _n=name, **k: fb.orders.append(_n))
    test_engine.broker = fb
    return test_engine


def test_parse_and_due_times():
    assert parse_times("09:31, 9:33,bad,25:00,09:31") == ["09:31", "09:33"]
    ts = ["09:31", "09:33", "10:00"]
    assert due_times(ts, "09:32", set()) == ["09:31"]
    assert due_times(ts, "09:32", {"09:31"}) == []
    assert due_times(ts, "10:05", {"09:31"}) == ["10:00"]


def test_summary_maths():
    rows = [{"ts": "2026-11-20T15:00:00", "underlying": "AAPL", "minutes_from_open": 1.0, "spread_pct": v}
            for v in (2.0, 4.0, 6.0, 10.0)]
    out = summarize(rows, 5.0)
    b = out["overall"][0]
    assert out["rows"] == 4 and out["days"] == 1
    assert b["n"] == 4 and b["median_spread_pct"] == 5.0 and b["p75_spread_pct"] == 6.0
    assert b["pct_pass"] == 50.0
    assert "AAPL" in out["by_underlying"]


async def test_samples_once_per_time_and_stores_rows(eng):
    set_test_time(datetime(2026, 11, 20, 9, 32, tzinfo=ET))
    sl = SpreadLogger(eng)
    n = await sl.maybe_sample()
    assert n >= 2 and eng.broker.calls["snap"] == 1
    assert await sl.maybe_sample() == 0            # deduped (09:31 already done)
    rows = eng.db.option_spreads(5)
    assert {r["kind"] for r in rows} <= {"target", "atm"}
    assert {r["underlying"] for r in rows} == {"AAPL", "MSFT"}
    assert rows[0]["minutes_from_open"] == 2.0
    set_test_time(datetime(2026, 11, 20, 9, 34, tzinfo=ET))
    assert await sl.maybe_sample() > 0             # 09:33 slot is new
    assert eng.broker.calls["contracts"] == 2      # chain cached for the day
    assert eng.broker.orders == []


async def test_skips_outside_core(eng):
    set_test_time(datetime(2026, 11, 20, 18, 0, tzinfo=ET))
    assert await SpreadLogger(eng).maybe_sample() == 0
    assert eng.broker.calls["contracts"] == 0


async def test_off_and_failure_never_raise(eng):
    set_test_time(datetime(2026, 11, 20, 10, 5, tzinfo=ET))
    eng.s = dataclasses.replace(eng.s, options_spread_log=False)
    assert await SpreadLogger(eng).maybe_sample() == 0
    eng.s = dataclasses.replace(eng.s, options_spread_log=True)

    async def boom(*a, **k):
        raise RuntimeError("x")
    eng.broker.parsed_option_snapshots = boom
    assert await SpreadLogger(eng).maybe_sample() == 0
    assert eng.broker.orders == []
