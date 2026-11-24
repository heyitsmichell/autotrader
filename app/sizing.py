"""Pure BUY sizing shared by the live engine and the backtester.

Keeping this in one place means a backtest cannot quietly use a looser quantity
rule than the live path. The function only does arithmetic on Settings and the
current prices; no broker or database access.
"""

from __future__ import annotations

from .config import Settings


def risk_budget_usd(settings: Settings, equity: float | None = None) -> float:
    """Dollar risk allowed on one trade: RISK_PER_TRADE_PCT of equity when both are set,
    otherwise MAX_RISK_PER_TRADE_USD. MAX_RISK_PER_TRADE_USD stays the ceiling in the
    percent mode only when equity is unknown (then the USD figure is used)."""
    pct = float(getattr(settings, "risk_per_trade_pct", 0.0) or 0.0)
    if pct > 0 and equity and equity > 0:
        return equity * pct / 100.0
    return float(settings.max_risk_per_trade_usd)


def max_allowed_qty(settings: Settings, *, limit_price: float, last_price: float,
                    held: float, stop_loss: float | None, max_qty_override: float | None = None,
                    equity: float | None = None, buying_power: float | None = None) -> float:
    """Largest BUY quantity allowed by every hard order/position/risk limit.

    Mirrors ``Engine._max_allowed_qty`` exactly:
      - MAX_ORDER_QTY
      - MAX_ORDER_USD at the limit price (skipped when SIZE_FROM_RISK is on)
      - MAX_POSITION_USD less the value already held
      - the risk budget at the distance to the stop loss: MAX_RISK_PER_TRADE_USD, or
        RISK_PER_TRADE_PCT of `equity` when both are set
      - `buying_power` (optional) at the limit price

    Returns float to support fractional quantities when fractional trading is enabled.

    If `max_qty_override` is provided, the result is capped at that value (used to
    re-apply hard limits after volatility scaling).
    """
    s = settings
    from_risk = bool(getattr(s, "size_from_risk", False))
    max_qty = float(s.max_order_qty)
    # SIZE_FROM_RISK: the risk budget sets the size, so MAX_ORDER_USD must not silently shrink
    # it (the position cap and buying power below still bound the order).
    has_stop = bool(stop_loss) and limit_price > (stop_loss or 0)
    if limit_price > 0 and not (from_risk and has_stop):   # no stop = no risk sizing: keep the USD cap
        max_qty = min(max_qty, s.max_order_usd / limit_price)
    if last_price > 0:
        max_qty = min(max_qty, max(0.0, (s.max_position_usd - held * last_price)
                                          / limit_price))
    if stop_loss and limit_price > stop_loss:
        risk_per_share = limit_price - stop_loss
        if risk_per_share > 0:
            max_qty = min(max_qty, risk_budget_usd(s, equity) / risk_per_share)
    if buying_power is not None and limit_price > 0:
        max_qty = min(max_qty, max(0.0, buying_power) / limit_price)
    # Apply override cap (e.g., from volatility scaling)
    if max_qty_override is not None:
        max_qty = min(max_qty, max_qty_override)
    # Round to fractional precision if fractional trading is enabled
    if s.fractional_enabled:
        max_qty = round(max_qty, s.fractional_decimals)
    else:
        max_qty = float(int(max_qty))
    return max(max_qty, 0.0)


def overnight_scaled_qty(settings: Settings, qty: float, session: str | None) -> float:
    """BUY quantity in the NIGHT session x OVERNIGHT_SIZE_FACTOR (thin overnight books); other
    sessions and factor >= 1 leave it unchanged. Whole shares floor but never below 1 (a 1-share
    order stays 1); fractional quantities keep FRACTIONAL_DECIMALS."""
    f = float(getattr(settings, "overnight_size_factor", 1.0))
    if session != "NIGHT" or f >= 1.0 or qty <= 0:
        return qty
    scaled = qty * f
    if float(qty) == int(qty):
        return float(max(1, int(scaled)))
    return round(scaled, settings.fractional_decimals)


def volatility_scaled_qty(settings: Settings, base_qty: float, atr: float | None,
                          limit_price: float) -> float:
    """Scale quantity by volatility (ATR) to target consistent risk per position.

    If VOL_SCALING_ENABLED, adjusts the base quantity so that the position's
    daily ATR% equals VOL_TARGET_ATR_PCT (default 1.5%).

    The scale factor is: target_atr_pct / (atr / price * 100)
    Capped at VOL_MAX_FACTOR (default 2.0) and floored at 1/VOL_MAX_FACTOR.

    This only ever adjusts the size within the limits already set by max_allowed_qty.
    """
    s = settings
    if not getattr(s, "volatility_scaling_enabled", False):
        return base_qty
    if not atr or atr <= 0 or limit_price <= 0:
        return base_qty

    target_atr_pct = getattr(s, "volatility_scaling_target_atr_pct", 1.5)
    max_factor = getattr(s, "volatility_scaling_max_factor", 2.0)

    current_atr_pct = (atr / limit_price) * 100
    if current_atr_pct <= 0:
        return base_qty

    # Scale factor: target / current
    scale = target_atr_pct / current_atr_pct

    # Cap the scale factor
    scale = max(1.0 / max_factor, min(max_factor, scale))

    scaled_qty = base_qty * scale

    # Round to fractional precision if enabled
    if s.fractional_enabled:
        scaled_qty = round(scaled_qty, s.fractional_decimals)
    else:
        scaled_qty = float(int(scaled_qty))

    return max(scaled_qty, 0.0)


def ml_size_factor(settings: Settings, ml_p: float | None) -> float:
    """Share (0..1) of the full code-sized quantity to buy, from the model's probability.

    1.0 when scaling is off, ML is off, or there is no model score. Otherwise it rises
    linearly from ML_SIZE_MIN_FACTOR at p == ML_MIN_P to 1.0 at p >= ML_SIZE_FULL_P.
    This only ever shrinks a BUY, so every hard limit in ``max_allowed_qty`` still holds.
    """
    s = settings
    if ml_p is None or not s.ml_enabled or not s.ml_size_scaling:
        return 1.0
    lo, hi = s.ml_min_p, max(s.ml_size_full_p, s.ml_min_p + 1e-6)
    if ml_p >= hi:
        return 1.0
    if ml_p <= lo:
        return s.ml_size_min_factor
    frac = (ml_p - lo) / (hi - lo)
    return s.ml_size_min_factor + (1.0 - s.ml_size_min_factor) * frac
