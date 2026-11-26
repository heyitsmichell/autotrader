"""Backtest fixes round 2: trading-bar time stop, fill-price cash clamp, validate_backtester,
engine hooks (signal_fn / portfolio_factory), harness holdout logging, autopsy thresholds."""
from __future__ import annotations

import dataclasses
import os
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.backtest.costs import CostModel
from app.backtest import engine
from app.backtest.data import BarStore
from app.backtest.engine import Portfolio, run_backtest, validate_backtester, _synthetic_bars
from app.config import load_settings


def _settings(**kw):
    base = dict(
        backtest_commission_per_order=0.0, backtest_slippage_bps=0.0, backtest_atr_slippage_mult=0.0,
        backtest_impact_bps=0.0, backtest_max_impact_bps=0.0, backtest_max_gross_exposure_pct=100.0,
        backtest_max_hold_days=0, backtest_exit_on_trend_break=False, exit_on_trend_break_enabled=False,
        backtest_ml_enabled=False, backtest_live_exits=False, backtest_min_bars=3,
        backtest_initial_cash=10_000.0, max_order_qty=1000, max_order_usd=50_000.0,
        max_position_usd=50_000.0, max_risk_per_trade_usd=500.0, max_daily_loss_usd=0.0)
    base.update(kw)
    return dataclasses.replace(load_settings(), **base)


def _bar(d, o, h, l, c):
    return {"time": datetime(2020, 1, d, tzinfo=timezone.utc), "open": o, "high": h, "low": l,
            "close": c, "volume": 1_000_000}


