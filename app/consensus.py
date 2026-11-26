"""Consensus gate: a BUY goes ahead only when the independent engines agree.

Runs in code after the analyst proposes and before the risk checks. The analyst cannot
change or skip it. It also limits prompt injection: a planted headline can sway
The LLM and the sentiment score, but it can't fake the price-based TA signal.

  BUY  needs: TA score >= CONSENSUS_TA_MIN (or regime-adjusted), trend not "down",
       sentiment not below CONSENSUS_SENT_MIN (too few headlines = neutral), and, when a
       model has been promoted, model probability >= ML_MIN_P. In ML-first mode (ML_FIRST,
       with a promoted model) the TA threshold drops to the ML_FIRST_TA_FLOOR floor and the
       model decides; the downtrend and sentiment checks still apply.
       Optional: relative strength vs benchmark, market regime filter, pullback gate
       (ENTRY_PULLBACK_GATE: price 2-5% below its 10-day high; off by default).
  SELL always passes: sells only close shares you hold, and signals must never
       stand between you and an exit.

It also sets the stop loss and take profit from ATR (see ta.levels), and rejects a BUY whose
take-profit is too small to cover the round-trip trading cost (COST_EDGE_MULT).
"""

from __future__ import annotations

import json
import logging

from . import ta
from .backtest.costs import CostModel, round_trip_cost_pct
from .config import Settings


# strategy.md: a model above 70% may override the downtrend block (followup-correctness)
ML_DOWNTREND_OVERRIDE_P = 0.70

_ML_UNAVAILABLE_LOGGED: set[tuple] = set()


def _log_ml_unavailable(symbol) -> None:
    """Log once per symbol per day that the ML gate had no score (M11)."""
    _log_once("ml", symbol, "ML unavailable for %s; entry gated on rules only")


def _log_once(kind: str, symbol, msg: str) -> None:
    from datetime import date
    key = (kind, symbol, date.today())
    if key in _ML_UNAVAILABLE_LOGGED:
        return
    if len(_ML_UNAVAILABLE_LOGGED) > 5000:
        _ML_UNAVAILABLE_LOGGED.clear()
    _ML_UNAVAILABLE_LOGGED.add(key)
    logging.getLogger(__name__).warning(msg, symbol)


def pullback_pct(sig: dict) -> float | None:
    """How far (in percent, positive = below) the close is under the 10-day high."""
    f = sig.get("features") or {}
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except ValueError:
            return None
    v = f.get("pct_from_high10") if isinstance(f, dict) else None
    if v is None:
        return None
    try:
        return -float(v)   # indicators report % from the high as a negative number
    except (TypeError, ValueError):
        return None


