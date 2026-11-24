"""Grouped, read-only views over the flat Settings.

``Settings`` stays flat (``s.max_order_usd``) so existing code and
``dataclasses.replace(settings, ...)`` keep working. ``s.risk``, ``s.options``,
``s.ml``, ``s.feed``, ``s.alerts``, ``s.backtest`` and ``s.llm`` return these frozen
sub-models, each holding a copy of the relevant fields under the same names.
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
        "feed_options_top_k", "stream_heartbeat_seconds", "broker_max_concurrency",
        "broker_rate_limit_retries", "broker_backoff_seconds", "broker_execution",
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
}


def _from_settings(cls, s: Any):
    return cls(**{f.name: getattr(s, f.name) for f in fields(cls)})


def _make(name: str, names: tuple[str, ...]):
    cls = make_dataclass(name, [(n, Any) for n in names], frozen=True,
                         namespace={"from_settings": classmethod(_from_settings)})
    cls.__module__ = __name__
    cls.__doc__ = f"Read-only view of the {name[:-8].lower()} settings."
    return cls


RiskSettings = _make("RiskSettings", GROUP_FIELDS["RiskSettings"])
OptionsSettings = _make("OptionsSettings", GROUP_FIELDS["OptionsSettings"])
MLSettings = _make("MLSettings", GROUP_FIELDS["MLSettings"])
FeedSettings = _make("FeedSettings", GROUP_FIELDS["FeedSettings"])
AlertSettings = _make("AlertSettings", GROUP_FIELDS["AlertSettings"])
BacktestSettings = _make("BacktestSettings", GROUP_FIELDS["BacktestSettings"])
LLMSettings = _make("LLMSettings", GROUP_FIELDS["LLMSettings"])
