"""Column definitions of the ML trade dataset (documented in data/ml_dataset/SCHEMA.md)."""
from __future__ import annotations

import pandas as pd

HOLDOUT_START = "2025-11-24"                # everything dated after 2025-11-23 is the locked holdout
HOLDOUT_LAST_BACKTEST_ENTRY = "2025-11-21"  # backtest entries must not be later than this

BASE_COLS = [
    "source", "strategy", "variant", "symbol", "asset", "signal_date", "entry_date", "entry_price",
    "exit_date", "exit_price", "exit_reason", "hold_days", "ret", "r_multiple", "mfe", "mae", "stop",
    "ta_score", "ml_p", "consensus", "holdout", "config_hash",
]
_FEATS = ["sma20", "sma50", "ema20", "rsi14", "adx14", "atr14", "atr_pct", "atr_pct_rank", "rv20", "pct_vs_sma20",
          "pct_vs_sma50", "sma20_slope_pct", "high10", "low10", "pct_from_high10", "chg_5d_pct", "chg_20d_pct",
          "days_below_sma20", "vol_vs_20d_avg", "sma200", "pct_vs_sma200", "ret_63d", "ret_126d", "ret_252d",
          "ret_12_1", "high252", "vol60", "vol60_rank", "rs_63d", "rs_126d", "pct_from_high252"]
FEATURE_NAMES = ["close"] + _FEATS
FEATURE_COLS = ["f_" + k for k in FEATURE_NAMES]
SPY_NAMES = ["close", "trend", "adx14", "pct_vs_sma200", "pct_vs_sma50", "rsi14", "chg_20d_pct"]
SPY_COLS = ["spy_" + k for k in SPY_NAMES]
EXTRA_COLS = [
    "opt_strike", "opt_iv", "opt_prem0", "opt_stock_ret", "opt_stock_reason",
    "intr_gap_pct", "intr_vs_open_bps", "intr_fill_slot", "intr_tags", "intr_r_struct", "intr_ret_struct",
    "live_quantity", "live_proposal_id", "live_broker_mode",
]
ALL_COLS = BASE_COLS + EXTRA_COLS + FEATURE_COLS + SPY_COLS


def is_holdout(entry_date, exit_date=None) -> int:
    """1 when the entry OR exit is dated on/after HOLDOUT_START (inside the locked holdout)."""
    for v in (entry_date, exit_date):
        s = "" if v is None else str(v)
        if s in ("", "NaT", "nan", "None"):
            continue
        if s[:10] >= HOLDOUT_START:
            return 1
    return 0
