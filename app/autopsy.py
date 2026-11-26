"""Deterministic autopsy system - classifies process vs outcome using trade ledger data.

The autopsy is evidence-based: it uses only trade ledger data (not narrative) to
rank causes by evidence strength. The LLM only narrates the findings, never
determines the cause.

Does an autopsy feed decisions? (QA M9)
  - Sizing, gates, risk checks, exits: NO. Nothing in sizing.py, consensus.py, risk.py or the
    engine reads autopsy rows. Wiring autopsy output into sizing would need a guard (n >= 30,
    significance, human approval, hard bounds) that does not exist, so it is deliberately not
    wired. ``influence()`` below states this in code so tests can pin it.
  - Parameter lessons: INDIRECTLY, as proposals only. ``lessons.propose_lessons_from_autopsies``
    reads autopsy causes, validates on a chronological split with a significance test, and stores
    a lesson in status 'proposed'. A human must approve it before the engine applies it
    (engine auto-apply also needs n >= 30, p < 0.05 and bounds; risk limits are never touched).
  - Dashboard: display only (/api/autopsies).
"""

from __future__ import annotations

from dataclasses import dataclass


# Evidence categories for causes
CAUSE_CATEGORIES = {
    "slippage": "process",          # excessive entry/exit slippage
    "gap_risk": "process",          # overnight/weekend gap through stop
    "stop_placement": "process",    # stop too tight/wide for ATR
    "signal_quality": "process",    # weak TA/sentiment/ML at entry
    "sizing": "process",            # position size vs risk limits
    "regime_mismatch": "process",   # trend regime wrong at entry
    "good_process": "good_process", # followed plan correctly
    "luck": "outcome",              # outcome driven by external factors
}


def influence() -> dict:
    """Machine-readable statement of what autopsy output is allowed to influence."""
    return {"sizing": False, "gates": False, "risk": False, "exits": False,
            "lessons": "proposed only (human approval required)", "display": True}


@dataclass
class Cause:
    cause: str
    evidence: str
    strength: float  # 0-1, higher = stronger evidence
    category: str    # "process", "good_process", or "outcome"


def analyze_slippage(trade: dict) -> list[Cause]:
    """Analyze entry/exit slippage as evidence."""
    causes = []
    entry_slip = trade.get("entry_slippage") or 0
    exit_slip = trade.get("exit_slippage") or 0

    # Entry slippage > 0.5% of entry price
    if trade.get("entry_fill_price") and abs(entry_slip) / trade["entry_fill_price"] > 0.005:
        causes.append(Cause(
            cause="excessive_entry_slippage",
            evidence=f"Entry slippage {entry_slip:+.2f} ({(entry_slip/trade['entry_fill_price']*100):+.2f}%)",
            strength=min(abs(entry_slip) / trade["entry_fill_price"] * 100, 1.0),
            category="process"
        ))

    # Exit slippage > 0.5%
    if trade.get("exit_fill_price") and abs(exit_slip) / trade["exit_fill_price"] > 0.005:
        causes.append(Cause(
            cause="excessive_exit_slippage",
            evidence=f"Exit slippage {exit_slip:+.2f} ({(exit_slip/trade['exit_fill_price']*100):+.2f}%)",
            strength=min(abs(exit_slip) / trade["exit_fill_price"] * 100, 1.0),
            category="process"
        ))

    return causes


def analyze_gap_risk(trade: dict) -> list[Cause]:
    """Analyze gap risk (overnight/weekend gap through stop)."""
    causes = []
    exit_reason = trade.get("exit_reason", "")
    entry_price = trade.get("entry_fill_price")
    stop_price = trade.get("stop_price")

    if exit_reason == "stop" and entry_price and stop_price:
        # If exit was a stop loss, check if price gapped through it
        # (exit price significantly worse than stop price)
        exit_price = trade.get("exit_fill_price")
        if exit_price and stop_price:
            gap = (stop_price - exit_price) / stop_price if trade["side"] == "BUY" else (exit_price - stop_price) / stop_price
            if gap > 0.01:  # >1% gap through stop
                causes.append(Cause(
                    cause="gap_through_stop",
                    evidence=f"Stop at {stop_price:.2f}, filled at {exit_price:.2f} ({gap*100:+.1f}% gap)",
                    strength=min(gap * 50, 1.0),
                    category="process"
                ))

    return causes


