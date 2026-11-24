"""Tests for universe management (Phase 3)."""

from __future__ import annotations

from app.universe import (
    SECTOR_ETFS,
    BROAD_ETFS,
    get_predefined_universe,
    build_universe_config,
    filter_universe,
    SymbolInfo,
    UniverseConfig,
)


def test_predefined_universes():
    """Predefined universes return expected symbols."""
    sector = get_predefined_universe("sector_etfs")
    assert "SPY" in sector
    assert "QQQ" in sector
    assert "XLF" in sector
    assert len(sector) == 15  # SPY, QQQ, DIA, IWM + 11 sector ETFs

    broad = get_predefined_universe("broad_etfs")
    assert "SPY" in broad
    assert "QQQ" in broad
    assert "EFA" in broad
    assert len(broad) == 23

    # sp500_top returns the static list of 50 symbols
    sp500 = get_predefined_universe("sp500_top")
    assert len(sp500) == 50
    assert "AAPL" in sp500
    assert "MSFT" in sp500
    assert "NVDA" in sp500

    # Unknown falls back to sector ETFs
    unknown = get_predefined_universe("unknown")
    assert "SPY" in unknown


def test_build_universe_config():
    """UniverseConfig is built from Settings correctly."""
    from app.config import Settings

    # Create minimal settings
    settings = Settings(
        groq_api_key="test",
        llm_model="test",
        groq_fallback_model="test",
        nvidia_nim_api_key="test",
        nvidia_nim_model="test",
        broker_mode="mock",
        webull_environment="uat",
        webull_region="us",
        webull_account_id=None,
        trading_mode="approve",
        allow_live_auto=False,
        watchlist=["AAPL"],
        cycle_minutes=30,
        auto_start_scheduler=False,
        market_hours_only=True,
        overnight_trading=False,
        overnight_symbols=[],
        max_order_usd=500,
        max_position_usd=2000,
        max_order_qty=50,
        max_trades_per_day=3,
        max_daily_loss_usd=200,
        price_band_pct=1.0,
        proposal_ttl_minutes=30,
        max_agent_iterations=20,
        research_scan_minutes=60,
        auto_trade_research_candidates=False,
        realtime_source="auto",
        poll_seconds=1.0,
        review_every_days=7,
        min_outcomes_for_review=10,
        consensus_ta_min=0.15,
        consensus_sent_min=-0.25,
        consensus_min_news=3,
        ml_enabled=True,
        ml_min_p=0.52,
        ml_history_bars=1000,
        ml_min_bars=500,
        ml_retrain_days=7,
        sl_atr_mult=2.0,
        tp_atr_mult=3.0,
        min_sl_pct=1.0,
        max_sl_pct=8.0,
        max_risk_per_trade_usd=100.0,
        bracket_orders=True,
        sentiment_source="webull",
        sentiment_llm=False,
        sentiment_rss_fallback=False,
        sentiment_rss_url="https://example.invalid/{symbol}",
        news_lookback_hours=72,
        news_refresh_minutes=15,
        signal_refresh_minutes=15,
        snapshot_seconds=60,
        watchdog_seconds=30,
        broker_down_alert_minutes=5,
        token_dir="/tmp",
        token_warn_days=3.0,
        reconcile_give_up_minutes=15,
        kill_max_drawdown_pct=10.0,
        kill_daily_loss_usd=300.0,
        kill_max_consecutive_errors=3,
        kill_action="pause",
        llm_min_cycle_minutes=10,
        sizing_mode="code",
        agent_result_chars=6000,
        agent_old_result_chars=600,
        flatten_slippage_pct=0.5,
        spy_benchmark_enabled=True,
        spy_benchmark_min_outperformance_pct=0.5,
        spy_benchmark_window_days=5,
        spy_benchmark_min_trades=10,
        spy_benchmark_action="warn",
        performance_daily_enabled=False,
        performance_benchmark_symbol="SPY",
        performance_min_days=60,
        performance_history_days=400,
        performance_risk_free_rate=0.0,
        backtest_data_dir="/tmp",
        backtest_results_dir="/tmp",
        backtest_initial_cash=25000.0,
        backtest_universe=[],
        backtest_benchmark_symbol="SPY",
        backtest_min_bars=60,
        backtest_commission_per_order=1.0,
        backtest_slippage_bps=5.0,
        backtest_atr_slippage_mult=0.03,
        backtest_impact_bps=5.0,
        backtest_max_impact_bps=100.0,
        backtest_max_gross_exposure_pct=100.0,
        backtest_max_hold_days=20,
        backtest_exit_on_trend_break=True,
        backtest_ml_enabled=False,
        backtest_fetch_count=1000,
        backtest_fetch_delay_seconds=1.0,
        universe_name="sector_etfs",
        universe_custom=[],
        universe_top_n=50,
        universe_min_price=10.0,
        universe_min_avg_dollar_volume=20_000_000.0,
        universe_min_history_days=250,
        core_symbol="SPY",
        core_target_pct=80.0,
        satellite_max_pct=20.0,
        core_rebalance_band_pct=2.0,
        core_min_trade_usd=100.0,
        core_rebalance_market_hours_only=True,
        satellite_drawdown_guard_pct=10.0,
        satellite_drawdown_action="shrink",
        eligibility_enabled=True,
        eligibility_min_price=5.0,
        eligibility_min_volume=20_000_000.0,
        eligibility_min_bars=250,
        eligibility_max_gap_pct=3.0,
        eligibility_signal_freshness_min=60,
        eligibility_earnings_blackout_days=2,
        eligibility_first_15_min=True,
        eligibility_one_starter_per_symbol=True,
        # Tiered scanning
        universe_tier1_size=50,
        universe_tier2_size=100,
        universe_tier3_size=200,
        universe_scan_all_tiers=False,
        # Diversification
        universe_max_per_sector=5,
        universe_max_correlation=0.70,
        universe_sector_map={"XLK": "Technology", "XLF": "Financials"},
        # Champion/Challenger (Phase 5)
        ml_challenger_min_trades=20,
        ml_challenger_min_hit_rate_improvement=0.02,
        ml_challenger_min_return_improvement=0.1,
        ml_challenger_max_days=14,
        alert_levels=frozenset(["error"]),
        alert_dedupe_minutes=30,
        alert_max_per_hour=30,
        alert_daily_summary=True,
        alert_ntfy_server="https://ntfy.sh",
        alert_ntfy_topic="",
        alert_telegram_token="",
        alert_telegram_chat_id="",
        alert_webhook_url="",
        alert_smtp_host="",
        alert_smtp_port=587,
        alert_smtp_user="",
        alert_smtp_password="",
        alert_email_from="",
        alert_email_to="",
        host="127.0.0.1",
        port=8000,
    )

    config = build_universe_config(settings)
    assert config.name == "sector_etfs"
    assert config.top_n == 50
    assert config.min_price == 10.0
    assert config.min_avg_dollar_volume == 20_000_000.0
    assert config.min_history_days == 250


