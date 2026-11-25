"""Engine passes date-aligned benchmark series to signal_at (HANDOFF item 8)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.backtest import engine
from app.backtest.data import BarStore
from app.backtest.engine import _align_benchmark, _synthetic_bars
from tests.test_backtest import _backtest_settings_with_overrides


def test_align_benchmark_by_date():
    t0 = datetime(2024, 1, 1)
    spy = [{"time": t0 + timedelta(days=i), "close": float(i)} for i in range(10)]
    sf = [{"i": i} for i in range(10)]
    sym = [{"time": t0 + timedelta(days=i)} for i in (5, 6, 8)]  # starts later, gap on day 7
    b, f, first = _align_benchmark(sym, spy, sf)
    assert [x["close"] for x in b] == [5.0, 6.0, 8.0]
    assert [x["i"] for x in f] == [5, 6, 8]
    assert first == 0
    sym2 = [{"time": t0 - timedelta(days=1)}] + sym
    assert _align_benchmark(sym2, spy, sf)[2] == 1


def test_engine_uses_date_aligned_benchmark(monkeypatch, tmp_path):
    s = _backtest_settings_with_overrides({"RELATIVE_STRENGTH_ENABLED": "true"})
    spy = _synthetic_bars(120, start_price=400.0, trend=0.0003, seed=3)
    sym = _synthetic_bars(120, start_price=100.0, trend=0.001, seed=1)[40:]  # starts 40 days later
    store = BarStore(tmp_path)
    store.save("LATE", sym)
    store.save("SPY", spy)
    seen = []
    real = engine.signal_at

    def spy_fn(bars, feats, idx, settings, **kw):
        bb = kw.get("benchmark_bars")
        if bb is not None:
            seen.append((bars[idx]["time"].date(), bb[idx]["time"].date(), len(bb) == len(bars)))
        return real(bars, feats, idx, settings, **kw)

    monkeypatch.setattr(engine, "signal_at", spy_fn)
    engine.run_backtest(s, universe=["LATE"], csv_dir=tmp_path, force_refresh=False)
    assert seen
    assert all(a == b and same for a, b, same in seen)