# ---- 11: time stop counts trading bars ------------------------------------------------------
def test_time_stop_counts_trading_bars_not_calendar_days():
    s = _settings(backtest_max_hold_days=2)
    p = Portfolio(s, cash=10_000.0)
    cm = CostModel.from_settings(s)
    fri = date(2020, 1, 3)
    p.on_bar_start(fri, {})
    p.fill_buy("X", 10, 100.0, fri, 1.0, 50.0, None, "up", 1.0, None, 0.0, 0.0)
    flat = lambda: {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    p.on_bar_start(date(2020, 1, 6), {})          # Monday: 3 calendar days, 1 trading bar
    assert p.check_exits("X", flat(), date(2020, 1, 6), cm, 1e9) is None
    p.on_bar_start(date(2020, 1, 7), {})          # 2 trading bars held
    res = p.check_exits("X", flat(), date(2020, 1, 7), cm, 1e9)
    assert res is not None and res[2] == "time"


# ---- 12: cash never negative when fill > sizing limit ----------------------------------------
def test_marketable_fill_above_limit_does_not_overspend(tmp_path):
    bars = [_bar(1, 100, 101, 99, 100), _bar(2, 100, 102, 99.5, 101), _bar(3, 108, 109, 107, 108),
            _bar(4, 108, 109, 107, 108), _bar(5, 108, 109, 107, 108)]
    BarStore(tmp_path).save("TEST", bars)
    s = _settings(backtest_live_exits=True, entry_order_type="marketable", price_band_pct=10.0)
    grabbed = []

    def sig(symbol, blist, feats, index, settings, **kw):
        if index != 1:
            return {"ok": False}
        return {"ok": True, "sl": 99.0, "tp": None,
                "signal": {"close": 101.0, "atr": 1.0, "ta_score": 1.0, "trend": "up", "ml_p": None}}

    def factory(settings, cash):
        p = Portfolio(settings, cash=cash)
        grabbed.append(p)
        return p

    run_backtest(s, universe=["TEST"], benchmark_symbol="TEST", csv_dir=tmp_path,
                 signal_fn=sig, portfolio_factory=factory)
    p = grabbed[0]
    assert p.cash >= 0.0
    pos = p.positions["TEST"]
    assert pos["entry_price"] == 108.0
    assert pos["qty"] * 108.0 <= 10_000.0 and pos["qty"] < 99   # 99 sized at 101 would cost 10692


# ---- 13: validate_backtester is real and leaves os.environ alone -------------------------------
def test_validate_backtester_real_and_env_clean():
    before = dict(os.environ)
    out = validate_backtester()
    assert out["validation"] == "ok" and out["trades"] == 1 and out["pnl"] < 0
    assert dict(os.environ) == before


# ---- 14: hooks default to current behaviour -----------------------------------------------------
def test_hooked_run_matches_default(tmp_path):
    bars = _synthetic_bars(120, trend=0.002, seed=7)
    for sym in ("AAA", "SPY"):
        BarStore(tmp_path).save(sym, bars)
    s = _settings(consensus_ta_min=-1.0, eligibility_enabled=False)
    base = run_backtest(s, universe=["AAA"], benchmark_symbol="SPY", csv_dir=tmp_path)
    calls = []

    def sig(symbol, blist, feats, index, settings, **kw):
        calls.append(symbol)
        return engine.signal_at(blist, feats, index, settings, **kw)

    hooked = run_backtest(s, universe=["AAA"], benchmark_symbol="SPY", csv_dir=tmp_path, signal_fn=sig,
                          portfolio_factory=lambda st, cash: Portfolio(st, cash=cash))
    assert calls and set(calls) == {"AAA"}
    assert hooked.equity_curve == base.equity_curve
    assert [(t.symbol, t.entry_day, t.exit_day, t.pnl) for t in hooked.trades] == \
           [(t.symbol, t.entry_day, t.exit_day, t.pnl) for t in base.trades]


# ---- 15: run_variant logs the holdout peek -------------------------------------------------------
def test_run_variant_holdout_logs_peek(monkeypatch):
    from app.strategy_lab import harness
    seen = []
    monkeypatch.setattr(harness, "log_holdout", lambda name, h, *a, **k: seen.append((name, h)) or 2)

    def boom(src):
        raise RuntimeError("stop here")
    monkeypatch.setattr(harness, "data_cutoff", boom)
    name = next(iter(harness.REGISTRY))
    cwd = os.getcwd()
    try:
        harness.run_variant(name, scope="holdout")
    except RuntimeError:
        pass
    assert len(seen) == 1 and seen[0][0] == name
    assert os.getcwd() == cwd                     # no chdir side effect
    seen.clear()
    try:
        harness.run_variant(name, scope="research")
    except RuntimeError:
        pass
    assert seen == []


# ---- 16: autopsy thresholds from settings, units, meaningful stop check --------------------------
def test_autopsy_thresholds_from_settings():
    from app import autopsy
    trade = {"entry_fill_price": 100.0, "stop_price": 97.0, "entry_atr": 1.5,    # 2.0 ATR
             "entry_ta_score": 0.4, "entry_ml_p": 0.6}
    names = lambda cs: {c.cause for c in cs}
    assert "stop_placement_appropriate" in names(autopsy.analyze_stop_placement(trade))
    assert "stop_too_tight" in names(autopsy.analyze_stop_placement(trade, SimpleNamespace(sl_atr_mult=4.0)))
    assert "weak_ta_signal" not in names(autopsy.analyze_signal_quality(trade))
    strict = SimpleNamespace(consensus_ta_min=0.5, ml_min_p=0.7)
    got = names(autopsy.analyze_signal_quality(trade, strict))
    assert {"weak_ta_signal", "low_ml_probability"} <= got


def test_stop_protected_downside_is_meaningful():
    from app import autopsy
    base = {"exit_reason": "stop", "entry_fill_price": 100.0, "stop_price": 98.0, "mae": -2.1, "mfe": 0.5}
    ok = autopsy.analyze_mae_mfe({**base, "realized_pnl_pct": -2.05})
    assert "stop_protected_downside" in {c.cause for c in ok}
    gapped = autopsy.analyze_mae_mfe({**base, "realized_pnl_pct": -6.0, "mae": -6.5})
    assert "stop_protected_downside" not in {c.cause for c in gapped}
