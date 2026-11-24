"""Config package: back-compat names, grouped views, contract section 2 fields, and
gen_env_example."""
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

import app.config as config
from app.config import Settings, load_settings
from app.config.groups import GROUP_FIELDS

REPO = Path(__file__).resolve().parent.parent


def test_root_points_at_repo_root():
    assert config.ROOT == REPO
    assert (config.ROOT / "app" / "config").is_dir()
    assert config.STRATEGY_FILE == REPO / "strategy.md"


@pytest.mark.parametrize("name", [
    "Settings", "load_settings", "ROOT", "DATA_DIR", "LOG_DIR", "MODEL_DIR", "WEB_DIR",
    "STRATEGY_FILE", "LESSONS_FILE", "estimate_cost", "PRICING", "_bool", "_int", "_float",
    "_str", "_symbols", "_sector_map", "_build_watchlist"])
def test_legacy_names_still_exported(name):
    assert hasattr(config, name)


def test_flat_attributes_and_replace(test_settings):
    s = test_settings
    assert s.max_order_usd == 1000
    s2 = dataclasses.replace(s, max_order_usd=5.0)
    assert s2.max_order_usd == 5.0 and s2.risk.max_order_usd == 5.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.max_order_usd = 1.0            # type: ignore[misc]


def test_every_group_field_exists_on_settings():
    names = {f.name for f in dataclasses.fields(Settings)}
    for cls_name, fields in GROUP_FIELDS.items():
        assert set(fields) <= names, (cls_name, set(fields) - names)


def test_group_views(test_settings):
    s = test_settings
    assert s.risk.max_position_usd == s.max_position_usd
    assert s.options.options_enabled is False
    assert s.ml.ml_min_p == s.ml_min_p
    assert s.feed.feed_ring_size == 2000
    assert s.alerts.alert_dedupe_minutes == s.alert_dedupe_minutes
    assert s.backtest.backtest_ml_enabled is True
    assert s.llm.llm_deadline_seconds == 90.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.risk.max_order_usd = 0         # type: ignore[misc]


def test_contract_defaults(monkeypatch):
    for k in ("ELIGIBILITY_EARNINGS_BLACKOUT_DAYS", "BACKTEST_ML_ENABLED", "OPTIONS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert s.options_exit_stop_pct == 0.50 and s.options_exit_take_profit_pct == 0.50
    assert s.options_exit_dte == 2 and s.options_expiry_close_time == "15:45"
    assert s.risk_loop_seconds == 1.0
    assert s.options_quote_max_age_seconds == 5.0 and s.equity_quote_max_age_seconds == 15.0
    assert s.options_iv_rank_min_history == 60
    assert s.entry_pullback_gate is False
    assert (s.entry_pullback_min_pct, s.entry_pullback_max_pct) == (0.02, 0.05)
    assert s.max_adds_per_symbol == 1 and s.block_add_to_loser is True
    assert s.eligibility_earnings_blackout_days == 3
    assert s.eligibility_earnings_fail_closed is True
    assert s.backtest_ml_enabled is True
    assert s.db_retention_days == 90
    assert s.broker_execution == "mcp"
    assert s.symbol_whitelist == []
    assert (s.llm_timeout_seconds, s.llm_deadline_seconds) == (25.0, 90.0)
    assert (s.yfinance_timeout_seconds, s.yfinance_negative_ttl_seconds) == (8.0, 120.0)
    assert (s.yfinance_breaker_failures, s.yfinance_breaker_cooldown_seconds) == (5, 300.0)
    assert s.headline_max_chars == 300
    assert (s.feed_ring_size, s.feed_max_subscriptions, s.feed_options_top_k) == (2000, 100, 4)
    assert s.api_token_file == config.DATA_DIR / "api_token"
    assert s.stream_heartbeat_seconds == 15.0
    # safe defaults untouched
    assert s.options_enabled is False and s.trading_mode == "approve"
    assert s.broker_mode == "mock" and s.allow_live_auto is False


def test_env_overrides_and_validation(monkeypatch):
    monkeypatch.setenv("RISK_LOOP_SECONDS", "0.01")
    monkeypatch.setenv("WEBULL_SYMBOL_WHITELIST", "aapl, msft")
    monkeypatch.setenv("MAX_ORDER_NOTIONAL", "250")
    monkeypatch.delenv("MAX_ORDER_USD", raising=False)
    s = load_settings()
    assert s.risk_loop_seconds == 0.25               # clamped to the minimum
    assert s.symbol_whitelist == ["AAPL", "MSFT"]
    assert s.max_order_usd == 250 == s.max_order_notional
    monkeypatch.setenv("BROKER_EXECUTION", "bogus")
    with pytest.raises(ValueError):
        load_settings()


def _gen(*args):
    return subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "gen_env_example.py"), *args],
                          capture_output=True, text=True, cwd=REPO)


def test_env_example_is_in_sync():
    r = _gen("--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_gen_env_example_check_detects_drift():
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import gen_env_example as g
    finally:
        sys.path.pop(0)
    current = g.TARGET.read_text(encoding="utf-8")
    from app.config import REGISTRY
    load_settings()
    assert g.generate(current, dict(REGISTRY), g._comments()) == current          # stable
    stripped = "\n".join(l for l in current.splitlines() if not l.startswith("RISK_LOOP_SECONDS="))
    assert g.generate(stripped, dict(REGISTRY), g._comments()) != stripped         # drift found
    assert "SIGNAL_REFRESH_MINUTES=5" in current
    secret_lines = [l for l in current.splitlines()
                    if l.split("=")[0] in {"GROQ_API_KEY", "NVIDIA_NIM_API_KEY", "WEBULL_APP_SECRET"}]
    assert secret_lines and all(l.endswith("=") for l in secret_lines)