def analyze_stop_placement(trade: dict) -> list[Cause]:
    """Analyze stop placement relative to ATR."""
    causes = []
    entry_price = trade.get("entry_fill_price")
    stop_price = trade.get("stop_price")
    entry_atr = trade.get("entry_atr")

    if entry_price and stop_price and entry_atr:
        atr_dist = abs(entry_price - stop_price) / entry_atr if entry_atr > 0 else 0
        # Stop too tight (<1.5 ATR) or too wide (>3 ATR)
        if atr_dist < 1.5:
            causes.append(Cause(
                cause="stop_too_tight",
                evidence=f"Stop distance {atr_dist:.1f}x ATR (entry: {entry_price:.2f}, stop: {stop_price:.2f}, ATR: {entry_atr:.2f})",
                strength=min((1.5 - atr_dist) / 1.5, 1.0),
                category="process"
            ))
        elif atr_dist > 3.0:
            causes.append(Cause(
                cause="stop_too_wide",
                evidence=f"Stop distance {atr_dist:.1f}x ATR (entry: {entry_price:.2f}, stop: {stop_price:.2f}, ATR: {entry_atr:.2f})",
                strength=min((atr_dist - 3.0) / 3.0, 1.0),
                category="process"
            ))
        else:
            causes.append(Cause(
                cause="stop_placement_appropriate",
                evidence=f"Stop distance {atr_dist:.1f}x ATR within normal range (1.5-3.0)",
                strength=0.7,
                category="good_process"
            ))

    return causes


def analyze_signal_quality(trade: dict) -> list[Cause]:
    """Analyze signal quality at entry."""
    causes = []
    ta_score = trade.get("entry_ta_score")
    sent_score = trade.get("entry_sent_score")
    ml_p = trade.get("entry_ml_p")
    trend = trade.get("entry_trend")

    # Weak TA score (<0.3)
    if ta_score is not None and ta_score < 0.3:
        causes.append(Cause(
            cause="weak_ta_signal",
            evidence=f"TA score {ta_score:.2f} at entry (threshold ~0.3)",
            strength=min((0.3 - ta_score) / 0.3, 1.0),
            category="process"
        ))
    elif ta_score is not None and ta_score >= 0.5:
        causes.append(Cause(
            cause="strong_ta_signal",
            evidence=f"TA score {ta_score:.2f} at entry",
            strength=min(ta_score, 1.0),
            category="good_process"
        ))

    # Negative sentiment
    if sent_score is not None and sent_score < -0.2:
        causes.append(Cause(
            cause="negative_sentiment",
            evidence=f"Sentiment score {sent_score:.2f} at entry",
            strength=min(abs(sent_score) / 0.5, 1.0),
            category="process"
        ))

    # Low ML probability
    if ml_p is not None and ml_p < 0.55:
        causes.append(Cause(
            cause="low_ml_probability",
            evidence=f"ML win probability {ml_p:.2f} at entry (threshold ~0.55)",
            strength=min((0.55 - ml_p) / 0.55, 1.0),
            category="process"
        ))

    # Trend mismatch
    if trend and trade.get("side") == "BUY" and trend == "down":
        causes.append(Cause(
            cause="counter_trend_entry",
            evidence=f"BUY entered in downtrend regime",
            strength=0.8,
            category="process"
        ))
    elif trend and trade.get("side") == "SELL" and trend == "up":
        causes.append(Cause(
            cause="counter_trend_exit",
            evidence=f"SELL in uptrend (may be profit-taking)",
            strength=0.5,
            category="process"
        ))

    return causes