def test_filter_universe_price():
    """Price filter drops symbols below minimum."""
    symbols = ["AAPL", "MSFT", "PENNY"]
    info = {
        "AAPL": SymbolInfo(symbol="AAPL", price=150.0, avg_dollar_volume=1_000_000_000, history_days=500),
        "MSFT": SymbolInfo(symbol="MSFT", price=300.0, avg_dollar_volume=1_000_000_000, history_days=500),
        "PENNY": SymbolInfo(symbol="PENNY", price=2.0, avg_dollar_volume=100_000, history_days=500),
    }
    config = UniverseConfig(
        name="custom",
        custom_symbols=[],
        top_n=50,
        min_price=5.0,
        min_avg_dollar_volume=100_000.0,
        min_history_days=100,
    )

    filtered = filter_universe(symbols, info, config)
    assert "AAPL" in filtered
    assert "MSFT" in filtered
    assert "PENNY" not in filtered  # below min price


def test_filter_universe_volume():
    """Volume filter drops symbols below minimum dollar volume."""
    symbols = ["LIQUID", "ILLIQUID"]
    info = {
        "LIQUID": SymbolInfo(symbol="LIQUID", price=50.0, avg_dollar_volume=100_000_000, history_days=500),
        "ILLIQUID": SymbolInfo(symbol="ILLIQUID", price=50.0, avg_dollar_volume=10_000, history_days=500),
    }
    config = UniverseConfig(
        name="custom",
        custom_symbols=[],
        top_n=50,
        min_price=5.0,
        min_avg_dollar_volume=50_000_000.0,  # $50M/day
        min_history_days=100,
    )

    filtered = filter_universe(symbols, info, config)
    assert "LIQUID" in filtered
    assert "ILLIQUID" not in filtered


def test_filter_universe_history():
    """History filter drops symbols with insufficient bars."""
    symbols = ["LONG", "SHORT"]
    info = {
        "LONG": SymbolInfo(symbol="LONG", price=50.0, avg_dollar_volume=100_000_000, history_days=500),
        "SHORT": SymbolInfo(symbol="SHORT", price=50.0, avg_dollar_volume=100_000_000, history_days=50),
    }
    config = UniverseConfig(
        name="custom",
        custom_symbols=[],
        top_n=50,
        min_price=5.0,
        min_avg_dollar_volume=10_000_000.0,
        min_history_days=250,
    )

    filtered = filter_universe(symbols, info, config)
    assert "LONG" in filtered
    assert "SHORT" not in filtered


def test_filter_universe_keeps_etfs_without_info():
    """Predefined ETFs are kept even without info (assumed liquid)."""
    symbols = ["SPY", "XLF", "UNKNOWN_STOCK"]
    info = {
        "UNKNOWN_STOCK": SymbolInfo(symbol="UNKNOWN_STOCK", price=50.0, avg_dollar_volume=100_000_000, history_days=500),
    }
    config = UniverseConfig(
        name="custom",
        custom_symbols=[],
        top_n=50,
        min_price=5.0,
        min_avg_dollar_volume=10_000_000.0,
        min_history_days=250,
    )

    filtered = filter_universe(symbols, info, config)
    assert "SPY" in filtered   # sector ETF, kept without info
    assert "XLF" in filtered   # sector ETF, kept without info
    assert "UNKNOWN_STOCK" in filtered  # has info, passes filters


def test_sector_etfs_list():
    """SECTOR_ETFS contains expected symbols."""
    assert "SPY" in SECTOR_ETFS
    assert "QQQ" in SECTOR_ETFS
    assert "XLF" in SECTOR_ETFS
    assert "XLK" in SECTOR_ETFS
    assert "XLV" in SECTOR_ETFS
    assert "XLE" in SECTOR_ETFS
    assert "XLI" in SECTOR_ETFS
    assert "XLP" in SECTOR_ETFS
    assert "XLY" in SECTOR_ETFS
    assert "XLB" in SECTOR_ETFS
    assert "XLRE" in SECTOR_ETFS
    assert "XLU" in SECTOR_ETFS
    assert "XLC" in SECTOR_ETFS
    assert len(SECTOR_ETFS) == 15


def test_broad_etfs_list():
    """BROAD_ETFS contains expected symbols."""
    assert "SPY" in BROAD_ETFS
    assert "QQQ" in BROAD_ETFS
    assert "EFA" in BROAD_ETFS
    assert "EEM" in BROAD_ETFS
    assert "VTI" in BROAD_ETFS
    assert len(BROAD_ETFS) == 23