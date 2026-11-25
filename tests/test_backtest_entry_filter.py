"""run_backtest(entry_allowed=...) hook: None changes nothing, a False hook blocks all new entries."""
from __future__ import annotations

from app.backtest.data import BarStore
from app.backtest.engine import _synthetic_bars, run_backtest
from tests.test_backtest import _backtest_settings_with_overrides


def _setup(tmp_path):
    s = _backtest_settings_with_overrides({"BACKTEST_MIN_BARS": "3", "CONSENSUS_TA_MIN": "-1.0"})
    store = BarStore(tmp_path)
    store.save("TEST", _synthetic_bars(120, start_price=100.0, trend=0.001, seed=1))
    store.save("SPY", _synthetic_bars(120, start_price=400.0, trend=0.0003, volatility=0.008, seed=2))
    return s


def _key(r):
    return [(t.symbol, t.entry_day, t.exit_day, round(t.pnl, 6)) for t in r.trades], list(r.equity_curve)


def test_entry_hook_none_is_noop_and_false_blocks(tmp_path):
    s = _setup(tmp_path)
    base = run_backtest(s, universe=["TEST"], csv_dir=tmp_path)
    assert len(base.trades) >= 1
    assert _key(run_backtest(s, universe=["TEST"], csv_dir=tmp_path, entry_allowed=None)) == _key(base)
    allow = run_backtest(s, universe=["TEST"], csv_dir=tmp_path, entry_allowed=lambda d, sym: True)
    assert _key(allow) == _key(base)
    blocked = run_backtest(s, universe=["TEST"], csv_dir=tmp_path, entry_allowed=lambda d, sym: False)
    assert blocked.trades == []
