"""Point-in-time signal adapter for the daily backtester.

It deliberately calls the live modules instead of re-implementing them:
``indicators.feature_series``, ``ta.analyze`` and ``consensus.evaluate``. Only
bars ``0..i`` are passed to the feature builder, so today's decision cannot see
tomorrow. Historical news does not exist, so sentiment is neutral (``sent_n=0``).
ML follows the live setting: ``settings.backtest_ml_enabled`` (default True, QA H7) with a
predictor. ``build_ml_predictor`` builds a walk-forward one (retrained on past data only) and
degrades to rules-only with a clear message when PyTorch or data is missing.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import consensus, ta
from ..config import Settings
from ..indicators import feature_series


def relative_strength_pct(sym_now: float, sym_then: float,
                          bench_now: float, bench_then: float) -> float:
    """Shared RS definition (live + backtest): symbol momentum minus benchmark momentum, in percent.

    ``then`` is the close ``lookback`` bars before ``now`` (i.e. bars[i - lookback]).
    """
    return ((sym_now / sym_then - 1) - (bench_now / bench_then - 1)) * 100


def bench_close_asof(bench_bars: list[dict], day, day_fn) -> float | None:
    """Close of the latest benchmark bar dated <= ``day`` (point-in-time, like _align_benchmark)."""
    out = None
    for b in bench_bars:
        if day_fn(b["time"]) <= day:
            out = b["close"]
        else:
            break
    return out


# What a daily-bar backtest cannot reproduce. Printed in every backtest report (QA M8).
LIMITATIONS = (
    "Sentiment is always neutral: historical headlines are not available, so the live "
    "'Sentiment' gate never blocks here.",
    "First-15-minutes-after-open rule is not simulated: daily bars have no intraday timing "
    "(entries fill at the next open).",
    "Earnings blackout is not simulated: no historical earnings calendar. Live entries are "
    "blocked within ELIGIBILITY_EARNINGS_BLACKOUT_DAYS of earnings and fail closed without a "
    "calendar; the backtest may trade through earnings.",
    "One position per symbol: the live 'max adds per symbol' and 'never add to a loser' rules "
    "never trigger here (the backtester never adds to a held symbol).",
    "Signal freshness, spread/quote checks and broker/risk guards (daily loss, kill switch) "
    "are live-only.",
)


def limitations_text(settings: Settings, ml_status: str = "") -> str:
    """Human-readable parity report: ML mode, pullback gate and the limitations above."""
    lines = ["BACKTEST PARITY NOTES"]
    lines.append("  ML filter: " + (ml_status or (
        "on (a predictor must be supplied)" if settings.backtest_ml_enabled
        else "off (rules-only comparison)")))
    if getattr(settings, "entry_pullback_gate", False):
        lines.append(f"  Pullback gate: on ({settings.entry_pullback_min_pct:.0%}-"
                     f"{settings.entry_pullback_max_pct:.0%} below the 10-day high)")
    lines.append("  Limitations vs live:")
    lines.extend(f"   - {t}" for t in LIMITATIONS)
    return "\n".join(lines)


def build_ml_predictor(settings: Settings, bars_by_symbol: dict[str, list[dict]]
                       ) -> tuple[Callable[[dict], float | None] | None, str]:
    """Walk-forward ML predictor for the backtest, or (None, reason) when it cannot be built.

    Never raises: a missing PyTorch install or too little data degrades to rules-only and
    says so in the returned status string (which the report prints).
    """
    if not settings.backtest_ml_enabled:
        return None, "off (BACKTEST_ML_ENABLED=false or --no-ml): rules-only"
    try:
        from .. import ml
        predictor = ml.make_walkforward_predictor(settings, bars_by_symbol)
    except Exception as e:  # noqa: BLE001 - MLUnavailable, torch import errors, anything else
        return None, (f"UNAVAILABLE ({type(e).__name__}: {e}); falling back to RULES-ONLY, "
                      "which does not match the live decision rule")
    return predictor, (f"on: walk-forward retraining every {predictor.refit_days} days on past "
                       f"data only ({predictor.n_rows} labelled rows)")


def build_features(bars: list[dict]) -> list[dict]:
    """Features for every bar, each using only that bar and earlier bars."""
    return feature_series(bars)


def eligibility_ok(bars: list[dict], index: int, features: dict,
                   settings: Settings) -> tuple[bool, str]:
    """Pure subset of the live entry-eligibility checks that daily bars support.

    Earnings, signal freshness and the first 15 minutes need intraday or
    calendar data that a daily bar cannot provide; they stay in the live path.
    """
    s = settings
    if not s.eligibility_enabled:
        return True, ""
    close = features.get("close")
    if close is None or close <= 0:
        return False, "no close"
    if s.eligibility_min_price > 0 and close < s.eligibility_min_price:
        return False, f"price {close:.2f} below {s.eligibility_min_price:.2f}"
    if s.eligibility_min_bars > 0 and index + 1 < s.eligibility_min_bars:
        return False, f"only {index + 1} bars"
    if s.eligibility_min_volume > 0:
        window = bars[max(0, index - 19):index + 1]
        dollar = [b["close"] * (b.get("volume") or 0) for b in window]
        avg = sum(dollar) / len(dollar) if dollar else 0.0
        if avg < s.eligibility_min_volume:
            return False, (f"avg dollar volume ${avg:,.0f} below "
                           f"${s.eligibility_min_volume:,.0f}")
    if s.eligibility_max_gap_pct > 0 and index > 0:
        prev = bars[index - 1]["close"]
        if prev > 0:
            gap = abs(bars[index]["open"] - prev) / prev * 100
            if gap > s.eligibility_max_gap_pct:
                return False, f"gap {gap:.2f}% over {s.eligibility_max_gap_pct:.2f}%"
    return True, ""


def signal_at(bars: list[dict], features: list[dict], index: int, settings: Settings,
              *, ml_predictor: Callable[[dict], float | None] | None = None,
              benchmark_bars: list[dict] | None = None,
              benchmark_features: list[dict] | None = None) -> dict:
    """Live TA + consensus decision for the close of ``bars[index]``.

    Returns ``{"ok", "signal", "sl", "tp", "gate", "eligible", "reason"}``.
    The caller is responsible for the one-position-per-symbol and cash checks.
    """
    if index < 0 or index >= len(bars) or index >= len(features):
        return {"ok": False, "reason": "no bar"}
    f = features[index]
    bars_so_far = bars[:index + 1]
    eligible, reason = eligibility_ok(bars_so_far, index, f, settings)
    if not eligible:
        return {"ok": False, "eligible": False, "reason": reason}

    analyzed = ta.analyze(f, settings)
    ml_p = None
    if settings.backtest_ml_enabled and ml_predictor is not None:
        try:
            ml_p = ml_predictor(f)
        except Exception:  # noqa: BLE001 - an ML failure must not create a signal
            ml_p = None

    # Compute relative strength vs benchmark if enabled
    relative_strength = None
    market_trend = "range"
    market_strength = "n/a"
    market_adx = None
    need_market_regime = (getattr(settings, "relative_strength_enabled", False) or
                          getattr(settings, "market_regime_filter_enabled", False))
    if need_market_regime and benchmark_bars and benchmark_features:
        lookback = getattr(settings, "relative_strength_lookback", 20)
        if index >= lookback and len(benchmark_bars) > index and len(benchmark_features) > index:
            # Momentum = (close / close_lookback_ago) - 1
            relative_strength = relative_strength_pct(
                bars[index]["close"], bars[index - lookback]["close"],
                benchmark_bars[index]["close"], benchmark_bars[index - lookback]["close"])

        # Market regime from benchmark
        bench_f = benchmark_features[index]
        bench_analyzed = ta.analyze(bench_f, settings)
        market_trend = bench_analyzed.get("trend", "range")
        market_strength = bench_analyzed.get("strength", "n/a")
        market_adx = bench_f.get("adx14")

    sig = {
        "symbol": "",
        "close": f.get("close"),
        "atr": analyzed.get("atr"),
        "ta_score": analyzed.get("ta_score"),
        "ta_components": analyzed.get("ta_components"),
        "trend": analyzed.get("trend"),
        "strength": analyzed.get("strength"),
        "vol": analyzed.get("vol"),
        "sent_score": None,
        "sent_n": 0,
        "ml_p": ml_p,
        "features": f,
        # New fields for strategy improvements
        "relative_strength": relative_strength,
        "market_trend": market_trend,
        "market_strength": market_strength,
        "market_adx": market_adx,
    }
    proposal = {
        "symbol": "",
        "side": "BUY",
        "quantity": 1,
        "limit_price": f.get("close"),
    }
    gate = consensus.evaluate(proposal, sig, settings)
    return {
        "ok": bool(gate.get("ok")),
        "eligible": True,
        "reason": "" if gate.get("ok") else "; ".join(
            c["detail"] for c in gate.get("checks", []) if not c.get("ok")),
        "signal": sig,
        "gate": gate,
        "sl": gate.get("sl"),
        "tp": gate.get("tp"),
    }
