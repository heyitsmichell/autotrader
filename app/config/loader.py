"""load_settings(): read the environment into a Settings."""

from __future__ import annotations

import os
from pathlib import Path

from .env import (DATA_DIR, ROOT, _bool, _build_watchlist, _float, _int, _raw, _reg, _sector_map,
                  _str, _symbols)
from .settings import Settings


def _broker_execution() -> str:
    mode = _str("BROKER_EXECUTION", "mcp").lower()
    if mode not in {"mcp", "sdk"}:
        raise ValueError("BROKER_EXECUTION must be 'mcp' or 'sdk'")
    return mode


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    v = _str(name, default).strip().lower()
    if v not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}")
    return v


def _trusted_hosts(raw: str) -> tuple[str, ...]:
    """TRUSTED_HOSTS: comma-separated host names (no ports, no wildcards). Names that look like a
    catch-all ("*", "0.0.0.0", "::") are refused, since this app has no login."""
    out = []
    for h in (x.strip().lower().rstrip(".") for x in raw.split(",")):
        if not h:
            continue
        if h in ("*", "0.0.0.0", "::", "[::]") or "*" in h or "/" in h or ":" in h:
            raise ValueError(f"TRUSTED_HOSTS: refusing {h!r} (give exact host names, no ports or wildcards)")
        out.append(h)
    return tuple(out)


