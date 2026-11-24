"""Pure helpers shared by the engine mixins."""

from __future__ import annotations

import re
import uuid

from ..broker import BrokerError

MAX_PROPOSALS_PER_RUN = 8  # includes rejected ones, so the analyst can correct a few


def normalize_proposal(args: dict) -> dict:
    def as_int(v):
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return v

    def as_float(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return v

    # Base fields for all proposals
    result = {
        "symbol": str(args.get("symbol", "")).strip().upper(),
        "side": str(args.get("side", "")).strip().upper(),
        "quantity": as_int(args.get("quantity")),
        "limit_price": as_float(args.get("limit_price")),
        "confidence": as_float(args.get("confidence")),
        "rationale": str(args.get("rationale", "")).strip()[:2000],
    }

    # Preserve option-specific fields if present
    option_fields = [
        "option_type", "strike_price", "expiration_date", "position_intent",
        "option_strategy", "legs"
    ]
    for field in option_fields:
        if field in args:
            result[field] = args[field]

    return result


def _coid() -> str:
    return uuid.uuid4().hex  # 32 chars, also our idempotency key at Webull


def _is_option_symbol(symbol: str) -> bool:
    """Check if a symbol is an OCC option symbol (e.g., AAPL240119C00150000)."""
    import re
    return bool(re.match(r"^[A-Z]{1,5}\d{6}[CP]\d{8}$", symbol.upper()))


def _session_rejection(e: BrokerError) -> bool:
    """True when the broker rejected an order because of its trading session rather than
    because of price, size or buying power. Used to retry a bracket as a single entry."""
    msg = str(e).lower()
    # Specific session wording only. A bare "outside" or "not support" also appears in price-band,
    # size and fractional errors, and retrying those as a single order would drop the stop-loss
    # and take-profit without anyone asking for that.
    if any(token in msg for token in (
        "trading_session", "trading session", "session_closed", "session closed",
        "non_trading_hours", "non-trading hours", "overnight", "extended hours",
        "extended-hours", "pre-market", "premarket", "after-hours", "after hours",
        "openapi_gtx_not_support", "gtx_not_support",
    )):
        return True
    return (("not support" in msg or "outside" in msg or "unsupported" in msg)
            and any(word in msg for word in ("session", "hours")))



_OCC_RE = re.compile(r"^([A-Z]{1,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def parse_occ(symbol: str) -> dict | None:
    """Split an OCC option symbol (``AAPL240119C00150000``) into its parts.

    Returns ``{"underlying", "expiration" (YYYY-MM-DD), "option_type" ("CALL"/"PUT"),
    "strike"}`` or None when the text is not an OCC symbol.
    """
    m = _OCC_RE.match(str(symbol or "").strip().upper())
    if not m:
        return None
    root, yy, mm, dd, cp, strike = m.groups()
    return {"underlying": root, "expiration": f"20{yy}-{mm}-{dd}",
            "option_type": "CALL" if cp == "C" else "PUT", "strike": int(strike) / 1000}
