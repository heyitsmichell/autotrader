"""Core-Satellite portfolio management (Phase 4).

The core holds a target percentage of equity in a broad market ETF (default SPY).
The satellite is the active strategy, bounded by configured limits.

Rebalancing:
- Only during market hours (configurable)
- Only when core drifts beyond rebalance band (+/-2% of equity by default)
- Only above minimum trade size ($100 by default)
- Uses the same risk-checked execution path (Engine.execute / RiskManager.check)
- Selling core to fund satellite buys goes through normal checks

Drawdown guard:
- When satellite drawdown exceeds threshold, can shrink/pause/flatten satellite
- Never sells core automatically (except via kill-switch flatten)
- Satellite sizing receives satellite budget, not total equity
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

# Market hours (ET)
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class CoreState:
    """Current state of core and satellite allocations."""
    total_equity: float
    core_value: float
    satellite_value: float
    core_target_pct: float
    core_actual_pct: float
    drift_pct: float  # core_actual_pct - core_target_pct
    rebalance_needed: bool
    rebalance_trade_usd: float  # positive = buy core, negative = sell core
    satellite_max_pct: float
    satellite_actual_pct: float


@dataclass
class RebalancePlan:
    """Plan for core rebalancing."""
    action: str  # "buy_core" | "sell_core" | "none"
    trade_usd: float
    shares: int
    reason: str


@dataclass
class DrawdownGuardResult:
    """Result of satellite drawdown guard check."""
    action: str  # "none" | "shrink" | "pause" | "flatten_satellite"
    new_satellite_max_pct: float
    reason: str


def is_market_hours(now: date, tz_aware_now=None) -> bool:
    """Check if current time is within market hours (ET).

    Note: This is a simplified check. In production, use a proper timezone-aware check
    with the NYSE calendar (app/market_calendar.py).
    """
    if tz_aware_now is not None:
        from datetime import datetime
        et_time = tz_aware_now.astimezone().time()
        return MARKET_OPEN <= et_time <= MARKET_CLOSE
    # Fallback: assume market hours if not provided
    return True


def calculate_core_state(
    total_equity: float,
    core_position_qty: int,
    core_price: float,
    satellite_positions_value: float,
    settings: Settings,
) -> CoreState:
    """Calculate current core/satellite state and whether rebalancing is needed.

    Args:
        total_equity: Total account equity (net liquidation)
        core_position_qty: Number of shares of CORE_SYMBOL held
        core_price: Current price of CORE_SYMBOL
        satellite_positions_value: Market value of all satellite positions
        settings: App settings with core/satellite config

    Returns:
        CoreState with all computed values
    """
    core_value = core_position_qty * core_price
    satellite_value = satellite_positions_value
    core_target_pct = settings.core_target_pct
    satellite_max_pct = settings.satellite_max_pct

    if total_equity <= 0:
        return CoreState(
            total_equity=total_equity,
            core_value=core_value,
            satellite_value=satellite_value,
            core_target_pct=core_target_pct,
            core_actual_pct=0.0,
            drift_pct=0.0,
            rebalance_needed=False,
            rebalance_trade_usd=0.0,
            satellite_max_pct=satellite_max_pct,
            satellite_actual_pct=0.0,
        )

    core_actual_pct = (core_value / total_equity) * 100
    satellite_actual_pct = (satellite_value / total_equity) * 100
    drift_pct = core_actual_pct - core_target_pct

    # Check if rebalancing is needed (outside band)
    band = settings.core_rebalance_band_pct
    rebalance_needed = abs(drift_pct) > band

    # Calculate trade size to bring core back to target
    target_core_value = total_equity * (core_target_pct / 100.0)
    rebalance_trade_usd = target_core_value - core_value  # positive = buy core

    # Check minimum trade size
    min_trade = settings.core_min_trade_usd
    if rebalance_needed and abs(rebalance_trade_usd) < min_trade:
        rebalance_needed = False
        rebalance_trade_usd = 0.0

    return CoreState(
        total_equity=total_equity,
        core_value=core_value,
        satellite_value=satellite_value,
        core_target_pct=core_target_pct,
        core_actual_pct=core_actual_pct,
        drift_pct=drift_pct,
        rebalance_needed=rebalance_needed,
        rebalance_trade_usd=rebalance_trade_usd,
        satellite_max_pct=satellite_max_pct,
        satellite_actual_pct=satellite_actual_pct,
    )


def create_rebalance_plan(
    state: CoreState,
    core_price: float,
    settings: Settings,
    is_market_hours: bool = True,
) -> RebalancePlan:
    """Create a rebalancing plan based on current state.

    Args:
        state: Current core/satellite state
        core_price: Current price of core symbol
        settings: App settings
        is_market_hours: Whether it's currently market hours

    Returns:
        RebalancePlan with action, trade size, and reason
    """
    if not state.rebalance_needed:
        return RebalancePlan(
            action="none",
            trade_usd=0.0,
            shares=0,
            reason="Core within rebalance band",
        )

    if settings.core_rebalance_market_hours_only and not is_market_hours:
        return RebalancePlan(
            action="none",
            trade_usd=0.0,
            shares=0,
            reason="Outside market hours; rebalancing deferred",
        )

    trade_usd = state.rebalance_trade_usd
    if abs(trade_usd) < settings.core_min_trade_usd:
        return RebalancePlan(
            action="none",
            trade_usd=0.0,
            shares=0,
            reason=f"Trade size ${abs(trade_usd):.2f} below minimum ${settings.core_min_trade_usd}",
        )

    shares = int(abs(trade_usd) / core_price)
    if shares <= 0:
        return RebalancePlan(
            action="none",
            trade_usd=0.0,
            shares=0,
            reason="Calculated shares <= 0",
        )

    if trade_usd > 0:
        # Need to buy core (sell satellite positions or use cash)
        action = "buy_core"
        reason = f"Core at {state.core_actual_pct:.1f}%, target {state.core_target_pct:.1f}% (drift {state.drift_pct:+.1f}%)"
    else:
        # Need to sell core (to fund satellite or reduce drift)
        action = "sell_core"
        reason = f"Core at {state.core_actual_pct:.1f}%, target {state.core_target_pct:.1f}% (drift {state.drift_pct:+.1f}%)"

    return RebalancePlan(
        action=action,
        trade_usd=trade_usd,
        shares=shares,
        reason=reason,
    )


def check_satellite_drawdown_guard(
    satellite_peak_equity: float,
    satellite_current_equity: float,
    settings: Settings,
) -> DrawdownGuardResult:
    """Check if satellite drawdown guard should activate.

    Args:
        satellite_peak_equity: Peak equity of satellite portion
        satellite_current_equity: Current equity of satellite portion
        settings: App settings with drawdown guard config

    Returns:
        DrawdownGuardResult with action and new satellite max pct
    """
    guard_pct = settings.satellite_drawdown_guard_pct
    if guard_pct <= 0:
        return DrawdownGuardResult(
            action="none",
            new_satellite_max_pct=settings.satellite_max_pct,
            reason="Drawdown guard disabled",
        )

    if satellite_peak_equity <= 0:
        return DrawdownGuardResult(
            action="none",
            new_satellite_max_pct=settings.satellite_max_pct,
            reason="No satellite peak equity recorded",
        )

    drawdown_pct = ((satellite_peak_equity - satellite_current_equity) / satellite_peak_equity) * 100

    if drawdown_pct < guard_pct:
        return DrawdownGuardResult(
            action="none",
            new_satellite_max_pct=settings.satellite_max_pct,
            reason=f"Satellite drawdown {drawdown_pct:.1f}% < guard {guard_pct:.1f}%",
        )

    # Guard triggered
    action = settings.satellite_drawdown_action
    if action == "shrink":
        # Reduce satellite max allocation proportionally
        reduction_factor = 1.0 - (drawdown_pct / 100.0)
        new_max = max(0.0, settings.satellite_max_pct * reduction_factor)
        reason = f"Satellite drawdown {drawdown_pct:.1f}% >= guard {guard_pct:.1f}%; shrinking satellite max from {settings.satellite_max_pct:.1f}% to {new_max:.1f}%"
    elif action == "pause":
        new_max = 0.0
        reason = f"Satellite drawdown {drawdown_pct:.1f}% >= guard {guard_pct:.1f}%; pausing satellite (max_pct=0%)"
    elif action == "flatten_satellite":
        new_max = 0.0
        reason = f"Satellite drawdown {drawdown_pct:.1f}% >= guard {guard_pct:.1f}%; flattening satellite (will close all positions)"
    else:
        new_max = settings.satellite_max_pct
        reason = f"Unknown action {action}; no change"

    return DrawdownGuardResult(
        action=action,
        new_satellite_max_pct=new_max,
        reason=reason,
    )


def calculate_satellite_budget(
    total_equity: float,
    core_state: CoreState,
    settings: Settings,
    drawdown_guard: DrawdownGuardResult | None = None,
) -> float:
    """Calculate the satellite budget (max USD available for satellite positions).

    Args:
        total_equity: Total account equity
        core_state: Current core/satellite state
        settings: App settings
        drawdown_guard: Optional drawdown guard result (if activated, uses reduced max)

    Returns:
        Satellite budget in USD
    """
    # Use drawdown guard max if activated, else settings max
    if drawdown_guard and drawdown_guard.action != "none":
        max_pct = drawdown_guard.new_satellite_max_pct
    else:
        max_pct = settings.satellite_max_pct

    # Satellite budget = total_equity * (max_pct / 100)
    # But also respect that core must hold at least (100 - max_pct)%
    core_min_pct = 100.0 - max_pct
    core_min_value = total_equity * (core_min_pct / 100.0)

    available_for_satellite = total_equity - max(core_state.core_value, core_min_value)
    return max(0.0, available_for_satellite)


def satellite_headroom_usd(total_equity: float, satellite_value: float, max_pct: float) -> float:
    """USD still available for NEW satellite buys: max_pct of equity minus what the satellite
    already holds. This is the cap the engine applies to BUY sizing when CORE_ENABLED=true."""
    if total_equity <= 0 or max_pct <= 0:
        return 0.0
    return max(0.0, total_equity * max_pct / 100.0 - max(0.0, satellite_value))


def format_core_satellite_report(
    state: CoreState,
    drawdown_guard: DrawdownGuardResult | None = None,
    rebalance_plan: RebalancePlan | None = None,
) -> dict[str, Any]:
    """Format a core-satellite report for the dashboard/API.

    Returns:
        Dictionary with all key metrics
    """
    report = {
        "total_equity": round(state.total_equity, 2),
        "core": {
            "symbol": "SPY",  # Could be parameterized
            "target_pct": state.core_target_pct,
            "actual_pct": round(state.core_actual_pct, 2),
            "value": round(state.core_value, 2),
            "drift_pct": round(state.drift_pct, 2),
            "rebalance_needed": state.rebalance_needed,
        },
        "satellite": {
            "max_pct": state.satellite_max_pct,
            "actual_pct": round(state.satellite_actual_pct, 2),
            "value": round(state.satellite_value, 2),
        },
        "rebalance": {
            "band_pct": 2.0,  # from settings
            "min_trade_usd": 100.0,  # from settings
        },
    }

    if drawdown_guard:
        report["drawdown_guard"] = {
            "enabled": True,
            "trigger_pct": 10.0,  # from settings
            "action": drawdown_guard.action,
            "new_satellite_max_pct": drawdown_guard.new_satellite_max_pct,
            "reason": drawdown_guard.reason,
        }
    else:
        report["drawdown_guard"] = {"enabled": False}

    if rebalance_plan:
        report["rebalance"]["action"] = rebalance_plan.action
        report["rebalance"]["trade_usd"] = round(rebalance_plan.trade_usd, 2)
        report["rebalance"]["shares"] = rebalance_plan.shares
        report["rebalance"]["reason"] = rebalance_plan.reason

    return report


def verify_satellite_off_tracks_spy(
    account_equity_curve: list[float],
    spy_equity_curve: list[float],
    tolerance_pct: float = 1.0,
) -> tuple[bool, str]:
    """Verify that with satellite disabled, account tracks SPY closely.

    Used as a test to ensure core implementation is correct.

    Args:
        account_equity_curve: Account equity values over time
        spy_equity_curve: SPY buy-and-hold equity values over same period
        tolerance_pct: Maximum allowed deviation (default 1%)

    Returns:
        (passed, message)
    """
    if len(account_equity_curve) != len(spy_equity_curve) or len(account_equity_curve) < 2:
        return False, "Curve length mismatch or insufficient data"

    max_dev = 0.0
    for i in range(1, len(account_equity_curve)):
        if spy_equity_curve[i] > 0:
            dev = abs(account_equity_curve[i] - spy_equity_curve[i]) / spy_equity_curve[i] * 100
            max_dev = max(max_dev, dev)

    passed = max_dev <= tolerance_pct
    return passed, f"Max deviation from SPY: {max_dev:.2f}% (tolerance: {tolerance_pct}%)"