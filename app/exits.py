"""Signal-based exit decisions for held positions (pure functions, no I/O).

The broker-held bracket (stop loss + take profit) is the primary exit. These rules are the
*secondary* exits the engine proposes when a position's picture deteriorates. They are kept
out of engine.py so they can be unit-tested and so the backtester can share them later.

Rules, in priority order:
  1. Time stop        : held at least EXIT_MAX_HOLD_DAYS (matches BACKTEST_MAX_HOLD_DAYS). 0 = off.
  2. Confirmed trend  : the trend has read "down" on EXIT_CONFIRM_READINGS consecutive signal
                        snapshots that span at least EXIT_CONFIRM_MINUTES. One noisy refresh
                        no longer shakes a position out.
  3. ML deterioration : model probability fell below ML_MIN_P by more than EXIT_ML_MARGIN.
  4. Weakness with hysteresis : trend is not "up" AND (TA score is below EXIT_TA_FLOOR, or news
                        sentiment is below the gate's floor). The entry bar is CONSENSUS_TA_MIN
                        (+0.15); the exit bar is EXIT_TA_FLOOR (-0.10), so a trade that enters
                        at +0.16 is not sold at +0.14 and re-bought a moment later.
  5. Trailing stop    : when enabled and position has moved TRAILING_ACTIVATION_R R in our favor,
                        trail at TRAILING_ATR_MULT * ATR from the highest close since activation.

SELLs still always pass the consensus gate; nothing here can block an exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ExitDecision:
    kind: str     # time_stop | trend_down | ml_drop | weak_signal | trailing_stop |
                  # scale_out | breakeven_stop | giveback | rsi_exhaustion
    reason: str   # human-readable, goes into the SELL rationale
    trail_price: Optional[float] = None  # For trailing stop: the calculated trail price
    sell_fraction: float = 1.0           # 1.0 = close the position; <1.0 = partial scale-out
    tranche: int = 0                     # scale-out tranche number (1 or 2); 0 for full exits


def _parse(ts: str | datetime | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def held_days(entry_time: str | datetime | None, now: datetime) -> float | None:
    """Calendar days since entry, or None when the entry time is unknown."""
    entry = _parse(entry_time)
    if entry is None:
        return None
    return max(0.0, (now - entry).total_seconds() / 86400.0)


def trend_down_confirmed(history: list[dict], readings: int, minutes: float) -> bool:
    """True when the newest `readings` snapshots all say "down" and span >= `minutes`.

    `history` is newest-first (as DB.signals() returns it). With readings <= 1 and
    minutes <= 0 this behaves like the old single-snapshot check.
    """
    readings = max(1, int(readings))
    if len(history) < readings:
        return False
    window = history[:readings]
    if any((r.get("trend") or "") != "down" for r in window):
        return False
    if readings == 1 or minutes <= 0:
        return True
    newest, oldest = _parse(window[0].get("created_at")), _parse(window[-1].get("created_at"))
    if newest is None or oldest is None:
        return False
    return (newest - oldest).total_seconds() / 60.0 >= minutes


def calculate_trailing_stop(
    entry_price: float,
    current_price: float,
    atr: float | None,
    highest_close_since_entry: float | None,
    settings
) -> tuple[float | None, bool]:
    """
    Calculate trailing stop price if enabled and activated.

    Returns (trail_price, is_activated) where:
    - trail_price is the trailing stop price if activated, else None
    - is_activated is True if trailing stop should be active

    Activation: price has moved TRAILING_ACTIVATION_R * R in our favor
    Trail distance: TRAILING_ATR_MULT * ATR from highest close since activation
    """
    if not getattr(settings, "trailing_stop_enabled", False):
        return None, False
    if not atr or atr <= 0:
        return None, False

    activation_r = getattr(settings, "trailing_stop_activation_r", 1.0)
    trail_atr_mult = getattr(settings, "trailing_stop_atr_mult", 1.5)

    # Calculate R (risk per share = entry - initial stop)
    initial_risk = risk_per_share(entry_price, atr, settings)   # same 1R as the profit rules

    # Check activation: price has moved activation_r * R in our favor
    move = current_price - entry_price
    if move < activation_r * initial_risk:
        return None, False

    # Activated - use highest close since entry (or current price if not tracked)
    # For backtesting, we'll use the highest close from the signal history
    # For live, the engine would track this per position
    reference_price = highest_close_since_entry or current_price
    trail_distance = trail_atr_mult * atr
    trail_price = reference_price - trail_distance

    # Never trail below entry (protects initial capital)
    trail_price = max(trail_price, entry_price)

    return trail_price, True


def risk_per_share(entry: float, atr: float | None, s) -> float:
    """Initial risk (1R) in dollars per share, mirroring ta.levels(): SL_ATR_MULT * ATR,
    clamped to [MIN_SL_PCT, MAX_SL_PCT] of the entry price."""
    lo = entry * getattr(s, "min_sl_pct", 1.0) / 100.0
    hi = entry * getattr(s, "max_sl_pct", 8.0) / 100.0
    dist = getattr(s, "sl_atr_mult", 2.0) * atr if atr else lo
    return max(lo, min(hi, dist))


def profit_take_decision(sig: dict, s, *, entry_price: float | None, current_price: float | None,
                         peak_price: float | None, tranches_done: int = 0) -> ExitDecision | None:
    """Aggressive profit-taking for a position that is in the money. Pure function.

    Order of rules (first match wins):
      1. Scale-out 1: gain >= PROFIT_SCALE_OUT_R (in R) -> sell PROFIT_SCALE_OUT_FRACTION.
      2. Scale-out 2: gain >= PROFIT_SCALE_OUT_SECOND_R  -> sell a fraction of the remainder.
      3. RSI exhaustion: RSI14 >= PROFIT_RSI_EXHAUSTION and in profit -> sell everything left.
      4. Break-even stop: once the peak reached PROFIT_BREAKEVEN_R, a fall back to entry exits.
      5. Give-back: once the peak gain is >= PROFIT_GIVEBACK_MIN_GAIN_PCT, losing
         PROFIT_GIVEBACK_PCT of that peak gain exits.
    The bracket's fixed take-profit stays in place as the outer limit; these rules act sooner.
    """
    if not entry_price or not current_price or entry_price <= 0:
        return None
    if max(peak_price or current_price, current_price) <= entry_price:
        return None   # never in profit: nothing to take or protect beyond the stop
    atr = sig.get("atr")
    r = risk_per_share(entry_price, atr, s)
    gain_r = (current_price - entry_price) / r if r > 0 else 0.0
    peak = max(peak_price or current_price, current_price)
    peak_r = (peak - entry_price) / r if r > 0 else 0.0

    if getattr(s, "profit_scale_out_enabled", False) and current_price > entry_price:
        r1 = getattr(s, "profit_scale_out_r", 1.0)
        r2 = getattr(s, "profit_scale_out_second_r", 0.0)
        if tranches_done < 1 and gain_r >= r1:
            f = getattr(s, "profit_scale_out_fraction", 0.5)
            return ExitDecision("scale_out", f"scale-out 1: +{gain_r:.1f}R (+{(current_price/entry_price-1)*100:.1f}%), "
                                f"selling {f:.0%} to lock profit", sell_fraction=f, tranche=1)
        if tranches_done < 2 and r2 > 0 and gain_r >= r2:
            f = getattr(s, "profit_scale_out_second_fraction", 0.5)
            return ExitDecision("scale_out", f"scale-out 2: +{gain_r:.1f}R, selling {f:.0%} of the remainder",
                                sell_fraction=f, tranche=2)

    rsi_cap = getattr(s, "profit_rsi_exhaustion", 0.0)
    rsi = (sig.get("features") or {}).get("rsi14")
    if rsi_cap and rsi is not None and rsi >= rsi_cap and current_price > entry_price:
        return ExitDecision("rsi_exhaustion", f"RSI14 {rsi:.0f} >= {rsi_cap:.0f} while in profit: selling into strength")

    be_r = getattr(s, "profit_breakeven_after_r", 0.0)
    if be_r > 0 and peak_r >= be_r and current_price <= entry_price * 1.001:
        return ExitDecision("breakeven_stop", f"break-even stop: peaked at +{peak_r:.1f}R and fell back to entry")

    gb = getattr(s, "profit_giveback_pct", 0.0)
    min_gain = getattr(s, "profit_giveback_min_gain_pct", 0.0)
    peak_gain_pct = (peak / entry_price - 1.0) * 100.0
    if gb > 0 and peak > entry_price and peak_gain_pct >= min_gain:
        given_back = (peak - current_price) / (peak - entry_price) * 100.0
        if given_back >= gb:
            return ExitDecision("giveback", f"gave back {given_back:.0f}% of a +{peak_gain_pct:.1f}% peak gain "
                                f"(limit {gb:.0f}%)")
    return None


def scale_out_levels(entry: float, atr: float | None, s) -> list[tuple[int, float, float]]:
    """Scale-out triggers as (tranche, price, fraction) for a long entered at `entry`.
    Empty when PROFIT_SCALE_OUT is off. Same R maths as profit_take_decision."""
    if not getattr(s, "profit_scale_out_enabled", False) or not entry or entry <= 0:
        return []
    r = risk_per_share(entry, atr, s)
    out = [(1, entry + getattr(s, "profit_scale_out_r", 1.0) * r,
            getattr(s, "profit_scale_out_fraction", 0.5))]
    r2 = getattr(s, "profit_scale_out_second_r", 0.0)
    if r2 > 0:
        out.append((2, entry + r2 * r, getattr(s, "profit_scale_out_second_fraction", 0.5)))
    return out


def code_stop_levels(entry: float, peak: float, atr: float | None, s) -> list[tuple[str, float]]:
    """Price levels at or below which the code-managed rules sell everything left, given the
    highest price seen so far (`peak`). Returns (kind, level) pairs; the highest level is the
    one that fires first on a falling price. The backtester uses this so it exits exactly
    where profit_take_decision / calculate_trailing_stop would on a live price tick.

      breakeven_stop : PROFIT_BREAKEVEN_R reached -> entry * 1.001
      giveback       : peak gain >= PROFIT_GIVEBACK_MIN_GAIN_PCT -> peak - PROFIT_GIVEBACK_PCT of the gain
      trailing_stop  : TRAILING_STOP_ENABLED and peak >= entry + TRAILING_ACTIVATION_R * R
                       -> max(peak - TRAILING_ATR_MULT * ATR, entry)
    """
    out: list[tuple[str, float]] = []
    if not entry or entry <= 0 or not peak or peak <= entry:
        return out
    r = risk_per_share(entry, atr, s)
    peak_r = (peak - entry) / r if r > 0 else 0.0
    be_r = getattr(s, "profit_breakeven_after_r", 0.0)
    if be_r > 0 and peak_r >= be_r:
        out.append(("breakeven_stop", entry * 1.001))
    gb = getattr(s, "profit_giveback_pct", 0.0)
    if gb > 0 and (peak / entry - 1.0) * 100.0 >= getattr(s, "profit_giveback_min_gain_pct", 0.0):
        out.append(("giveback", peak - gb / 100.0 * (peak - entry)))
    if getattr(s, "trailing_stop_enabled", False) and atr and atr > 0:
        if peak - entry >= getattr(s, "trailing_stop_activation_r", 1.0) * r:
            out.append(("trailing_stop", max(peak - getattr(s, "trailing_stop_atr_mult", 1.5) * atr, entry)))
    return out


def decide_exit(
    sig: dict,
    history: list[dict],
    s,
    *,
    held_for_days: float | None = None,
    entry_price: float | None = None,
    current_price: float | None = None,
    highest_close_since_entry: float | None = None,
    tranches_done: int = 0,
) -> ExitDecision | None:
    """Return why a held position should be sold now, or None to keep holding.

    `sig` is the latest signal, `history` the recent signals newest-first (including `sig`),
    `s` the Settings (any object with the attributes used below).
    """
    max_hold = getattr(s, "exit_max_hold_days", 0)
    if max_hold and held_for_days is not None and held_for_days >= max_hold:
        return ExitDecision("time_stop",
                            f"time stop: held {held_for_days:.0f} days (limit {max_hold})")

    # ML reversal exit runs first so an ML flip closes the trade immediately.
    ml_p = sig.get("ml_p")
    if ml_p is not None and getattr(s, "ml_enabled", False) and getattr(s, "exit_ml_drop_enabled", True):
        floor = s.ml_min_p - getattr(s, "exit_ml_drop_margin", 0.0)
        if ml_p < floor:
            return ExitDecision("ml_drop", f"ML probability {ml_p:.0%} below exit floor {floor:.0%}")

    # Profit-taking runs before the trend/weakness rules so a winner is trimmed into strength
    # instead of waiting for the trend to roll over.
    pt = profit_take_decision(sig, s, entry_price=entry_price, current_price=current_price,
                              peak_price=highest_close_since_entry, tranches_done=tranches_done)
    if pt is not None:
        return pt

    if getattr(s, "exit_on_trend_break_enabled", True):
        if trend_down_confirmed(history or [sig], getattr(s, "exit_confirm_readings", 1),
                                getattr(s, "exit_confirm_minutes", 0)):
            n = max(1, int(getattr(s, "exit_confirm_readings", 1)))
            return ExitDecision("trend_down", f"trend down on {n} consecutive readings")

    if sig.get("trend") != "up":
        ta_score = sig.get("ta_score")
        floor = getattr(s, "exit_ta_floor", -0.10)
        if ta_score is not None and ta_score < floor:
            return ExitDecision("weak_signal",
                                f"TA score {ta_score:+.2f} below exit floor {floor:+.2f}")
        sent, n = sig.get("sent_score"), sig.get("sent_n") or 0
        if (sent is not None and n >= getattr(s, "consensus_min_news", 3)
                and sent < getattr(s, "consensus_sent_min", -0.25)):
            return ExitDecision("weak_signal", f"news sentiment {sent:+.2f} below gate floor")

    # Trailing stop check (only if we have the required price data)
    if getattr(s, "trailing_stop_enabled", False) and entry_price and current_price:
        atr = sig.get("atr")
        trail_price, activated = calculate_trailing_stop(
            entry_price, current_price, atr, highest_close_since_entry, s
        )
        if activated and trail_price and current_price <= trail_price:
            return ExitDecision("trailing_stop",
                                f"trailing stop hit at {trail_price:.2f} (current {current_price:.2f})",
                                trail_price=trail_price)

    return None


def sell_quantity(qty: float, s, symbol: str) -> float:
    """Quantity to sell for a held position of `qty` shares.

    The old code sold int(qty), leaving a fractional remainder behind. When fractional
    trading is enabled and the symbol is on the whitelist, sell the exact amount (rounded
    to FRACTIONAL_DECIMALS); otherwise sell whole shares only. Returns 0 if nothing can be sold.
    """
    if qty <= 0:
        return 0
    whole = int(qty)
    if qty == whole:
        return whole
    if getattr(s, "fractional_enabled", False) and symbol.upper() in {
            x.upper() for x in getattr(s, "fractional_whitelist", [])}:
        # Round DOWN to the allowed precision so we never try to sell more than we hold.
        d = getattr(s, "fractional_decimals", 2)
        factor = 10 ** d
        return int(qty * factor) / factor
    return whole


def partial_quantity(qty: float, fraction: float, s, symbol: str) -> float:
    """Shares to sell for a partial exit. Whole shares unless fractional selling is allowed;
    returns 0 when the position is too small to split (the caller then skips the scale-out)."""
    if fraction >= 1.0:
        return sell_quantity(qty, s, symbol)
    target = qty * fraction
    if getattr(s, "fractional_enabled", False) and symbol.upper() in {
            x.upper() for x in getattr(s, "fractional_whitelist", [])}:
        d = getattr(s, "fractional_decimals", 2)
        f = 10 ** d
        sold = int(target * f) / f
        return sold if 0 < sold < qty else 0
    sold = int(target)
    return float(sold) if 0 < sold < qty else 0