def analyze_sizing(trade: dict) -> list[Cause]:
    """Analyze position sizing."""
    causes = []
    qty_req = trade.get("quantity_requested") or 0
    qty_filled = trade.get("quantity_filled") or qty_req
    entry_price = trade.get("entry_fill_price") or trade.get("limit_price")

    if qty_filled < qty_req * 0.8:
        causes.append(Cause(
            cause="partial_fill",
            evidence=f"Only {qty_filled}/{qty_req} shares filled ({qty_filled/max(qty_req,1)*100:.0f}%)",
            strength=0.5,
            category="outcome"
        ))

    return causes


def analyze_regime_mismatch(trade: dict) -> list[Cause]:
    """Analyze regime mismatch (trend/vol at entry vs exit)."""
    # This would require comparing entry vs exit regime - simplified for now
    return []


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.2f}%"


def analyze_mae_mfe(trade: dict) -> list[Cause]:
    """Analyze MAE/MFE for trade management quality."""
    causes = []
    pnl_pct = trade.get("realized_pnl_pct") or 0
    mae = trade.get("mae") or 0
    mfe = trade.get("mfe") or 0
    exit_reason = trade.get("exit_reason", "")

    # High MAE relative to profit (gave back most gains)
    if pnl_pct > 0 and mfe > 0 and pnl_pct / mfe < 0.5:
        causes.append(Cause(
            cause="gave_back_gains",
            evidence=f"Realized {pnl_pct:.1f}% vs MFE {mfe:.1f}% (kept only {pnl_pct/mfe*100:.0f}% of max gain)",
            strength=min((1 - pnl_pct/mfe) * 2, 1.0),
            category="process"
        ))

    # Stop loss hit but MAE not much worse (good stop placement)
    if exit_reason == "stop" and pnl_pct < 0 and abs(mae) < abs(pnl_pct) * 1.5:
        causes.append(Cause(
            cause="stop_protected_downside",
            evidence=f"Stop limited loss: MAE {mae:.1f}% vs realized {pnl_pct:.1f}%",
            strength=0.7,
            category="good_process"
        ))

    # Target hit efficiently (MFE close to profit)
    if exit_reason == "target" and pnl_pct > 0 and mfe > 0 and pnl_pct / mfe > 0.8:
        causes.append(Cause(
            cause="efficient_target_capture",
            evidence=f"Captured {pnl_pct/mfe*100:.0f}% of max favorable move",
            strength=0.8,
            category="good_process"
        ))

    return causes


def analyze_counterfactuals(trade: dict, broker, entry_time: datetime, exit_time: datetime) -> list[Cause]:
    """
    Analyze counterfactual scenarios: what if we used different stop/target levels?
    This requires intraday data which may not always be available.
    """
    causes = []

    try:
        # Get intraday bars for the trade period
        # This is a placeholder - would need async call to broker
        # For now, use the MAE/MFE data already computed
        mae = trade.get("mae")
        mfe = trade.get("mfe")
        pnl_pct = trade.get("realized_pnl_pct") or 0
        stop_price = trade.get("stop_price")
        target_price = trade.get("target_price")
        entry_price = trade.get("entry_fill_price")

        if not all([mae is not None, mfe is not None, entry_price, stop_price, target_price]):
            return causes

        # Counterfactual 1: What if stop was at MAE (best possible stop)?
        # If MAE > 0 and we got stopped out, a tighter stop would have lost more
        # If MAE was very small, our stop was good
        if trade.get("exit_reason") == "stop" and pnl_pct < 0:
            if abs(mae) < abs(pnl_pct) * 1.2:
                causes.append(Cause(
                    cause="stop_was_efficient",
                    evidence=f"MAE {mae:.1f}% close to realized {pnl_pct:.1f}% - stop placement was efficient",
                    strength=0.7,
                    category="good_process"
                ))
            elif abs(mae) > abs(pnl_pct) * 2:
                causes.append(Cause(
                    cause="stop_too_loose",
                    evidence=f"MAE {mae:.1f}% much worse than realized {pnl_pct:.1f}% - tighter stop would have saved",
                    strength=min((abs(mae) - abs(pnl_pct)) / abs(mae), 1.0),
                    category="process"
                ))

        # Counterfactual 2: What if target was at MFE?
        if trade.get("exit_reason") == "target" and pnl_pct > 0:
            if mfe > pnl_pct * 1.5:
                causes.append(Cause(
                    cause="target_too_conservative",
                    evidence=f"MFE {mfe:.1f}% vs realized {pnl_pct:.1f}% - wider target would have captured more",
                    strength=min((mfe - pnl_pct) / mfe, 1.0),
                    category="process"
                ))
            elif mfe < pnl_pct * 1.2:
                causes.append(Cause(
                    cause="target_efficient",
                    evidence=f"MFE {mfe:.1f}% close to realized {pnl_pct:.1f}% - target placement was efficient",
                    strength=0.7,
                    category="good_process"
                ))

        # Counterfactual 3: Time-based exit analysis
        hold_seconds = trade.get("hold_seconds") or 0
        if hold_seconds > 86400 * 5:  # Held more than 5 days
            if pnl_pct > 0 and mfe > pnl_pct * 2:
                causes.append(Cause(
                    cause="held_too_long_winner",
                    evidence=f"Held {hold_seconds/3600:.0f}h; MFE {mfe:.1f}% vs realized {pnl_pct:.1f}% - earlier exit would be better",
                    strength=0.6,
                    category="process"
                ))
            elif pnl_pct < 0 and abs(mae) > abs(pnl_pct) * 2:
                causes.append(Cause(
                    cause="held_too_long_loser",
                    evidence=f"Held {hold_seconds/3600:.0f}h; MAE {mae:.1f}% vs realized {pnl_pct:.1f}% - earlier stop would save",
                    strength=0.6,
                    category="process"
                ))

    except Exception:
        pass  # Best effort only

    return causes


