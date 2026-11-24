"""Order-type decisions (market-orders feature). Pure helpers, no I/O.

Styles recorded on ``proposals.order_type``: ``limit`` (today's behaviour), ``marketable`` (BUY
limit at ask + offset, capped at the price band), ``market`` (true MARKET, exits in the CORE
session only) and ``chase`` (an unfilled entry re-sent once as marketable).
"""

from __future__ import annotations

import math

STYLES = ("limit", "marketable", "market", "chase")


def tick_for(price: float) -> float:
    """Webull price increment: 0.01 at or above $1, 0.0001 below."""
    return 0.01 if price >= 1.0 else 0.0001


def _round_tick(price: float, *, down: bool = False) -> float:
    tick = tick_for(price)
    n = price / tick
    n = math.floor(n + 1e-9) if down else math.floor(n + 0.5 + 1e-9)
    return round(n * tick, 4 if tick < 0.01 else 2)


def marketable_buy_price(settings, *, last: float | None, ask: float | None = None,
                         ref_price: float | None = None) -> float | None:
    """BUY limit price: ask (else last) x (1 + MARKETABLE_OFFSET_BPS/10000), rounded to the tick
    and never above ref_price x (1 + PRICE_BAND_PCT/100). None when there is no usable price."""
    base = ask if ask and ask > 0 else last
    if not base or base <= 0:
        return None
    px = base * (1 + float(settings.marketable_offset_bps) / 10000.0)
    px = _round_tick(px)
    if ref_price and ref_price > 0:
        cap = ref_price * (1 + float(settings.price_band_pct) / 100.0)
        if px > cap:
            px = _round_tick(cap, down=True)
    return px if px > 0 else None


def within_band(settings, price: float | None, ref_price: float | None) -> bool:
    """True when ``price`` is within PRICE_BAND_PCT of the reference price (either side)."""
    if not price or not ref_price or ref_price <= 0:
        return False
    return abs(price - ref_price) / ref_price * 100.0 <= float(settings.price_band_pct) + 1e-9


def exit_should_be_market(settings, session: str | None) -> bool:
    """MARKET exits only when enabled and the CORE session is open (Webull rejects market-type
    orders that include extended hours: OPENAPI_GTX_NOT_SUPPORT_MARKET)."""
    return str(getattr(settings, "exit_order_type", "limit")).lower() == "market" and session == "CORE"


# --- MARKET rejection / cancel-reply classification ---------------------------------------------
_DEFINITIVE_REJECTION = (
    "validation error", "parameter error", "gtx_not_support", "not_support_market",
    "market not allowed", "market order not", "market orders are not", "not allowed",
    "not supported", "unsupported order type",
)
_AMBIGUOUS_MARK = ("internal error", "http 5", "timeout", "timed out", "readtimeout")


def market_rejection_is_definitive(err) -> bool:
    """True only when a MARKET order error proves the order was NOT accepted (parameter or
    validation rejection, market type not allowed). MCP maps SDK/HTTP failures to "Server error
    (HTTP 5xx)" / "Internal error: ReadTimeout", where the MARKET may already be live; those are
    ambiguous and must never be answered with a second (LIMIT) SELL."""
    if getattr(err, "transport", False):
        return False
    msg = str(err).lower()
    if any(m in msg for m in _AMBIGUOUS_MARK):
        return False
    return any(m in msg for m in _DEFINITIVE_REJECTION)


def cancel_reply_confirmed(reply) -> bool:
    """False for MCP cancel replies that do not prove the order is gone ("Order not found; it may
    have been filled or already cancelled", "Permission denied...") but do not look like errors."""
    low = str(reply or "").strip().lower()
    return not low.startswith(("order not found", "permission denied"))