def evaluate(p: dict, sig: dict | None, s: Settings) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, value=None, threshold=None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "value": value,
                       "threshold": threshold})

    if p.get("side") == "SELL":
        add("Exit", True, "Sells close shares you hold; signals never block an exit.")
        return {"ok": True, "checks": checks, "sl": None, "tp": None}
    if not sig or sig.get("ta_score") is None:
        add("Signal", False, "No TA signal for this symbol yet (refresh signals and retry).")
        return {"ok": False, "checks": checks, "sl": None, "tp": None}

    t = sig["ta_score"]
    ml_p = sig.get("ml_p")
    # ML-first: with a promoted model scoring this symbol, the model is the main selector and
    # the TA score is only a floor. With no model (or ML off) the full rules threshold applies.
    ml_first = bool(s.ml_enabled and s.ml_first and ml_p is not None)

    # Regime-aware TA threshold
    if getattr(s, "regime_aware_entries", False) and not ml_first:
        trend = sig.get("trend") or "range"
        if trend == "up":
            ta_min = s.regime_ta_min_up
        elif trend == "down":
            ta_min = s.regime_ta_min_down
        else:
            ta_min = s.regime_ta_min_range
    else:
        ta_min = min(s.ml_first_ta_floor, s.consensus_ta_min) if ml_first else s.consensus_ta_min

    add("TA direction", t >= ta_min,
        f"TA score {t:+.2f} (needs {ta_min:+.2f} or more"
        + ("; ML-first, so this is only a floor)" if ml_first else ")"), t, ta_min)
    trend = sig.get("trend") or "range"
    strength = sig.get("strength") or "n/a"
    feats = sig.get("features") or {}
    if isinstance(feats, str):       # DB rows store features as a JSON string
        try:
            import json
            feats = json.loads(feats) or {}
        except ValueError:
            feats = {}
    rsi = feats.get("rsi14") if isinstance(feats, dict) else None
    if rsi is None:
        rsi = sig.get("rsi14")
    # Allow buys in weak downtrend with oversold RSI (< 30) - mean reversion opportunity
    weak_downtrend_oversold = (
        trend == "down" and strength == "weak" and rsi is not None and rsi < 30
    )
    # ML-first override: high-confidence model can override downtrend block
    ml_override = (ml_first and ml_p is not None
                   and ml_p > max(ML_DOWNTREND_OVERRIDE_P, s.ml_min_p))
    add("TA regime", trend != "down" or weak_downtrend_oversold or ml_override,
        f"Trend is {trend} ({strength})"
        + ("" if trend != "down" else " (oversold bounce allowed)" if weak_downtrend_oversold
           else " (ML override)" if ml_override else " (blocked)"), trend)

    n, sent = sig.get("sent_n") or 0, sig.get("sent_score")
    if sig.get("news_unavailable") or sig.get("sent_state") == "unavailable":
        # M12: news outage is not a neutral read; flag it but do not block entries
        _log_once("news", sig.get("symbol"), "News unavailable for %s; sentiment check not a real read")
        add("Sentiment", True, "News unavailable (outage); not a neutral read, entry not blocked", sent)
    elif sent is None or n < s.consensus_min_news:
        add("Sentiment", True, f"Only {n} recent headline(s) (fewer than "
                               f"{s.consensus_min_news}), treated as neutral", sent)
    else:
        add("Sentiment", sent >= s.consensus_sent_min,
            f"News score {sent:+.2f} from {n} headline(s) (may not be below "
            f"{s.consensus_sent_min:+.2f})", sent, s.consensus_sent_min)

    if not s.ml_enabled:
        add("ML filter", True, "Turned off (ML_ENABLED=false)")
    elif ml_p is None:
        # M11: ML unavailable (no model or missing features). Not ML-first: the full rules
        # threshold and downtrend block above already applied; passes only on the rules.
        _log_ml_unavailable(sig.get("symbol"))
        add("ML filter", True, "ML unavailable (no model or missing features); "
                               "full rules threshold applied, rules only")
    else:
        add("ML filter", ml_p >= s.ml_min_p,
            f"Model gives {ml_p:.0%} odds of hitting take-profit before stop-loss "
            f"(needs {s.ml_min_p:.0%})",
            ml_p, s.ml_min_p)

    # Pullback gate (strategy.md "prefer pullbacks 2-5% below the 10-day high"). Opt-in via
    # ENTRY_PULLBACK_GATE. Missing data fails closed when the gate is on.
    if getattr(s, "entry_pullback_gate", False):
        off = pullback_pct(sig)
        lo, hi = s.entry_pullback_min_pct * 100, s.entry_pullback_max_pct * 100
        if off is None:
            add("Pullback", False, "Pullback data (10-day high) not available", None, (lo, hi))
        else:
            add("Pullback", lo <= off <= hi,
                f"Price is {off:.2f}% below its 10-day high (needs {lo:.1f}% to {hi:.1f}% below)",
                round(off, 3), (lo, hi))

    # Market regime filter: check broader market regime if enabled
    if getattr(s, "market_regime_filter_enabled", False):
        market_trend = sig.get("market_trend") or "range"
        market_strength = sig.get("market_strength") or "n/a"
        market_adx = sig.get("market_adx")
        min_trend = getattr(s, "market_regime_min_trend", "up")
        adx_min = getattr(s, "market_regime_adx_min", 20.0)

        # Check if market regime is acceptable
        regime_ok = True
        detail_parts = [f"Market trend is {market_trend} ({market_strength})"]

        if min_trend == "up":
            if market_trend != "up":
                regime_ok = False
                detail_parts.append("(blocked: need up trend)")
            elif market_adx is not None and market_adx < adx_min:
                regime_ok = False
                detail_parts.append(f"(blocked: ADX {market_adx:.1f} < {adx_min})")
        elif min_trend == "range":
            if market_trend == "down":
                regime_ok = False
                detail_parts.append("(blocked: down trend not allowed)")
        # min_trend == "down" means no filter

        add("Market regime", regime_ok, " ".join(detail_parts), market_trend, min_trend)
        if not regime_ok:
            # Early return if market regime blocks
            entry = p.get("limit_price") or sig.get("close")
            sl, tp = ta.levels(entry, sig.get("atr"), s)
            return {"ok": False, "checks": checks, "sl": sl, "tp": tp}

    # Relative strength check: rank vs benchmark if enabled
    if getattr(s, "relative_strength_enabled", False):
        rs = sig.get("relative_strength")
        rs_min = getattr(s, "relative_strength_min", 0.0)
        if rs is not None:
            add("Relative strength", rs >= rs_min,
                f"Relative strength vs {getattr(s, 'market_regime_symbol', 'SPY')}: {rs:+.2f}% "
                f"(needs {rs_min:+.2f}% or more)", rs, rs_min)
        else:
            add("Relative strength", False, "Relative strength data not available")

    entry = p.get("limit_price") or sig.get("close")
    sl, tp = ta.levels(entry, sig.get("atr"), s)

    # Cost edge: the take-profit has to be worth the round-trip cost (slippage + commission,
    # same numbers as the backtester). Skipped when there is no target or the check is off.
    if s.cost_edge_mult > 0 and tp and entry:
        cost_pct = round_trip_cost_pct(CostModel.from_settings(s), entry, atr=sig.get("atr"),
                                       notional=s.max_order_usd)
        tp_pct = (tp / entry - 1) * 100
        needed = s.cost_edge_mult * cost_pct
        add("Cost edge", tp_pct >= needed,
            f"Take-profit is {tp_pct:.2f}% away; round-trip cost is about {cost_pct:.2f}% "
            f"(needs at least {s.cost_edge_mult:g}x = {needed:.2f}%)", round(tp_pct, 3), round(needed, 3))
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "sl": sl, "tp": tp}