def load_settings() -> Settings:
    broker_mode = _str("BROKER_MODE", "mock").lower()
    if broker_mode not in {"mock", "webull"}:
        raise ValueError("BROKER_MODE must be 'mock' or 'webull'")
    trading_mode = _str("TRADING_MODE", "approve").lower()
    if trading_mode not in {"approve", "auto"}:
        raise ValueError("TRADING_MODE must be 'approve' or 'auto'")
    environment = _str("WEBULL_ENVIRONMENT", "uat").lower()
    if environment not in {"uat", "prod"}:
        raise ValueError("WEBULL_ENVIRONMENT must be 'uat' or 'prod'")
    kill_action = _str("KILL_ACTION", "pause").lower()
    if kill_action not in {"pause", "cancel_entries", "flatten"}:
        raise ValueError("KILL_ACTION must be 'pause', 'cancel_entries' or 'flatten'")
    sat_action = _str("SATELLITE_DRAWDOWN_ACTION", "shrink").lower()
    if sat_action not in {"shrink", "pause", "flatten_satellite"}:
        raise ValueError("SATELLITE_DRAWDOWN_ACTION must be 'shrink', 'pause' or 'flatten_satellite'")
    token_dir = Path(_str("WEBULL_TOKEN_DIR", "./conf"))
    if not token_dir.is_absolute():
        token_dir = (ROOT / token_dir).resolve()
    def _dir(name: str, default: Path) -> Path:
        _reg(name, str(default))
        raw = Path(os.environ.get(name, "").strip() or default)
        return raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()
    sentiment_source = _str("SENTIMENT_SOURCE", "webull").lower()
    if sentiment_source not in {"webull", "llm", "blend"}:
        raise ValueError("SENTIMENT_SOURCE must be 'webull', 'llm' or 'blend'")

    return Settings(
        groq_api_key=_raw("GROQ_API_KEY").strip(),
        llm_model=_str("LLM_MODEL", "openai/gpt-oss-120b"),
        groq_fallback_model=_str("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b"),
        nvidia_nim_api_key=_raw("NVIDIA_NIM_API_KEY").strip(),
        nvidia_nim_model=_str("NVIDIA_NIM_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b"),
        broker_mode=broker_mode,
        webull_environment=environment,
        webull_region=_str("WEBULL_REGION_ID", "us").lower(),
        webull_account_id=_raw("WEBULL_ACCOUNT_ID").strip() or None,
        trading_mode=trading_mode,
        allow_live_auto=_bool("ALLOW_LIVE_AUTO_TRADING", False),
        watchlist=_build_watchlist(),
        cycle_minutes=max(1, _int("CYCLE_MINUTES", 30)),
        auto_start_scheduler=_bool("AUTO_START_SCHEDULER", False),
        market_hours_only=_bool("MARKET_HOURS_ONLY", True),
        overnight_trading=_bool("OVERNIGHT_TRADING", False),
        overnight_symbols=_symbols("OVERNIGHT_SYMBOLS", ""),
        max_order_usd=_float("MAX_ORDER_USD", _float("MAX_ORDER_NOTIONAL", 500.0)),
        max_position_usd=_float("MAX_POSITION_USD", 2000.0),
        max_order_qty=_int("MAX_ORDER_QTY", 50),
        max_trades_per_day=_int("MAX_TRADES_PER_DAY", 3),
        max_daily_loss_usd=_float("MAX_DAILY_LOSS_USD", 200.0),
        price_band_pct=_float("PRICE_BAND_PCT", 1.0),
        proposal_ttl_minutes=_int("PROPOSAL_TTL_MINUTES", 30),
        max_agent_iterations=_int("MAX_AGENT_ITERATIONS", 20),
        research_scan_minutes=max(0, _int("RESEARCH_SCAN_MINUTES", 60)),
        auto_trade_research_candidates=_bool("AUTO_TRADE_RESEARCH_CANDIDATES", False),
        realtime_source=_str("REALTIME_SOURCE", "auto").lower(),
        poll_seconds=max(0.5, _float("POLL_SECONDS", 1.0 if broker_mode == "mock" else 2.0)),
        review_every_days=max(0, _int("REVIEW_EVERY_DAYS", 7)),
        min_outcomes_for_review=max(1, _int("MIN_OUTCOMES_FOR_REVIEW", 10)),
        consensus_ta_min=_float("CONSENSUS_TA_MIN", 0.15),
        consensus_sent_min=_float("CONSENSUS_SENT_MIN", -0.25),
        consensus_min_news=max(0, _int("CONSENSUS_MIN_NEWS", 3)),
        ml_enabled=_bool("ML_ENABLED", True),
        ml_min_p=_float("ML_MIN_P", 0.52),
        ml_min_auc=_float("ML_MIN_AUC", 0.51),
        ml_history_bars=min(1200, max(200, _int("ML_HISTORY_BARS", 1000))),
        ml_min_bars=max(100, _int("ML_MIN_BARS", 500)),
        ml_retrain_days=max(0, _int("ML_RETRAIN_DAYS", 7)),
        sl_atr_mult=max(0.5, _float("SL_ATR_MULT", 2.0)),
        tp_atr_mult=max(0.5, _float("TP_ATR_MULT", 3.0)),
        min_sl_pct=max(0.1, _float("MIN_SL_PCT", 1.0)),
        max_sl_pct=max(0.5, _float("MAX_SL_PCT", 8.0)),
        max_risk_per_trade_usd=_float("MAX_RISK_PER_TRADE_USD", 100.0),
        bracket_orders=_bool("BRACKET_ORDERS", True),
        sentiment_source=sentiment_source,
        sentiment_llm=_bool("SENTIMENT_LLM", False),
        sentiment_rss_fallback=_bool("SENTIMENT_RSS_FALLBACK", False),
        sentiment_rss_url=_str(
            "SENTIMENT_RSS_URL",
            "https://news.google.com/rss/search?q={symbol}&hl=en-US&gl=US&ceid=US:en",   # news-pipeline: US edition; "SYM" stock is built in code
        ),
        news_lookback_hours=max(6, _int("NEWS_LOOKBACK_HOURS", 72)),
        news_refresh_minutes=max(0, _int("NEWS_REFRESH_MINUTES", 15)),
        signal_refresh_minutes=max(0, _int("SIGNAL_REFRESH_MINUTES", 15)),
        snapshot_seconds=max(15, _int("SNAPSHOT_SECONDS", 60)),
        watchdog_seconds=max(10, _int("WATCHDOG_SECONDS", 30)),
        broker_down_alert_minutes=max(1, _int("BROKER_DOWN_ALERT_MINUTES", 5)),
        token_dir=token_dir,
        token_warn_days=max(0.5, _float("TOKEN_WARN_DAYS", 3.0)),
        reconcile_give_up_minutes=max(5, _int("RECONCILE_GIVE_UP_MINUTES", 15)),
        kill_max_drawdown_pct=max(0.0, _float("KILL_MAX_DRAWDOWN_PCT", 10.0)),
        kill_daily_loss_usd=max(0.0, _float("KILL_DAILY_LOSS_USD", 300.0)),
        kill_max_consecutive_errors=max(0, _int("KILL_MAX_CONSECUTIVE_ERRORS", 3)),
        kill_action=kill_action,
        llm_min_cycle_minutes=max(1, _int("LLM_MIN_CYCLE_MINUTES", 10)),
        sizing_mode=("llm" if _str("SIZING_MODE", "code").lower() == "llm" else "code"),
        agent_result_chars=max(1000, _int("AGENT_RESULT_CHARS", 6000)),
        agent_old_result_chars=max(200, _int("AGENT_OLD_RESULT_CHARS", 600)),
        flatten_slippage_pct=min(5.0, max(0.05, _float("FLATTEN_SLIPPAGE_PCT", 0.5))),
        # SPY Benchmark settings
        spy_benchmark_enabled=_bool("SPY_BENCHMARK_ENABLED", True),
        spy_benchmark_min_outperformance_pct=_float("SPY_BENCHMARK_MIN_OUTPERFORMANCE_PCT", 0.5),
        spy_benchmark_window_days=_int("SPY_BENCHMARK_WINDOW_DAYS", 5),
        spy_benchmark_min_trades=_int("SPY_BENCHMARK_MIN_TRADES", 10),
        spy_benchmark_action=_str("SPY_BENCHMARK_ACTION", "warn").lower(),  # warn, pause, kill
        # Daily performance measurement (Phase 0). Read-only: stores no orders.
        # Off by default; .env.example turns it on explicitly.
        performance_daily_enabled=_bool("PERFORMANCE_DAILY_ENABLED", False),
        performance_benchmark_symbol=_str("PERFORMANCE_BENCHMARK_SYMBOL", "SPY").upper(),
        performance_min_days=max(2, _int("PERFORMANCE_MIN_DAYS", 60)),
        performance_history_days=min(2000, max(60, _int("PERFORMANCE_HISTORY_DAYS", 400))),
        performance_risk_free_rate=max(0.0, _float("PERFORMANCE_RISK_FREE_RATE", 0.0)),
        # Backtester (Phase 1)
        backtest_data_dir=_dir("BACKTEST_DATA_DIR", DATA_DIR / "bars"),
        backtest_results_dir=_dir("BACKTEST_RESULTS_DIR", DATA_DIR / "backtests"),
        backtest_initial_cash=max(1.0, _float("BACKTEST_INITIAL_CASH", 25_000.0)),
        backtest_universe=_symbols("BACKTEST_UNIVERSE", ""),
        backtest_benchmark_symbol=_str("BACKTEST_BENCHMARK_SYMBOL", "SPY").upper(),
        backtest_min_bars=min(500, max(20, _int("BACKTEST_MIN_BARS", 60))),
        backtest_commission_per_order=max(0.0, _float("BACKTEST_COMMISSION_PER_ORDER", 1.0)),
        backtest_slippage_bps=max(0.0, _float("BACKTEST_SLIPPAGE_BPS", 5.0)),
        backtest_atr_slippage_mult=max(0.0, _float("BACKTEST_ATR_SLIPPAGE_MULT", 0.03)),
        backtest_impact_bps=max(0.0, _float("BACKTEST_IMPACT_BPS", 5.0)),
        backtest_max_impact_bps=max(0.0, _float("BACKTEST_MAX_IMPACT_BPS", 100.0)),
        backtest_max_gross_exposure_pct=min(100.0, max(1.0, _float("BACKTEST_MAX_GROSS_EXPOSURE_PCT", 100.0))),
        backtest_max_hold_days=max(0, _int("BACKTEST_MAX_HOLD_DAYS", 20)),
        backtest_exit_on_trend_break=_bool("BACKTEST_EXIT_ON_TREND_BREAK", True),
        backtest_ml_enabled=_bool("BACKTEST_ML_ENABLED", True),
        backtest_fetch_count=min(5000, max(60, _int("BACKTEST_FETCH_COUNT", 1000))),
        backtest_fetch_delay_seconds=max(0.0, _float("BACKTEST_FETCH_DELAY_SECONDS", 1.0)),
        # Universe (Phase 3)
        universe_name=_str("UNIVERSE_NAME", "sector_etfs").lower(),
        universe_custom=_symbols("UNIVERSE_CUSTOM", ""),
        universe_top_n=max(5, min(500, _int("UNIVERSE_TOP_N", 50))),
        universe_min_price=max(1.0, _float("UNIVERSE_MIN_PRICE", 10.0)),
        universe_min_avg_dollar_volume=max(100_000.0, _float("UNIVERSE_MIN_AVG_DOLLAR_VOLUME", 20_000_000.0)),
        universe_min_history_days=max(60, _int("UNIVERSE_MIN_HISTORY_DAYS", 250)),
        # Tiered scanning (Task 3)
        universe_tier1_size=max(10, min(100, _int("UNIVERSE_TIER1_SIZE", 50))),
        universe_tier2_size=max(0, min(200, _int("UNIVERSE_TIER2_SIZE", 100))),
        universe_tier3_size=max(0, min(500, _int("UNIVERSE_TIER3_SIZE", 200))),
        universe_scan_all_tiers=_bool("UNIVERSE_SCAN_ALL_TIERS", False),
        # Diversification limits (Task 3)
        universe_max_per_sector=max(0, _int("UNIVERSE_MAX_PER_SECTOR", 5)),
        universe_max_correlation=max(0.0, _float("UNIVERSE_MAX_CORRELATION", 0.7)),
        universe_sector_map=_sector_map("UNIVERSE_SECTOR_MAP", ""),  # comma-separated SYMBOL:SECTOR pairs
        # Core-Satellite (Phase 4)
        core_enabled=_bool("CORE_ENABLED", False),
        ml_first=_bool("ML_FIRST", True),
        ml_first_ta_floor=max(-1.0, min(1.0, _float("ML_FIRST_TA_FLOOR", 0.0))),
        ml_first_top_n=max(1, _int("ML_FIRST_TOP_N", 3)),
        ml_size_scaling=_bool("ML_SIZE_SCALING", True),
        ml_size_min_factor=max(0.05, min(1.0, _float("ML_SIZE_MIN_FACTOR", 0.4))),
        ml_size_full_p=max(0.5, min(0.99, _float("ML_SIZE_FULL_P", 0.62))),
        ml_auto_promote=_bool("ML_AUTO_PROMOTE", True),
        cost_edge_mult=max(0.0, _float("COST_EDGE_MULT", 3.0)),
        exit_max_hold_days=max(0, _int("EXIT_MAX_HOLD_DAYS", 20)),
        exit_confirm_readings=max(1, _int("EXIT_CONFIRM_READINGS", 3)),
        exit_confirm_minutes=max(0.0, _float("EXIT_CONFIRM_MINUTES", 10.0)),
        exit_ta_floor=max(-1.0, min(1.0, _float("EXIT_TA_FLOOR", -0.10))),
        exit_ml_margin=max(0.0, min(0.5, _float("EXIT_ML_MARGIN", 0.05))),
        fast_loop_enabled=_bool("FAST_LOOP_ENABLED", False),
        fast_loop_seconds=max(5.0, _float("FAST_LOOP_SECONDS", 20.0)),
        fast_loop_arm_margin=max(0.0, min(1.0, _float("FAST_LOOP_ARM_MARGIN", 0.15))),
        fast_loop_max_armed=max(1, min(30, _int("FAST_LOOP_MAX_ARMED", 10))),
        fast_loop_cooldown_minutes=max(1, _int("FAST_LOOP_COOLDOWN_MINUTES", 30)),
        broker_max_concurrency=max(1, min(16, _int("BROKER_MAX_CONCURRENCY", 4))),
        broker_rate_limit_retries=max(0, min(10, _int("BROKER_RATE_LIMIT_RETRIES", 4))),
        broker_backoff_seconds=max(0.1, _float("BROKER_BACKOFF_SECONDS", 2.0)),
        core_symbol=_str("CORE_SYMBOL", "SPY").upper(),
        core_target_pct=min(95.0, max(50.0, _float("CORE_TARGET_PCT", 80.0))),
        satellite_max_pct=min(50.0, max(0.0, _float("SATELLITE_MAX_PCT", 20.0))),
        core_rebalance_band_pct=max(0.1, min(10.0, _float("CORE_REBALANCE_BAND_PCT", 2.0))),
        core_min_trade_usd=max(1.0, _float("CORE_MIN_TRADE_USD", 100.0)),
        core_rebalance_market_hours_only=_bool("CORE_REBALANCE_MARKET_HOURS_ONLY", True),
        satellite_drawdown_guard_pct=max(0.0, _float("SATELLITE_DRAWDOWN_GUARD_PCT", 10.0)),
        satellite_drawdown_action=_str("SATELLITE_DRAWDOWN_ACTION", "shrink").lower(),
        # Strategy improvements (Task 2)
        regime_aware_entries=_bool("REGIME_AWARE_ENTRIES", False),
        regime_ta_min_up=max(-1.0, min(1.0, _float("REGIME_TA_MIN_UP", 0.10))),
        regime_ta_min_range=max(-1.0, min(1.0, _float("REGIME_TA_MIN_RANGE", 0.20))),
        regime_ta_min_down=max(-1.0, min(1.0, _float("REGIME_TA_MIN_DOWN", 0.30))),
        relative_strength_enabled=_bool("RELATIVE_STRENGTH_ENABLED", False),
        relative_strength_lookback=max(5, _int("RELATIVE_STRENGTH_LOOKBACK", 20)),
        relative_strength_min=max(-1.0, min(1.0, _float("RELATIVE_STRENGTH_MIN", 0.0))),
        volatility_scaling_enabled=_bool("VOL_SCALING_ENABLED", False),
        volatility_scaling_target_atr_pct=max(0.1, _float("VOL_TARGET_ATR_PCT", 1.5)),
        volatility_scaling_max_factor=max(0.1, _float("VOL_MAX_FACTOR", 2.0)),
        market_regime_filter_enabled=_bool("MARKET_REGIME_FILTER", False),
        market_regime_symbol=_str("MARKET_REGIME_SYMBOL", "SPY").upper(),
        market_regime_min_trend=_str("MARKET_REGIME_MIN_TREND", "up").lower(),
        market_regime_adx_min=max(0.0, _float("MARKET_REGIME_ADX_MIN", 20.0)),
        trailing_stop_enabled=_bool("TRAILING_STOP_ENABLED", False),
        trailing_stop_activation_r=max(0.1, _float("TRAILING_ACTIVATION_R", 1.0)),
        trailing_stop_atr_mult=max(0.1, _float("TRAILING_ATR_MULT", 1.5)),
        sentiment_yahoo_rss=_bool("SENTIMENT_YAHOO_RSS", True),
        sentiment_yahoo_rss_url=_str("SENTIMENT_YAHOO_RSS_URL", ""),
        sentiment_rss_query_suffix=_raw("SENTIMENT_RSS_QUERY_SUFFIX", " stock"),
        bars_concurrency=max(1, min(16, _int("SIGNAL_BARS_CONCURRENCY", 6))),
        news_concurrency=max(1, min(16, _int("NEWS_CONCURRENCY", 4))),
        news_max_per_source=max(1, min(50, _int("NEWS_MAX_PER_SOURCE", 10))),
        profit_scale_out_enabled=_bool("PROFIT_SCALE_OUT", True),
        profit_scale_out_r=max(0.1, _float("PROFIT_SCALE_OUT_R", 1.0)),
        profit_scale_out_fraction=max(0.05, min(1.0, _float("PROFIT_SCALE_OUT_FRACTION", 0.5))),
        profit_scale_out_second_r=max(0.0, _float("PROFIT_SCALE_OUT_SECOND_R", 2.0)),
        profit_scale_out_second_fraction=max(0.05, min(1.0, _float("PROFIT_SCALE_OUT_SECOND_FRACTION", 0.5))),
        profit_breakeven_after_r=max(0.0, _float("PROFIT_BREAKEVEN_R", 0.75)),
        profit_giveback_pct=max(0.0, min(100.0, _float("PROFIT_GIVEBACK_PCT", 35.0))),
        profit_giveback_min_gain_pct=max(0.0, _float("PROFIT_GIVEBACK_MIN_GAIN_PCT", 2.0)),
        profit_rsi_exhaustion=max(0.0, min(100.0, _float("PROFIT_RSI_EXHAUSTION", 78.0))),
        exit_on_trend_break_enabled=_bool("EXIT_ON_TREND_BREAK", True),
        exit_ml_drop_enabled=_bool("EXIT_ML_DROP", True),
        exit_ml_drop_margin=max(0.0, min(0.5, _float("EXIT_ML_MARGIN", 0.10))),
        # Champion/Challenger ML (Phase 5)
        ml_challenger_min_trades=_int("ML_CHALLENGER_MIN_TRADES", 20),
        ml_challenger_min_hit_rate_improvement=_float("ML_CHALLENGER_MIN_HIT_RATE_IMPROVEMENT", 0.02),
        ml_challenger_min_return_improvement=_float("ML_CHALLENGER_MIN_RETURN_IMPROVEMENT", 0.1),
        ml_challenger_max_days=_int("ML_CHALLENGER_MAX_DAYS", 14),
        # Entry eligibility settings
        eligibility_enabled=_bool("ELIGIBILITY_ENABLED", True),
        eligibility_min_price=_float("ELIGIBILITY_MIN_PRICE", 5.0),
        eligibility_min_volume=_float("ELIGIBILITY_MIN_VOLUME", 20_000_000.0),
        eligibility_min_bars=_int("ELIGIBILITY_MIN_BARS", 250),
        eligibility_max_gap_pct=_float("ELIGIBILITY_MAX_GAP_PCT", 3.0),
        eligibility_signal_freshness_min=_int("ELIGIBILITY_SIGNAL_FRESHNESS_MIN", 60),
        eligibility_earnings_blackout_days=_int("ELIGIBILITY_EARNINGS_BLACKOUT_DAYS", 3),
        eligibility_first_15_min=_bool("ELIGIBILITY_FIRST_15_MIN", True),
        eligibility_one_starter_per_symbol=_bool("ELIGIBILITY_ONE_STARTER_PER_SYMBOL", True),

        # Fractional shares (Phase A)
        fractional_enabled=_bool("FRACTIONAL_ENABLED", False),
        fractional_min_usd=_float("FRACTIONAL_MIN_USD", 1.0),
        fractional_decimals=_int("FRACTIONAL_DECIMALS", 4),
        fractional_whitelist=_symbols("FRACTIONAL_WHITELIST", "AAPL,MSFT,NVDA,SPY,QQQ,AMZN,GOOGL,META,TSLA"),

        # Diversification limits (Task 3)
        max_sector_exposure_pct=max(0.0, min(100.0, _float("MAX_SECTOR_EXPOSURE_PCT", 30.0))),
        max_correlation=max(0.0, min(1.0, _float("MAX_CORRELATION", 0.70))),
        sector_map=_str("SECTOR_MAP", "XLK:Technology,XLF:Financials,XLV:Healthcare,XLE:Energy,XLI:Industrials,XLP:Staples,XLY:Discretionary,XLB:Materials,XLRE:RealEstate,XLU:Utilities,XLC:Communication,SPY:Broad,QQQ:Tech,VTI:Broad,IEFA:Intl,VWO:EM"),

        # Options trading (Phase B)
        options_enabled=_bool("OPTIONS_ENABLED", False),
        options_max_contracts_per_order=_int("OPTIONS_MAX_CONTRACTS_PER_ORDER", 10),
        options_max_notional_usd=_float("OPTIONS_MAX_NOTIONAL_USD", 5000.0),
        options_allowed_strategies=_str("OPTIONS_ALLOWED_STRATEGIES", "SINGLE,VERTICAL,STRADDLE,STRANGLE").split(","),
        options_require_approval=_bool("OPTIONS_REQUIRE_APPROVAL", True),
        options_min_dte=_int("OPTIONS_MIN_DTE", 7),
        options_max_dte=_int("OPTIONS_MAX_DTE", 45),
        options_allowed_underlyings=_symbols("OPTIONS_ALLOWED_UNDERLYINGS", "AAPL,MSFT,NVDA,SPY,QQQ,AMZN,GOOGL,META,TSLA"),
        # Option consensus gate
        options_iv_rank_max=_float("OPTIONS_IV_RANK_MAX", 0.85),
        options_iv_rank_min=_float("OPTIONS_IV_RANK_MIN", 0.10),
        options_delta_min=_float("OPTIONS_DELTA_MIN", 0.20),
        options_delta_max=_float("OPTIONS_DELTA_MAX", 0.80),
        options_min_volume=_int("OPTIONS_MIN_VOLUME", 100),
        options_min_open_interest=_int("OPTIONS_MIN_OPEN_INTEREST", 500),
        options_max_bid_ask_pct=_float("OPTIONS_MAX_BID_ASK_PCT", 5.0),

        alert_levels=frozenset(x.strip().lower() for x in _str("ALERT_LEVELS", "error").split(",")
                               if x.strip()),
        alert_dedupe_minutes=max(0, _int("ALERT_DEDUPE_MINUTES", 30)),
        alert_max_per_hour=max(1, _int("ALERT_MAX_PER_HOUR", 30)),
        alert_daily_summary=_bool("ALERT_DAILY_SUMMARY", True),
        alert_ntfy_server=_str("ALERT_NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        alert_ntfy_topic=_str("ALERT_NTFY_TOPIC", ""),
        alert_telegram_token=_str("ALERT_TELEGRAM_BOT_TOKEN", ""),
        alert_telegram_chat_id=_str("ALERT_TELEGRAM_CHAT_ID", ""),
        alert_webhook_url=_str("ALERT_WEBHOOK_URL", ""),
        alert_smtp_host=_str("ALERT_SMTP_HOST", ""),
        alert_smtp_port=_int("ALERT_SMTP_PORT", 587),
        alert_smtp_user=_str("ALERT_SMTP_USER", ""),
        alert_smtp_password=_str("ALERT_SMTP_PASSWORD", ""),
        alert_email_from=_str("ALERT_EMAIL_FROM", ""),
        alert_email_to=_str("ALERT_EMAIL_TO", ""),
        # --- Wave 1 additions (docs/ARCHITECTURE_CONTRACTS.md section 2) ---
        options_exit_stop_pct=max(0.0, _float("OPTIONS_EXIT_STOP_PCT", 0.50)),
        options_exit_take_profit_pct=max(0.0, _float("OPTIONS_EXIT_TAKE_PROFIT_PCT", 0.50)),
        options_exit_dte=max(0, _int("OPTIONS_EXIT_DTE", 2)),
        options_expiry_close_time=_str("OPTIONS_EXPIRY_CLOSE_TIME", "15:45"),
        risk_loop_seconds=max(0.25, _float("RISK_LOOP_SECONDS", 1.0)),
        options_quote_max_age_seconds=max(0.0, _float("OPTIONS_QUOTE_MAX_AGE_SECONDS", 5.0)),
        equity_quote_max_age_seconds=max(0.0, _float("EQUITY_QUOTE_MAX_AGE_SECONDS", 15.0)),
        options_iv_rank_min_history=max(1, _int("OPTIONS_IV_RANK_MIN_HISTORY", 60)),
        entry_pullback_gate=_bool("ENTRY_PULLBACK_GATE", False),
        entry_pullback_min_pct=max(0.0, _float("ENTRY_PULLBACK_MIN_PCT", 0.02)),
        entry_pullback_max_pct=max(0.0, _float("ENTRY_PULLBACK_MAX_PCT", 0.05)),
        max_adds_per_symbol=max(0, _int("MAX_ADDS_PER_SYMBOL", 1)),
        block_add_to_loser=_bool("BLOCK_ADD_TO_LOSER", True),
        eligibility_earnings_fail_closed=_bool("ELIGIBILITY_EARNINGS_FAIL_CLOSED", True),
        db_retention_days=max(1, _int("DB_RETENTION_DAYS", 90)),
        broker_execution=_broker_execution(),
        symbol_whitelist=_symbols("WEBULL_SYMBOL_WHITELIST", ""),
        llm_timeout_seconds=max(1.0, _float("LLM_TIMEOUT_SECONDS", 25.0)),
        llm_deadline_seconds=max(1.0, _float("LLM_DEADLINE_SECONDS", 90.0)),
        yfinance_timeout_seconds=max(1.0, _float("YFINANCE_TIMEOUT_SECONDS", 8.0)),
        yfinance_negative_ttl_seconds=max(0.0, _float("YFINANCE_NEGATIVE_TTL_SECONDS", 120.0)),
        yfinance_breaker_failures=max(1, _int("YFINANCE_BREAKER_FAILURES", 5)),
        yfinance_breaker_cooldown_seconds=max(1.0, _float("YFINANCE_BREAKER_COOLDOWN_SECONDS", 300.0)),
        headline_max_chars=max(20, _int("HEADLINE_MAX_CHARS", 300)),
        feed_ring_size=max(10, _int("FEED_RING_SIZE", 2000)),
        feed_max_subscriptions=max(1, _int("FEED_MAX_SUBSCRIPTIONS", 100)),
        feed_options_top_k=max(0, _int("FEED_OPTIONS_TOP_K", 4)),
        api_token_file=_dir("API_TOKEN_FILE", DATA_DIR / "api_token"),
        stream_heartbeat_seconds=max(1.0, _float("STREAM_HEARTBEAT_SECONDS", 15.0)),
        host=_str("HOST", "127.0.0.1"),
        port=_int("PORT", 8000),
        extended_hours_soft_stop=_bool("EXTENDED_HOURS_SOFT_STOP", True),  # ext-hours-protection
        # news-pipeline
        news_half_life_hours=max(1.0, _float("NEWS_HALF_LIFE_HOURS", 24.0)),
        news_relevance_filter=_bool("NEWS_RELEVANCE_FILTER", True),
        news_google_concurrency=max(1, min(4, _int("NEWS_GOOGLE_CONCURRENCY", 1))),
        news_rss_spacing_seconds=max(0.0, _float("NEWS_RSS_SPACING_SECONDS", 0.7)),
        news_google_cooldown_minutes=max(1.0, _float("NEWS_GOOGLE_COOLDOWN_MINUTES", 20.0)),
        news_webull_enabled=_bool("NEWS_WEBULL_ENABLED", True),
        news_llm_batch_size=max(2, min(25, _int("NEWS_LLM_BATCH_SIZE", 10))),
        news_llm_max_batches=max(1, min(30, _int("NEWS_LLM_MAX_BATCHES", 6))),
        news_llm_cooldown_minutes=max(1.0, _float("NEWS_LLM_COOLDOWN_MINUTES", 10.0)),
        event_veto_enabled=_bool("EVENT_VETO_ENABLED", True),
        event_veto_hours=max(1.0, _float("EVENT_VETO_HOURS", 48.0)),
        earnings_cache_hours=max(0.1, _float("EARNINGS_CACHE_HOURS", 12.0)),
        # market-orders
        exit_order_type=_choice("EXIT_ORDER_TYPE", "limit", ("limit", "market")),
        entry_order_type=_choice("ENTRY_ORDER_TYPE", "limit", ("limit", "marketable")),
        marketable_offset_bps=min(200.0, max(0.0, _float("MARKETABLE_OFFSET_BPS", 10.0))),
        entry_session_all_day=_bool("ENTRY_SESSION_ALL_DAY", True),
        overnight_size_factor=min(1.0, max(0.0, _float("OVERNIGHT_SIZE_FACTOR", 0.5))),
        max_spread_pct_extended=max(0.0, _float("MAX_SPREAD_PCT_EXTENDED", 1.0)),
        entry_chase_seconds=max(0.0, _float("ENTRY_CHASE_SECONDS", 0.0)),
        market_notional_buffer_pct=min(10.0, max(0.0, _float("MARKET_NOTIONAL_BUFFER_PCT", 1.0))),
        # swing-strategy
        bracket_take_profit=_bool("BRACKET_TAKE_PROFIT", True),
        trusted_hosts=_trusted_hosts(_str("TRUSTED_HOSTS", "")),
        size_from_risk=_bool("SIZE_FROM_RISK", False),
        risk_per_trade_pct=min(10.0, max(0.0, _float("RISK_PER_TRADE_PCT", 0.0))),
        backtest_live_exits=_bool("BACKTEST_LIVE_EXITS", False),
    )
