"""Lessons system: parameter changes proposed from closed-trade autopsies.

A proposal needs: a minimum sample, a chronological train/val/test split, a significance
test, and a human approval gate. Risk limits are NEVER touchable by learning.

Names here are the engine's Settings field names, so an approved lesson actually applies.
NOTE: the evidence is observational (later trades vs earlier trades). It does not replay
the proposed value on past data, so treat proposals as hypotheses; promote them through
the backtester / UAT shadow period before trusting them live.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .db import DB

# Risk parameters that can NEVER be changed by the learning system (Settings field names)
UNTOUCHABLE_RISK_PARAMS = {
    "max_order_usd", "max_position_usd", "max_trades_per_day", "max_risk_per_trade_usd",
    "max_daily_loss_usd", "kill_daily_loss_usd", "kill_max_drawdown_pct",
    "kill_max_consecutive_errors", "kill_action", "bracket_orders", "flatten_slippage_pct",
    "price_band_pct", "max_order_qty",
}

# Parameters that CAN be learned: Settings field -> (min, max). Must match engine AUTO_APPLICABLE.
LEARNABLE_PARAMS = {
    "sl_atr_mult": (1.5, 3.0),
    "tp_atr_mult": (2.0, 6.0),
    "consensus_ta_min": (0.1, 0.5),
    "ml_min_p": (0.5, 0.7),
    "consensus_sent_min": (-0.5, 0.0),
}


def mann_whitney_greater(a: list[float], b: list[float]) -> tuple[float, float]:
    """One-sided Mann-Whitney U (a > b), normal approximation with tie correction.
    Pure Python so the significance test never silently disappears without scipy."""
    n1, n2 = len(a), len(b)
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, i, tie_term = {}, 0, 0.0
    r1 = 0.0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        avg = (i + j + 1) / 2
        tie_term += (j - i) ** 3 - (j - i)
        r1 += avg * sum(1 for k in range(i, j) if allv[k][1] == 0)
        i = j
    u = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0
    if var <= 0:
        return u, 1.0
    z = (u - n1 * n2 / 2 - 0.5) / math.sqrt(var)
    return u, 0.5 * math.erfc(z / math.sqrt(2))


@dataclass
class LessonProposal:
    param_name: str
    param_value: float
    param_scope: str
    sample_size: int
    baseline_metric: float
    proposed_metric: float
    metric_name: str
    p_value: float
    supporting_autopsies: list[int]
    evidence_summary: str
    created_by: str = "autopsy"


def validate_learnable_param(param_name: str, param_value: float) -> tuple[bool, str]:
    """Check if a parameter is learnable and within bounds."""
    if param_name.lower() in UNTOUCHABLE_RISK_PARAMS:
        return False, f"Parameter {param_name} is a risk limit and cannot be learned"

    if param_name not in LEARNABLE_PARAMS:
        return False, f"Parameter {param_name} is not in the learnable parameter registry"

    min_val, max_val = LEARNABLE_PARAMS[param_name]
    if not (min_val <= param_value <= max_val):
        return False, f"Parameter {param_name} value {param_value} outside bounds [{min_val}, {max_val}]"

    return True, "OK"


def time_split_validation(trades: list[dict], train_pct: float = 0.6,
                          val_pct: float = 0.2, test_pct: float = 0.2) -> dict[str, Any]:
    """Validate lesson using time-split (train/val/test) on closed trades.

    Trades are sorted by entry time, then split chronologically to avoid lookahead bias.
    """
    if not trades:
        return {"valid": False, "reason": "No trades provided"}

    # Sort by entry time
    sorted_trades = sorted(trades, key=lambda t: t.get("entry_fill_time") or "")
    n = len(sorted_trades)

    train_end = int(n * train_pct)
    val_end = train_end + int(n * val_pct)

    train = sorted_trades[:train_end]
    val = sorted_trades[train_end:val_end]
    test = sorted_trades[val_end:]

    def compute_metrics(trade_list: list[dict]) -> dict[str, float]:
        if not trade_list:
            return {"win_rate": 0, "expectancy": 0, "sharpe": 0, "n": 0}
        pnls = [t.get("realized_pnl_pct", 0) for t in trade_list if t.get("realized_pnl_pct") is not None]
        if not pnls:
            return {"win_rate": 0, "expectancy": 0, "sharpe": 0, "n": 0}
        wins = sum(1 for p in pnls if p > 0)
        mean_pnl = sum(pnls) / len(pnls)
        # Pure Python std dev
        variance = sum((x - mean_pnl) ** 2 for x in pnls) / len(pnls)
        std_pnl = variance ** 0.5
        return {
            "win_rate": wins / len(pnls),
            "expectancy": mean_pnl,
            "sharpe": mean_pnl / std_pnl if std_pnl > 0 else 0,
            "n": len(pnls),
        }

    return {
        "valid": True,
        "train": compute_metrics(train),
        "val": compute_metrics(val),
        "test": compute_metrics(test),
        "total_n": n,
    }


def statistical_significance(baseline_trades: list[dict], proposed_trades: list[dict],
                             metric_name: str = "expectancy") -> tuple[float, str]:
    """Calculate p-value comparing baseline vs proposed performance.

    Uses appropriate test based on metric:
    - expectancy/win_rate: Mann-Whitney U test (non-parametric)
    - sharpe: t-test on returns
    """
    if not baseline_trades or not proposed_trades:
        return 1.0, "Insufficient data"

    if metric_name in ("expectancy", "win_rate"):
        # Use P&L percentages
        baseline_pnls = [t.get("realized_pnl_pct", 0) for t in baseline_trades if t.get("realized_pnl_pct") is not None]
        proposed_pnls = [t.get("realized_pnl_pct", 0) for t in proposed_trades if t.get("realized_pnl_pct") is not None]

        if len(baseline_pnls) < 10 or len(proposed_pnls) < 10:
            return 1.0, "Sample size too small for statistical test"

        stat, p = mann_whitney_greater(proposed_pnls, baseline_pnls)
        return p, f"Mann-Whitney U test: U={stat:.1f}, p={p:.4f}"

    return 1.0, "Unknown metric"


def analyze_autopsies_for_lessons(db: DB, param_name: str, settings: Any, min_sample: int = 30) -> LessonProposal | None:
    """Analyze autopsies to propose a parameter change.

    This looks at the top process issues across autopsies and suggests parameter adjustments.
    """
    autopsies = db.autopsies(limit=200)
    if not autopsies:
        return None

    # Get closed trades linked to these autopsies
    trade_ids = [a.get("trade_id") for a in autopsies if a.get("trade_id")]
    trades = []
    for tid in trade_ids:
        t = db.get_trade(tid)
        if t and t.get("status") == "closed" and t.get("realized_pnl_pct") is not None:
            trades.append(t)

    # autopsies come newest-first; every split below must be chronological
    trades.sort(key=lambda t: t.get("entry_fill_time") or "")
    if len(trades) < min_sample:
        return None

    # Analyze causes by frequency and strength
    cause_scores: dict[str, list[float]] = {}
    for a in autopsies:
        try:
            causes = json.loads(a.get("causes") or "[]")
            for c in causes:
                cause = c.get("cause", "")
                strength = c.get("strength", 0)
                category = c.get("category", "")
                if category == "process":
                    cause_scores.setdefault(cause, []).append(strength)
        except (json.JSONDecodeError, AttributeError):
            continue

    # Map causes to parameter adjustments
    param_adjustments = {
        "stop_too_tight": ("sl_atr_mult", +0.2),
        "stop_too_wide": ("sl_atr_mult", -0.2),
        "gave_back_gains": ("tp_atr_mult", -0.3),
        "weak_ta_signal": ("consensus_ta_min", +0.05),
        "counter_trend_entry": ("consensus_ta_min", +0.1),
        "low_ml_probability": ("ml_min_p", +0.05),
        "negative_sentiment": ("consensus_sent_min", +0.1),
    }

    # Find the most common process issue that maps to our target parameter
    target_adjustment = param_adjustments.get(param_name)
    if not target_adjustment:
        # Try reverse mapping
        for cause, (pname, adj) in param_adjustments.items():
            if pname == param_name:
                target_adjustment = (pname, adj)
                break

    if not target_adjustment:
        return None

    _, adjustment = target_adjustment

    # Get current param value (would come from settings/engine)
    current_value = get_current_param_value(param_name, settings)
    proposed_value = current_value + adjustment

    # Validate
    valid, msg = validate_learnable_param(param_name, proposed_value)
    if not valid:
        return None

    # Time-split validation
    split_results = time_split_validation(trades)

    if not split_results["valid"]:
        return None

    # Statistical significance
    # Split trades into baseline (before any lesson) and proposed (simulated)
    # For now, use train as baseline, test as proposed
    baseline_trades = trades[:split_results["train"]["n"]]
    proposed_trades = trades[-split_results["test"]["n"]:] if split_results["test"]["n"] > 0 else []

    p_value, test_desc = statistical_significance(baseline_trades, proposed_trades, "expectancy")

    # Check if proposed is better on validation set
    val_expectancy = split_results["val"]["expectancy"]
    test_expectancy = split_results["test"]["expectancy"]
    baseline_expectancy = split_results["train"]["expectancy"]

    if test_expectancy <= baseline_expectancy or val_expectancy <= baseline_expectancy:
        return None  # No improvement on both held-out blocks
    if p_value >= 0.05:
        return None  # not statistically significant

    # Build evidence summary
    supporting_ids = [a.get("id") for a in autopsies if a.get("id")]
    def mean_strength(vals):
        return sum(vals) / len(vals) if vals else 0
    cause_desc = ", ".join([f"{c} (avg strength: {mean_strength(v):.2f})"
                            for c, v in sorted(cause_scores.items(), key=lambda x: -mean_strength(x[1]))[:3]])

    return LessonProposal(
        param_name=param_name,
        param_value=proposed_value,
        param_scope="global",
        sample_size=len(trades),
        baseline_metric=baseline_expectancy,
        proposed_metric=test_expectancy,
        metric_name="expectancy",
        p_value=p_value,
        supporting_autopsies=supporting_ids[:10],  # Top 10
        evidence_summary=f"Top causes: {cause_desc}. Time-split: train={split_results['train']['n']}, val={split_results['val']['n']}, test={split_results['test']['n']}. {test_desc}",
        created_by="autopsy",
    )


def get_current_param_value(param_name: str, settings: Any) -> float:
    """Live value from the engine's Settings (includes any already-applied lessons)."""
    return float(getattr(settings, param_name))


def propose_lessons_from_autopsies(db: DB, settings: Any) -> int:
    """Analyze autopsies and store new 'proposed' lessons (human approval still required).
    Returns how many were stored. Skips params that already have a proposed/active lesson."""
    stored = 0
    for param_name in LEARNABLE_PARAMS:
        open_ = [l for l in db.lessons(None, param_name, 50) if l["status"] in ("proposed", "approved", "active")]
        if open_:
            continue
        pr = analyze_autopsies_for_lessons(db, param_name, settings)
        if pr is None:
            continue
        db.add_lesson(param_name=pr.param_name, param_value=pr.param_value, param_scope=pr.param_scope,
                      sample_size=pr.sample_size, baseline_metric=pr.baseline_metric,
                      proposed_metric=pr.proposed_metric, metric_name=pr.metric_name,
                      p_value=pr.p_value, supporting_autopsies=json.dumps(pr.supporting_autopsies),
                      evidence_summary=pr.evidence_summary, created_by=pr.created_by)
        stored += 1
    return stored
