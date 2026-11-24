"""Technical-analysis engine: rules-based regime, a transparent direction score,
and ATR-based stop-loss / take-profit levels. Pure code, no AI: the same inputs
always give the same answer, and it can be backtested honestly.

The score is a sum of small, named components so the dashboard can show exactly
why a symbol scored what it did. It is clipped to -1..+1 (+ favours buying).
"""

from __future__ import annotations

import logging
import os

from .config import Settings

log = logging.getLogger(__name__)


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def regime(f: dict) -> dict:
    close, s20, s50 = f.get("close"), f.get("sma20"), f.get("sma50")
    if None in (close, s20, s50):
        trend = "range"
    elif close > s50 and s20 > s50:
        trend = "up"
    elif close < s50 and s20 < s50:
        trend = "down"
    else:
        trend = "range"
    a = f.get("adx14")
    strength = "n/a" if a is None else "strong" if a >= 25 else "weak" if a < 20 else "moderate"
    rank = f.get("atr_pct_rank")
    vol = "n/a" if rank is None else "high" if rank >= 0.7 else "low" if rank <= 0.3 else "normal"
    return {"trend": trend, "strength": strength, "vol": vol}


_warned_v1 = False


def _warn_v1_once() -> None:
    global _warned_v1
    if not _warned_v1:
        _warned_v1 = True
        log.warning("TA_MODEL=v1 was retired (QA L14); using the v2 scorer. The score scale "
                    "differs from v1: re-check CONSENSUS_TA_MIN and retrain the ML model. "
                    "Remove TA_MODEL from your .env to silence this.")


def model() -> str:
    """Active scoring model. Only ``v2`` exists. ``TA_MODEL=v1`` (the old default, possibly
    still in a saved .env) is accepted, mapped to v2, and warned about once."""
    raw = os.getenv("TA_MODEL", "").strip().lower()
    if raw == "v1":
        _warn_v1_once()
    return "v2"


def score(f: dict, which: str | None = None) -> tuple[float, list[dict]]:
    """Direction score and its components (v2). ``which="v1"`` is accepted for old callers and
    maps to v2 with a one-time warning."""
    if which is not None and which.strip().lower() == "v1":
        _warn_v1_once()
    return _score_v2(f)


