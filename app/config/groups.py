"""Grouped, read-only typed views over the flat Settings.

``Settings`` stays flat (``s.max_order_usd``) and is the source of truth, so existing code,
``dataclasses.replace(settings, ...)`` and test monkeypatching keep working. ``s.risk``,
``s.options``, ``s.ml``, ``s.sleeves``, ``s.news``, ``s.broker``, ``s.orders``, ``s.feed``,
``s.alerts``, ``s.backtest``, ``s.llm`` and ``s.core`` return frozen dataclasses, built on each
access (no cache, so they can never go stale), with field types taken from Settings.
Every Settings field belongs to exactly one group; ``core`` holds the leftovers (keys,
watchlist, paths, shadows, misc) and is computed, so a new Settings field lands there until
it is added to a group in GROUP_FIELDS.

Migration plan (needs the user's go-ahead for stages 2 and 3):
  Stage 1 (done): typed, exhaustive, non-overlapping groups + tests/test_settings_groups.py.
  Stage 2: migrate call sites module by module from ``s.max_order_usd`` to
    ``s.risk.max_order_usd`` (one module per commit, tests green each time). ``replace()`` keeps
    using flat names until stage 3.
  Stage 3: make the groups the stored state (Settings composed of group dataclasses), provide
    a ``replace_group``/nested-replace helper, update loader, tests and replace() callers,
    then drop the flat duplicates and the flat-name properties.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, make_dataclass
from typing import Any

GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "RiskSettings": (
        "max_order_usd", "max_position_usd", "max_order_qty", "max_trades_per_day",
        "max_daily_loss_usd", "price_band_pct", "proposal_ttl_minutes", "max_risk_per_trade_usd",
        "sl_atr_mult", "tp_atr_mult", "min_sl_pct", "max_sl_pct", "bracket_orders",
        "kill_max_drawdown_pct", "kill_daily_loss_usd", "kill_max_consecutive_errors",
        "kill_action", "flatten_slippage_pct", "max_sector_exposure_pct", "max_correlation",
        "max_adds_per_symbol", "block_add_to_loser", "equity_quote_max_age_seconds",
        "risk_loop_seconds", "symbol_whitelist", "db_retention_days",
    ),
    "OptionsSettings": (
        "options_enabled", "options_max_contracts_per_order", "options_max_notional_usd",
        "options_allowed_strategies", "options_require_approval", "options_min_dte",
        "options_max_dte", "options_allowed_underlyings", "options_iv_rank_max",
        "options_iv_rank_min", "options_delta_min", "options_delta_max", "options_min_volume",
        "options_min_open_interest", "options_max_bid_ask_pct", "options_exit_stop_pct",
        "options_exit_take_profit_pct", "options_exit_dte", "options_expiry_close_time",
        "options_quote_max_age_seconds", "options_iv_rank_min_history",
        "options_auto_entry", "options_entry_target_delta", "options_entry_target_dte",
        "options_entry_max_premium_usd", "options_entry_max_open", "options_entry_max_per_day",
        "options_entry_earnings_days", "options_entry_skip_open_close_minutes",
        "options_ml_enabled", "options_ml_explore_pct", "options_ml_min_p",
        "options_ml_retrain_days", "options_ml_live_weight",
        "options_exit_underlying_stop", "options_spread_log", "options_spread_log_times",
    ),
    "MLSettings": (
        "ml_enabled", "ml_min_p", "ml_min_auc", "ml_history_bars", "ml_min_bars",
        "ml_retrain_days", "ml_first", "ml_first_ta_floor", "ml_first_top_n", "ml_size_scaling",
        "ml_size_min_factor", "ml_size_full_p", "ml_auto_promote",
        "ml_challenger_min_trades", "ml_challenger_min_hit_rate_improvement",
        "ml_challenger_min_return_improvement", "ml_challenger_max_days",
    ),
    "FeedSettings": (
        "realtime_source", "poll_seconds", "feed_ring_size", "feed_max_subscriptions",
        "feed_options_top_k", "stream_heartbeat_seconds",
    ),
    "AlertSettings": (
        "alert_levels", "alert_dedupe_minutes", "alert_max_per_hour", "alert_daily_summary",
        "alert_ntfy_server", "alert_ntfy_topic", "alert_telegram_token",
        "alert_telegram_chat_id", "alert_webhook_url", "alert_smtp_host", "alert_smtp_port",
        "alert_smtp_user", "alert_smtp_password", "alert_email_from", "alert_email_to",
    ),
    "BacktestSettings": (
        "backtest_data_dir", "backtest_results_dir", "backtest_initial_cash", "backtest_universe",
        "backtest_benchmark_symbol", "backtest_min_bars", "backtest_commission_per_order",
        "backtest_slippage_bps", "backtest_atr_slippage_mult", "backtest_impact_bps",
        "backtest_max_impact_bps", "backtest_max_gross_exposure_pct", "backtest_max_hold_days",
        "backtest_exit_on_trend_break", "backtest_ml_enabled", "backtest_fetch_count",
        "backtest_fetch_delay_seconds",
    ),
    "LLMSettings": (
        "llm_model", "groq_fallback_model", "nvidia_nim_model", "llm_min_cycle_minutes",
        "llm_timeout_seconds", "llm_deadline_seconds", "max_agent_iterations",
        "agent_result_chars", "agent_old_result_chars", "sizing_mode", "headline_max_chars",
        "yfinance_timeout_seconds", "yfinance_negative_ttl_seconds",
        "yfinance_breaker_failures", "yfinance_breaker_cooldown_seconds",
    ),
    "SleevesSettings": (
        "sleeves_enabled", "sleeve_start_capital", "sleeve_pause_dd_pct",
        "sleeve_momentum_enabled", "sleeve_putwrite_enabled", "sleeve_vaa_enabled",
        "sleeve_momentum_n", "sleeve_putwrite_underlying", "sleeve_putwrite_delta",
        "sleeve_putwrite_dte", "sleeve_min_trade_usd", "sleeve_rebalance_after_et",
        "shadows_enabled",
    ),
    "NewsSettings": (
        "news_lookback_hours", "news_refresh_minutes", "news_concurrency", "news_max_per_source",
        "news_half_life_hours", "news_relevance_filter", "news_google_concurrency",
        "news_rss_spacing_seconds", "news_google_cooldown_minutes", "news_google_spacing_seconds",
        "news_google_refresh_minutes", "news_outage_minutes", "news_nasdaq_enabled",
        "news_nasdaq_rss_url", "news_seekingalpha_enabled", "news_seekingalpha_rss_url",
        "news_bing_enabled", "news_bing_rss_url", "news_webull_enabled", "news_llm_batch_size",
        "news_llm_max_batches", "news_llm_cooldown_minutes",
    ),
    "BrokerSettings": (
        "broker_mode", "webull_environment", "webull_region", "webull_account_id",
        "broker_execution", "broker_max_concurrency", "broker_rate_limit_retries",
        "broker_call_timeout_seconds", "broker_backoff_seconds", "broker_down_alert_minutes",
    ),
    "OrderSettings": (
        "entry_order_type", "exit_order_type", "market_notional_buffer_pct",
        "bracket_take_profit", "fractional_enabled", "fractional_min_usd", "fractional_decimals",
        "fractional_whitelist", "market_hours_only",
    ),
}


def _core_names() -> tuple[str, ...]:
    """Every Settings field not claimed by another group (secrets, watchlist, paths, shadows...)."""
    from .settings import Settings
    claimed = {n for names in GROUP_FIELDS.values() for n in names}
    return tuple(f.name for f in fields(Settings) if f.name not in claimed)


def _hints() -> dict[str, Any]:
    from typing import get_type_hints

    from .settings import Settings
    try:
        return get_type_hints(Settings)
    except Exception:  # unresolvable annotation: fall back to untyped
        return {}


def _from_settings(cls, s: Any):
    return cls(**{f.name: getattr(s, f.name) for f in fields(cls)})


def _make(name: str, names: tuple[str, ...]):
    hints = _hints()
    cls = make_dataclass(name, [(n, hints.get(n, Any)) for n in names], frozen=True,
                         namespace={"from_settings": classmethod(_from_settings)})
    cls.__module__ = __name__
    cls.__doc__ = f"Read-only typed view of the {name[:-8].lower()} settings."
    return cls


RiskSettings = _make("RiskSettings", GROUP_FIELDS["RiskSettings"])
OptionsSettings = _make("OptionsSettings", GROUP_FIELDS["OptionsSettings"])
MLSettings = _make("MLSettings", GROUP_FIELDS["MLSettings"])
FeedSettings = _make("FeedSettings", GROUP_FIELDS["FeedSettings"])
AlertSettings = _make("AlertSettings", GROUP_FIELDS["AlertSettings"])
BacktestSettings = _make("BacktestSettings", GROUP_FIELDS["BacktestSettings"])
LLMSettings = _make("LLMSettings", GROUP_FIELDS["LLMSettings"])
SleevesSettings = _make("SleevesSettings", GROUP_FIELDS["SleevesSettings"])
NewsSettings = _make("NewsSettings", GROUP_FIELDS["NewsSettings"])
BrokerSettings = _make("BrokerSettings", GROUP_FIELDS["BrokerSettings"])
OrderSettings = _make("OrderSettings", GROUP_FIELDS["OrderSettings"])
GROUP_FIELDS["CoreSettings"] = _core_names()
CoreSettings = _make("CoreSettings", GROUP_FIELDS["CoreSettings"])