def run_autopsy(trade: dict, broker=None, entry_time=None, exit_time=None) -> list[Cause]:
    """Run complete deterministic autopsy on a closed trade."""
    all_causes = []

    # Run all evidence analyzers
    all_causes.extend(analyze_slippage(trade))
    all_causes.extend(analyze_gap_risk(trade))
    all_causes.extend(analyze_stop_placement(trade))
    all_causes.extend(analyze_signal_quality(trade))
    all_causes.extend(analyze_sizing(trade))
    all_causes.extend(analyze_regime_mismatch(trade))
    all_causes.extend(analyze_mae_mfe(trade))

    # Add counterfactual analysis if broker and times available
    if broker is not None and entry_time is not None and exit_time is not None:
        all_causes.extend(analyze_counterfactuals(trade, broker, entry_time, exit_time))

    # Sort by strength descending
    all_causes.sort(key=lambda c: c.strength, reverse=True)

    return all_causes


def format_autopsy_for_llm(trade: dict, causes: list[Cause]) -> str:
    """Format autopsy findings for LLM narration (not determination)."""
    pnl = trade.get("realized_pnl_pct", 0)
    outcome = "winning" if pnl > 0 else "losing" if pnl < 0 else "breakeven"

    cause_summaries = []
    for c in causes:
        cat_label = {
            "process": "[PROCESS ISSUE]",
            "good_process": "[GOOD PROCESS]",
            "outcome": "[OUTCOME/LUCK]"
        }.get(c.category, "")
        cause_summaries.append(f"  - {cat_label} {c.cause}: {c.evidence} (strength: {c.strength:.2f})")

    return f"""
Trade Autopsy: {trade.get('symbol')} {trade.get('side')} ({outcome}, {pnl:+.2f}%)

Entry: {trade.get('entry_fill_price', 'N/A')} at {trade.get('entry_fill_time', 'N/A')}
Exit:  {trade.get('exit_fill_price', 'N/A')} at {trade.get('exit_fill_time', 'N/A')} ({trade.get('exit_reason', 'N/A')})
Hold:  {trade.get('hold_seconds', 0)/3600:.1f} hours
MAE:   {_pct(trade.get('mae'))}  MFE: {_pct(trade.get('mfe'))}

Evidence-Based Causes (ranked by strength):
{chr(10).join(cause_summaries)}

Narrate this autopsy for the trader. Explain what the evidence shows about process vs outcome.
Do NOT add causes not listed above. Be concise and factual.
""".strip()
