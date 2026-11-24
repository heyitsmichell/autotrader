"""Swing-strategy pieces: stop-only bracket, risk sizing, stop-first bar rule, per-trade exit state,
same-day re-entry guard / cooldown and sell-side regulatory fees in the backtester."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest

from app.backtest import engine as eng
from app.backtest.costs import CostModel
from app.backtest.engine import Portfolio, run_backtest
from app.config import load_settings
from app.sizing import max_allowed_qty

ZERO = CostModel(commission_per_order=0.0, slippage_bps=0.0, atr_slippage_mult=0.0,
                 impact_bps=0.0, max_impact_bps=0.0)
D0 = date(2024, 1, 2)


def _settings(**kw):
    base = dict(profit_scale_out_enabled=False, profit_breakeven_after_r=0.0, profit_giveback_pct=0.0,
                profit_rsi_exhaustion=0.0, trailing_stop_enabled=False, backtest_max_hold_days=0,
                exit_on_trend_break_enabled=False, max_order_qty=100000, max_order_usd=1e9,
                max_position_usd=1e9, max_risk_per_trade_usd=200.0)
    base.update(kw)
    return dataclasses.replace(load_settings(), **base)


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c, "volume": 1_000_000}


def _open(pf, sym="X", entry=100.0, sl=95.0, tp=110.0, qty=10, day=D0):
    pf.fill_buy(sym, qty, entry, day, 2.5, sl, tp, "up", 0.5, None, 0.0, 0.0)


def _live(pf, sym, bar, day, atr=2.5):
    pf.process_bar_live(sym, bar, day, ZERO, 1e9, atr_now=atr, rsi_close=50.0)


def test_stop_wins_when_stop_and_target_in_same_bar():
    pf = Portfolio(_settings(), cash=100000.0)
    pf.cost_model = ZERO
    _open(pf)
    _live(pf, "X", _bar(100, 112, 94, 105), D0 + timedelta(days=1))   # range covers stop 95 and target 110
    assert len(pf.trades) == 1
    t = pf.trades[0]
    assert t.exit_reason == "stop" and t.exit_fill_price == pytest.approx(95.0) and t.pnl < 0


def test_gap_down_through_stop_fills_at_open():
    pf = Portfolio(_settings(), cash=100000.0)
    _open(pf)
    _live(pf, "X", _bar(90, 96, 89, 92), D0 + timedelta(days=1))
    assert pf.trades[0].exit_fill_price == pytest.approx(90.0)


def test_no_take_profit_leaves_winner_open():
    pf = Portfolio(_settings(), cash=100000.0)
    _open(pf, tp=None)
    _live(pf, "X", _bar(101, 150, 100, 149), D0 + timedelta(days=1))
    assert "X" in pf.positions and not pf.trades        # nothing sells at a target that does not exist


def test_peak_and_scale_state_reset_per_trade():
    s = _settings(profit_giveback_pct=20.0, profit_giveback_min_gain_pct=1.0,
                  profit_scale_out_enabled=True, profit_scale_out_r=1.0, profit_scale_out_second_r=2.0)
    pf = Portfolio(s, cash=100000.0)
    _open(pf, tp=None, qty=100)
    _live(pf, "X", _bar(101, 125, 100, 124), D0 + timedelta(days=1))   # big run: scale-outs + high peak
    assert pf.positions["X"]["tranches"] >= 1 and pf.positions["X"]["peak"] >= 125
    _live(pf, "X", _bar(124, 124, 80, 85), D0 + timedelta(days=2))     # crash: closed by stop/give-back
    assert "X" not in pf.positions
    _open(pf, entry=100.0, tp=None, qty=100, day=D0 + timedelta(days=3))   # re-enter the same symbol
    p = pf.positions["X"]
    assert p["peak"] == 100.0 and p["tranches"] == 0 and p["pending_exit"] is None and p["qty"] == p["qty0"] == 100
    # a 3% dip must not trip a give-back computed from the previous trade's 125 peak
    _live(pf, "X", _bar(100, 101, 97, 98), D0 + timedelta(days=4))
    assert "X" in pf.positions


def test_size_from_risk_ignores_order_usd_cap():
    kw = dict(limit_price=100.0, last_price=100.0, held=0.0, stop_loss=96.0, equity=25000.0)
    old = max_allowed_qty(_settings(size_from_risk=False, max_order_usd=500.0, max_position_usd=50000.0), **kw)
    new = max_allowed_qty(_settings(size_from_risk=True, risk_per_trade_pct=1.0, max_order_usd=500.0,
                                    max_position_usd=50000.0), **kw)
    assert old == 5.0                      # $500 order cap binds
    assert new == 62.0                     # 1% of $25k = $250 risk / $4 per share = 62.5 -> 62


def test_size_from_risk_respects_position_cap_and_pct_scales_with_equity():
    s = _settings(size_from_risk=True, risk_per_trade_pct=2.0, max_position_usd=3000.0)
    kw = dict(limit_price=100.0, last_price=100.0, held=0.0, stop_loss=98.0)
    assert max_allowed_qty(s, equity=25000.0, **kw) == 30.0     # $500 / $2 = 250 shares, $3000 cap -> 30
    s2 = dataclasses.replace(s, max_position_usd=1e9)
    assert max_allowed_qty(s2, equity=50000.0, **kw) == 500.0 and max_allowed_qty(s2, equity=25000.0, **kw) == 250.0


def test_sell_fees_added_to_trade_cost():
    m = dataclasses.replace(ZERO, sec_fee_rate=0.0000278, taf_per_share=0.000166, taf_max=8.30)
    assert m.sell_fees(1000, 100.0) == pytest.approx(0.0000278 * 100000 + 0.166)
    assert m.sell_fees(1_000_000, 100.0) == pytest.approx(0.0000278 * 1e8 + 8.30)      # TAF capped
    pf = Portfolio(_settings(), cash=100000.0)
    pf.cost_model = m
    _open(pf, qty=1000, entry=100.0)
    _live(pf, "X", _bar(100, 101, 90, 92), D0 + timedelta(days=1))
    assert pf.trades[0].commission == pytest.approx(m.sell_fees(1000, pf.trades[0].exit_fill_price))


def _write_csv(path, rows):
    path.write_text("date,open,high,low,close,volume\n" + "\n".join(
        f"{d},{o},{h},{l},{c},{v}" for d, o, h, l, c, v in rows), encoding="utf-8")


def _flat_world(tmp_path, monkeypatch, **kw):
    """TEST bars that dip below the stop every day, so an always-buy signal re-enters constantly."""
    rows, day = [], datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(30):
        d = (day + timedelta(days=i)).date().isoformat()
        rows.append((d, 100, 101, 90, 100, 1_000_000))
    for sym in ("TEST", "SPY"):
        _write_csv(tmp_path / f"{sym}.csv", rows)
    monkeypatch.setattr(eng, "signal_at", lambda bars, feats, idx, s, **k: {
        "ok": True, "sl": 95.0, "tp": 110.0,
        "signal": {"close": 100.0, "atr": 2.5, "ta_score": 0.5, "trend": "up", "ml_p": None}})
    return _settings(backtest_min_bars=3, backtest_initial_cash=100000.0, backtest_max_gross_exposure_pct=100.0,
                     backtest_live_exits=True, max_daily_loss_usd=0.0, max_sector_exposure_pct=0.0,
                     eligibility_enabled=False, **kw)


def test_bracket_take_profit_false_and_reentry_guard(tmp_path, monkeypatch):
    s = _flat_world(tmp_path, monkeypatch, bracket_take_profit=False, max_order_usd=5000.0, max_position_usd=5000.0)
    res = run_backtest(s, universe=["TEST"], csv_dir=tmp_path)
    assert res.trades and all(t.tp is None for t in res.trades)
    # every bar stops the position out; the next entry may not fill on the exit day
    days = [(t.entry_day, t.exit_day) for t in res.trades]
    assert all(e1 > x0 for (_, x0), (e1, _) in zip(days, days[1:]))
    s_tp = dataclasses.replace(s, bracket_take_profit=True)
    assert all(t.tp == 110.0 for t in run_backtest(s_tp, universe=["TEST"], csv_dir=tmp_path).trades)


def test_reentry_cooldown_spaces_entries(tmp_path, monkeypatch):
    s = _flat_world(tmp_path, monkeypatch, max_order_usd=5000.0, max_position_usd=5000.0)
    r0 = run_backtest(s, universe=["TEST"], csv_dir=tmp_path)
    r3 = run_backtest(s, universe=["TEST"], csv_dir=tmp_path, reentry_cooldown_days=3)
    assert len(r3.trades) < len(r0.trades)
    gaps = [(b.entry_day - a.exit_day).days for a, b in zip(r3.trades, r3.trades[1:])]
    assert gaps and min(gaps) >= 4
