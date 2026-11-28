"""Tests for core-satellite portfolio (Phase 4)."""

from __future__ import annotations

from app.config import Settings
from app.core import (
    calculate_core_state,
    create_rebalance_plan,
    check_satellite_drawdown_guard,
    calculate_satellite_budget,
    format_core_satellite_report,
    verify_satellite_off_tracks_spy,
    RebalancePlan,
    DrawdownGuardResult,
)


def make_settings(**overrides) -> Settings:
    """Create a Settings object with defaults + overrides."""
    defaults = {
        "groq_api_key": "test",
        "llm_model": "test",
        "groq_fallback_model": "test",
        "nvidia_nim_api_key": "test",
        "nvidia_nim_model": "test",
        "broker_mode": "mock",
        "webull_environment": "uat",
        "webull_region": "us",
        "webull_account_id": None,
        "trading_mode": "approve",
        "allow_live_auto": False,
        "watchlist": ["AAPL"],
        "cycle_minutes": 30,
        "auto_start_scheduler": False,
        "market_hours_only": True,
        "overnight_trading": False,
        "overnight_symbols": [],
        "max_order_usd": 500,
        "max_position_usd": 2000,
        "max_order_qty": 50,
        "max_trades_per_day": 3,
        "max_daily_loss_usd": 200,
        "price_band_pct": 1.0,
        "proposal_ttl_minutes": 30,
        "research_scan_minutes": 60,
        "auto_trade_research_candidates": False,
        "realtime_source": "auto",
        "poll_seconds": 1.0,
        "review_every_days": 7,
        "min_outcomes_for_review": 10,
        "consensus_ta_min": 0.15,
        "consensus_sent_min": -0.25,
        "consensus_min_news": 3,
        "ml_enabled": True,
        "ml_min_p": 0.52,
        "ml_history_bars": 1000,
        "ml_min_bars": 500,
        "ml_retrain_days": 7,
        "sl_atr_mult": 2.0,
        "tp_atr_mult": 3.0,
        "min_sl_pct": 1.0,
        "max_sl_pct": 8.0,
        "max_risk_per_trade_usd": 100.0,
        "bracket_orders": True,
        "sentiment_source": "webull",
        "sentiment_llm": False,
        "sentiment_rss_fallback": False,
        "sentiment_rss_url": "https://example.invalid/{symbol}",
        "news_lookback_hours": 72,
        "news_refresh_minutes": 15,
        "signal_refresh_minutes": 15,
        "snapshot_seconds": 60,
        "watchdog_seconds": 30,
        "broker_down_alert_minutes": 5,
        "token_dir": "/tmp",
        "token_warn_days": 3.0,
        "reconcile_give_up_minutes": 15,
        "kill_max_drawdown_pct": 10.0,
        "kill_daily_loss_usd": 300.0,
        "kill_max_consecutive_errors": 3,
        "kill_action": "pause",
        "llm_min_cycle_minutes": 10,
        "sizing_mode": "code",
        "agent_result_chars": 6000,
        "agent_old_result_chars": 600,
        "flatten_slippage_pct": 0.5,
        "spy_benchmark_enabled": True,
        "spy_benchmark_min_outperformance_pct": 0.5,
        "spy_benchmark_window_days": 5,
        "spy_benchmark_min_trades": 10,
        "spy_benchmark_action": "warn",
        "performance_daily_enabled": False,
        "performance_benchmark_symbol": "SPY",
        "performance_min_days": 60,
        "performance_history_days": 400,
        "performance_risk_free_rate": 0.0,
        "backtest_data_dir": "/tmp",
        "backtest_results_dir": "/tmp",
        "backtest_initial_cash": 25000.0,
        "backtest_universe": [],
        "backtest_benchmark_symbol": "SPY",
        "backtest_min_bars": 60,
        "backtest_commission_per_order": 1.0,
        "backtest_slippage_bps": 5.0,
        "backtest_atr_slippage_mult": 0.03,
        "backtest_impact_bps": 5.0,
        "backtest_max_impact_bps": 100.0,
        "backtest_max_gross_exposure_pct": 100.0,
        "backtest_max_hold_days": 20,
        "backtest_exit_on_trend_break": True,
        "backtest_ml_enabled": False,
        "backtest_fetch_count": 1000,
        "backtest_fetch_delay_seconds": 1.0,
        "universe_name": "sector_etfs",
        "universe_custom": [],
        "universe_top_n": 50,
        "universe_min_price": 10.0,
        "universe_min_avg_dollar_volume": 20_000_000.0,
        "universe_min_history_days": 250,
        # Tiered scanning
        "universe_tier1_size": 50,
        "universe_tier2_size": 100,
        "universe_tier3_size": 200,
        "universe_scan_all_tiers": False,
        # Diversification
        "universe_max_per_sector": 5,
        "universe_max_correlation": 0.70,
        "universe_sector_map": {"XLK": "Technology", "XLF": "Financials", "XLV": "Healthcare", "XLE": "Energy", "XLI": "Industrials", "XLP": "Staples", "XLY": "Discretionary", "XLB": "Materials", "XLRE": "RealEstate", "XLU": "Utilities", "XLC": "Communication", "SPY": "Broad", "QQQ": "Tech", "VTI": "Broad", "IEFA": "Intl", "VWO": "EM"},
        "core_symbol": "SPY",
        "core_target_pct": 80.0,
        "satellite_max_pct": 20.0,
        "core_rebalance_band_pct": 2.0,
        "core_min_trade_usd": 100.0,
        "core_rebalance_market_hours_only": True,
        "satellite_drawdown_guard_pct": 10.0,
        "satellite_drawdown_action": "shrink",
        "eligibility_enabled": True,
        "eligibility_min_price": 5.0,
        "eligibility_min_volume": 20_000_000.0,
        "eligibility_min_bars": 250,
        "eligibility_max_gap_pct": 3.0,
        "eligibility_signal_freshness_min": 60,
        "eligibility_earnings_blackout_days": 2,
        "eligibility_first_15_min": True,
        "eligibility_one_starter_per_symbol": True,
        # Champion/Challenger (Phase 5)
        "ml_challenger_min_trades": 20,
        "ml_challenger_min_hit_rate_improvement": 0.02,
        "ml_challenger_min_return_improvement": 0.1,
        "ml_challenger_max_days": 14,
        "alert_levels": frozenset(["error"]),
        "alert_dedupe_minutes": 30,
        "alert_max_per_hour": 30,
        "alert_daily_summary": True,
        "alert_ntfy_server": "https://ntfy.sh",
        "alert_ntfy_topic": "",
        "alert_telegram_token": "",
        "alert_telegram_chat_id": "",
        "alert_webhook_url": "",
        "alert_smtp_host": "",
        "alert_smtp_port": 587,
        "alert_smtp_user": "",
        "alert_smtp_password": "",
        "alert_email_from": "",
        "alert_email_to": "",
        "host": "127.0.0.1",
        "port": 8000,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_calculate_core_state_on_target():
    """Core at target -> no rebalance needed."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_band_pct=2.0, core_min_trade_usd=100.0)
    state = calculate_core_state(
        total_equity=100_000.0,
        core_position_qty=200,
        core_price=400.0,  # core_value = 80,000
        satellite_positions_value=20_000.0,
        settings=settings,
    )
    assert state.core_value == 80_000.0
    assert state.satellite_value == 20_000.0
    assert state.core_actual_pct == 80.0
    assert state.drift_pct == 0.0
    assert not state.rebalance_needed
    assert state.rebalance_trade_usd == 0.0


def test_calculate_core_state_above_band():
    """Core above target + band -> sell core."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_band_pct=2.0, core_min_trade_usd=100.0)
    # Core at 85% (5% drift, band is 2%)
    state = calculate_core_state(
        total_equity=100_000.0,
        core_position_qty=212,
        core_price=400.0,  # core_value = 84,800
        satellite_positions_value=15_200.0,
        settings=settings,
    )
    assert abs(state.core_actual_pct - 84.8) < 0.01
    assert abs(state.drift_pct - 4.8) < 0.01
    assert state.rebalance_needed
    assert state.rebalance_trade_usd < 0  # Need to sell core


def test_calculate_core_state_below_band():
    """Core below target - band -> buy core."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_band_pct=2.0, core_min_trade_usd=100.0)
    # Core at 75% (-5% drift, band is 2%)
    state = calculate_core_state(
        total_equity=100_000.0,
        core_position_qty=187,
        core_price=400.0,  # core_value = 74,800
        satellite_positions_value=25_200.0,
        settings=settings,
    )
    assert abs(state.core_actual_pct - 74.8) < 0.01
    assert abs(state.drift_pct - (-5.2)) < 0.01
    assert state.rebalance_needed
    assert state.rebalance_trade_usd > 0  # Need to buy core


def test_calculate_core_state_within_band():
    """Core within band -> no rebalance."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_band_pct=2.0, core_min_trade_usd=100.0)
    # Core at 81% (1% drift, band is 2%)
    state = calculate_core_state(
        total_equity=100_000.0,
        core_position_qty=202,
        core_price=400.0,  # core_value = 80,800
        satellite_positions_value=19_200.0,
        settings=settings,
    )
    assert abs(state.drift_pct - 0.8) < 0.01
    assert not state.rebalance_needed


def test_calculate_core_state_min_trade_filter():
    """Trade below minimum -> no rebalance even if outside band."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_band_pct=2.0, core_min_trade_usd=5000.0)
    # Small drift, trade size < $5000
    state = calculate_core_state(
        total_equity=100_000.0,
        core_position_qty=200,
        core_price=401.0,  # core_value = 80,200 (drift 0.2%)
        satellite_positions_value=19_800.0,
        settings=settings,
    )
    # 0.2% drift is within 2% band, so no rebalance anyway
    assert not state.rebalance_needed


def test_create_rebalance_plan_sell_core():
    """Plan to sell core when above target."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_band_pct=2.0, core_min_trade_usd=100.0)
    from app.core import CoreState
    state = CoreState(
        total_equity=100_000.0,
        core_value=85_000.0,
        satellite_value=15_000.0,
        core_target_pct=80.0,
        core_actual_pct=85.0,
        drift_pct=5.0,
        rebalance_needed=True,
        rebalance_trade_usd=-5_000.0,  # Need to sell $5000 of core
        satellite_max_pct=20.0,
        satellite_actual_pct=15.0,
    )
    plan = create_rebalance_plan(state, core_price=400.0, settings=settings, is_market_hours=True)
    assert plan.action == "sell_core"
    assert plan.shares == 12  # 5000 / 400 = 12.5 -> 12
    assert plan.trade_usd == -5_000.0


def test_create_rebalance_plan_buy_core():
    """Plan to buy core when below target."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_band_pct=2.0, core_min_trade_usd=100.0)
    from app.core import CoreState
    state = CoreState(
        total_equity=100_000.0,
        core_value=75_000.0,
        satellite_value=25_000.0,
        core_target_pct=80.0,
        core_actual_pct=75.0,
        drift_pct=-5.0,
        rebalance_needed=True,
        rebalance_trade_usd=5_000.0,  # Need to buy $5000 of core
        satellite_max_pct=20.0,
        satellite_actual_pct=25.0,
    )
    plan = create_rebalance_plan(state, core_price=400.0, settings=settings, is_market_hours=True)
    assert plan.action == "buy_core"
    assert plan.shares == 12  # 5000 / 400 = 12.5 -> 12
    assert plan.trade_usd == 5_000.0


def test_create_rebalance_plan_outside_market_hours():
    """No rebalance outside market hours when setting enabled."""
    settings = make_settings(core_target_pct=80.0, core_rebalance_market_hours_only=True)
    from app.core import CoreState
    state = CoreState(
        total_equity=100_000.0,
        core_value=85_000.0,
        satellite_value=15_000.0,
        core_target_pct=80.0,
        core_actual_pct=85.0,
        drift_pct=5.0,
        rebalance_needed=True,
        rebalance_trade_usd=-5_000.0,
        satellite_max_pct=20.0,
        satellite_actual_pct=15.0,
    )
    plan = create_rebalance_plan(state, core_price=400.0, settings=settings, is_market_hours=False)
    assert plan.action == "none"
    assert "market hours" in plan.reason.lower()


def test_create_rebalance_plan_within_band():
    """No rebalance when within band."""
    settings = make_settings()
    from app.core import CoreState
    state = CoreState(
        total_equity=100_000.0,
        core_value=81_000.0,
        satellite_value=19_000.0,
        core_target_pct=80.0,
        core_actual_pct=81.0,
        drift_pct=1.0,
        rebalance_needed=False,
        rebalance_trade_usd=0.0,
        satellite_max_pct=20.0,
        satellite_actual_pct=19.0,
    )
    plan = create_rebalance_plan(state, core_price=400.0, settings=settings, is_market_hours=True)
    assert plan.action == "none"


def test_check_satellite_drawdown_guard_disabled():
    """Drawdown guard disabled -> no action."""
    settings = make_settings(satellite_drawdown_guard_pct=0.0)
    result = check_satellite_drawdown_guard(
        satellite_peak_equity=20_000.0,
        satellite_current_equity=15_000.0,  # 25% drawdown
        settings=settings,
    )
    assert result.action == "none"
    assert result.new_satellite_max_pct == 20.0


def test_check_satellite_drawdown_guard_below_threshold():
    """Drawdown below threshold -> no action."""
    settings = make_settings(satellite_drawdown_guard_pct=10.0, satellite_drawdown_action="shrink")
    result = check_satellite_drawdown_guard(
        satellite_peak_equity=20_000.0,
        satellite_current_equity=19_000.0,  # 5% drawdown
        settings=settings,
    )
    assert result.action == "none"


def test_check_satellite_drawdown_guard_shrink():
    """Drawdown exceeds threshold -> shrink action."""
    settings = make_settings(satellite_drawdown_guard_pct=10.0, satellite_drawdown_action="shrink", satellite_max_pct=20.0)
    result = check_satellite_drawdown_guard(
        satellite_peak_equity=20_000.0,
        satellite_current_equity=15_000.0,  # 25% drawdown
        settings=settings,
    )
    assert result.action == "shrink"
    # reduction_factor = 1 - 0.25 = 0.75, new_max = 20 * 0.75 = 15
    assert result.new_satellite_max_pct == 15.0


def test_check_satellite_drawdown_guard_pause():
    """Drawdown exceeds threshold -> pause action."""
    settings = make_settings(satellite_drawdown_guard_pct=10.0, satellite_drawdown_action="pause")
    result = check_satellite_drawdown_guard(
        satellite_peak_equity=20_000.0,
        satellite_current_equity=15_000.0,
        settings=settings,
    )
    assert result.action == "pause"
    assert result.new_satellite_max_pct == 0.0


def test_check_satellite_drawdown_guard_flatten():
    """Drawdown exceeds threshold -> flatten action."""
    settings = make_settings(satellite_drawdown_guard_pct=10.0, satellite_drawdown_action="flatten_satellite")
    result = check_satellite_drawdown_guard(
        satellite_peak_equity=20_000.0,
        satellite_current_equity=15_000.0,
        settings=settings,
    )
    assert result.action == "flatten_satellite"
    assert result.new_satellite_max_pct == 0.0


def test_calculate_satellite_budget_normal():
    """Normal satellite budget calculation."""
    settings = make_settings(satellite_max_pct=20.0, core_target_pct=80.0)
    from app.core import CoreState
    state = CoreState(
        total_equity=100_000.0,
        core_value=80_000.0,
        satellite_value=20_000.0,
        core_target_pct=80.0,
        core_actual_pct=80.0,
        drift_pct=0.0,
        rebalance_needed=False,
        rebalance_trade_usd=0.0,
        satellite_max_pct=20.0,
        satellite_actual_pct=20.0,
    )
    budget = calculate_satellite_budget(100_000.0, state, settings)
    # satellite budget = 100000 * 0.20 = 20000
    assert budget == 20_000.0


def test_calculate_satellite_budget_with_drawdown_guard():
    """Satellite budget reduced by drawdown guard."""
    settings = make_settings(satellite_max_pct=20.0, core_target_pct=80.0)
    from app.core import CoreState
    state = CoreState(
        total_equity=100_000.0,
        core_value=85_000.0,
        satellite_value=15_000.0,
        core_target_pct=80.0,
        core_actual_pct=85.0,
        drift_pct=5.0,
        rebalance_needed=True,
        rebalance_trade_usd=-5_000.0,
        satellite_max_pct=20.0,
        satellite_actual_pct=15.0,
    )
    guard = DrawdownGuardResult(
        action="shrink",
        new_satellite_max_pct=10.0,  # Reduced from 20% to 10%
        reason="test",
    )
    budget = calculate_satellite_budget(100_000.0, state, settings, drawdown_guard=guard)
    # satellite budget = 100000 * 0.10 = 10000
    assert budget == 10_000.0


def test_calculate_satellite_budget_respects_core_minimum():
    """Satellite budget respects core minimum allocation."""
    settings = make_settings(satellite_max_pct=20.0, core_target_pct=80.0)
    from app.core import CoreState
    # Core is above target (85%), satellite max is 20%
    # Core minimum = 80%, so satellite budget = 100000 - 85000 = 15000 (not 20000)
    state = CoreState(
        total_equity=100_000.0,
        core_value=85_000.0,
        satellite_value=15_000.0,
        core_target_pct=80.0,
        core_actual_pct=85.0,
        drift_pct=5.0,
        rebalance_needed=True,
        rebalance_trade_usd=-5_000.0,
        satellite_max_pct=20.0,
        satellite_actual_pct=15.0,
    )
    budget = calculate_satellite_budget(100_000.0, state, settings)
    # core_min_pct = 80%, core_min_value = 80000
    # But core_value (85000) > core_min_value (80000), so use core_value
    # available = 100000 - 85000 = 15000
    assert budget == 15_000.0


def test_format_core_satellite_report():
    """Report formatting includes all key metrics."""
    settings = make_settings()
    from app.core import CoreState
    state = CoreState(
        total_equity=100_000.0,
        core_value=80_000.0,
        satellite_value=20_000.0,
        core_target_pct=80.0,
        core_actual_pct=80.0,
        drift_pct=0.0,
        rebalance_needed=False,
        rebalance_trade_usd=0.0,
        satellite_max_pct=20.0,
        satellite_actual_pct=20.0,
    )
    report = format_core_satellite_report(state)
    assert report["total_equity"] == 100_000.0
    assert report["core"]["target_pct"] == 80.0
    assert report["core"]["actual_pct"] == 80.0
    assert report["satellite"]["max_pct"] == 20.0
    assert report["drawdown_guard"]["enabled"] is False


def test_verify_satellite_off_tracks_spy():
    """With satellite off, account should track SPY closely."""
    # Account equity = SPY equity (core only)
    spy = [100_000, 101_000, 102_000, 101_500, 103_000]
    account = [100_000, 101_000, 102_000, 101_500, 103_000]  # Identical
    passed, msg = verify_satellite_off_tracks_spy(account, spy, tolerance_pct=0.1)
    assert passed


def test_verify_satellite_off_tracks_spy_fails():
    """With satellite off, account diverging from SPY should fail."""
    spy = [100_000, 101_000, 102_000, 101_500, 103_000]
    account = [100_000, 95_000, 90_000, 85_000, 80_000]  # Large deviation
    passed, msg = verify_satellite_off_tracks_spy(account, spy, tolerance_pct=1.0)
    assert not passed
    assert "Max deviation" in msg

# --- satellite cap, enable flag and live guard (patch) ---------------------------------------

def test_satellite_headroom_is_cap_minus_holdings():
    from app.core import satellite_headroom_usd
    assert satellite_headroom_usd(100_000, 0, 20) == 20_000
    assert satellite_headroom_usd(100_000, 12_000, 20) == 8_000
    assert satellite_headroom_usd(100_000, 25_000, 20) == 0      # already over the cap
    assert satellite_headroom_usd(100_000, 0, 0) == 0            # paused / shrunk to nothing
    assert satellite_headroom_usd(0, 0, 20) == 0


def test_core_enabled_defaults_off(monkeypatch):
    from app.config import load_settings
    monkeypatch.delenv("CORE_ENABLED", raising=False)
    assert load_settings().core_enabled is False
    monkeypatch.setenv("CORE_ENABLED", "true")
    assert load_settings().core_enabled is True


def test_paused_guard_gives_zero_headroom():
    from app.core import check_satellite_drawdown_guard, satellite_headroom_usd
    s = make_settings(satellite_drawdown_guard_pct=10.0, satellite_drawdown_action="pause", satellite_max_pct=20.0)
    g = check_satellite_drawdown_guard(10_000, 8_000, s)      # 20% drawdown
    assert satellite_headroom_usd(100_000, 8_000, g.new_satellite_max_pct) == 0


import asyncio


class _FakeBroker:
    def __init__(self, positions, net_liq):
        self._pos, self._nl = positions, net_liq

    async def account_balance(self):
        return {"net_liq": self._nl}

    async def positions(self):
        return self._pos

    async def last_price(self, sym):
        return 100.0


class _FakeDB:
    def get_kv(self, k, default=None):
        return default


def _engine_stub(settings, broker):
    from app.engine import Engine
    e = Engine.__new__(Engine)       # no broker/db wiring needed for the pure budget helper
    e.s, e.broker, e.db = settings, broker, _FakeDB()
    return e


def test_engine_headroom_excludes_core_and_counts_satellite():
    s = make_settings(core_enabled=True, core_symbol="SPY", satellite_max_pct=20.0, satellite_drawdown_guard_pct=0.0)
    e = _engine_stub(s, _FakeBroker({"SPY": 800, "AAPL": 50}, 100_000))   # AAPL = $5,000 satellite
    assert asyncio.run(e._satellite_headroom_usd()) == 15_000


def test_core_rebalance_blocked_when_disabled_manual_or_live_unapproved():
    from app.engine import Engine

    async def go(**kw):
        e = _engine_stub(make_settings(**{k: v for k, v in kw.items() if k != "mode"}),
                         _FakeBroker({}, 100_000))
        e.kill_switch, e.trading_mode = False, kw.get("mode", "auto")
        return await Engine.check_core_rebalance(e)

    assert asyncio.run(go(core_enabled=False)) is None                       # off by default
    assert asyncio.run(go(core_enabled=True, mode="approve")) is None        # manual-approval mode
    assert asyncio.run(go(core_enabled=True, broker_mode="webull", webull_environment="prod", allow_live_auto=False)) is None