def _score_v2(f: dict) -> tuple[float, list[dict]]:
    """Grouped, volatility-normalised score.

    What changed versus v1, and why:
    * v1 spent four of its six rules (price vs SMA50, SMA20 vs SMA50, SMA20 slope, 20-day
      momentum) on the same fact, "the trend is up", so one fact counted about 80% of the
      score. v2 has one trend group, one momentum group, one oscillator group and one
      volume group, each capped, so no single idea dominates.
    * v1 used fixed +/-0.25 steps, so a stock 0.1% above SMA50 scored the same as one 15%
      above it. v2 measures distance in ATR units (distance / daily range), which also makes
      a calm utility and a volatile semiconductor comparable.
    * ADX was computed but never used. v2 scales the trend and momentum groups by trend
      strength: weak trends (ADX < 20) count 60%, strong trends (ADX >= 25) count 115%.
    * v1's RSI rule rewarded RSI < 25 but penalised 25-30 (inconsistent) and happily
      "bought" falling knives. v2 rewards oversold only when the trend is not down and
      penalises overbought readings.
    * Volume was computed but never used. v2 adds a small confirmation: a heavy-volume
      move (>= 1.5x average) in the direction of the 5-day change adds or removes 0.10.
    Weights are still judgement calls. Validate with scripts/ta_evaluate.py before
    changing CONSENSUS_TA_MIN.
    """
    comps: list[dict] = []

    def add(name: str, value, points: float, note: str) -> None:
        comps.append({"name": name, "value": value, "points": round(points, 3), "note": note})

    close, s20, s50 = f.get("close"), f.get("sma20"), f.get("sma50")
    atr_pct = f.get("atr_pct")
    adx = f.get("adx14")
    # Trend strength multiplier (ADX). Unknown ADX leaves the score unscaled.
    mult = 1.0 if adx is None else 0.6 if adx < 20 else 1.15 if adx >= 25 else 1.0

    def atr_units(pct: float | None, days: float = 1.0) -> float | None:
        """A % move expressed in daily-ATR units (scaled by sqrt(days) for multi-day moves)."""
        if pct is None or not atr_pct or atr_pct <= 0:
            return None
        return pct / (atr_pct * days ** 0.5)

    # 1) Trend: where price sits relative to SMA50 (ATR units) plus average alignment.
    dist = atr_units(f.get("pct_vs_sma50"))
    if dist is not None:
        pts = 0.20 * _clip(dist / 4.0) * mult
        if s20 is not None and s50:
            pts += (0.10 if s20 > s50 else -0.10) * mult
        aligned = s20 is not None and s50 and s20 > s50
        add("Trend (vs SMA50, ATR units)", round(dist, 2), pts,
            "above the 50-day average with aligned averages" if pts > 0 and aligned
            else "above the 50-day average" if pts > 0
            else "below the 50-day average" if pts < 0 else "at the 50-day average")
    elif close is not None and s50 is not None:
        up = close > s50
        add("Trend (vs SMA50)", f.get("pct_vs_sma50"), 0.15 if up else -0.15,
            "above the 50-day average" if up else "below the 50-day average")

    # 2) Momentum: 20-day change in ATR units, plus slope of the 20-day average.
    m20 = atr_units(f.get("chg_20d_pct"), 20)
    slope = f.get("sma20_slope_pct")
    if m20 is not None or slope is not None:
        pts = 0.0
        if m20 is not None:
            pts += 0.15 * _clip(m20 / 2.0)
        if slope is not None:
            pts += 0.10 * _clip(slope / 1.0)
        pts *= mult
        add("Momentum (20d, ATR units)", None if m20 is None else round(m20, 2), pts,
            "rising" if pts > 0.0 else "falling" if pts < 0.0 else "flat")

    # 3) Oscillator: oversold dips count only when the trend is not down.
    r = f.get("rsi14")
    if r is not None:
        trend = regime(f)["trend"]
        if r > 75:
            add("RSI14", r, -0.15, "overbought (> 75)")
        elif r > 65:
            add("RSI14", r, -0.05, "stretched (65-75)")
        elif r < 30 and trend != "down":
            add("RSI14", r, 0.15, "oversold in a non-down trend (< 30), dip-buy setup")
        elif r < 30:
            add("RSI14", r, -0.10, "oversold in a downtrend (< 30), falling knife")
        elif 40 <= r <= 65:
            add("RSI14", r, 0.10, "healthy range (40-65)")
        else:
            add("RSI14", r, 0.0, "neutral")

    # 4) Pullback in an uptrend (kept from v1).
    off_high = f.get("pct_from_high10")
    if off_high is not None and close is not None and s50 is not None:
        pullback = close > s50 and -5.0 <= off_high <= -2.0
        add("Pullback in uptrend", off_high, 0.10 if pullback else 0.0,
            "2-5% below the 10-day high in an uptrend" if pullback else "no pullback setup")

    # 5) Volume confirmation: heavy volume in the direction of the 5-day move.
    vol, chg5 = f.get("vol_vs_20d_avg"), f.get("chg_5d_pct")
    if vol is not None and chg5 is not None:
        if vol >= 1.5 and chg5 > 0:
            add("Volume confirmation", vol, 0.10, f"{vol:.1f}x volume behind a rising 5 days")
        elif vol >= 1.5 and chg5 < 0:
            add("Volume confirmation", vol, -0.10, f"{vol:.1f}x volume behind a falling 5 days")
        else:
            add("Volume confirmation", vol, 0.0, "no unusual volume")

    total = _clip(sum(c["points"] for c in comps))
    return round(total, 3), comps


def levels(entry: float, atr: float | None, s: Settings) -> tuple[float | None, float | None]:
    """Stop loss and take profit for a long entry, from ATR with % clamps."""
    if not entry or not atr:
        return None, None
    dist = s.sl_atr_mult * atr
    dist = max(entry * s.min_sl_pct / 100, min(entry * s.max_sl_pct / 100, dist))
    reward = dist * (s.tp_atr_mult / s.sl_atr_mult)
    return round(entry - dist, 2), round(entry + reward, 2)


def analyze(f: dict, s: Settings) -> dict:
    """Full TA signal for the latest bar's features."""
    total, comps = score(f)
    sl, tp = levels(f.get("close"), f.get("atr14"), s)
    return {"close": f.get("close"), "atr": f.get("atr14"), "ta_score": total,
            "ta_components": comps, **regime(f), "sl": sl, "tp": tp}