"""Execution quality: how fast and how well signals turn into fills.

Answers "did the bot miss the window?" with numbers, from rows already in the database:
signal -> proposal -> order -> fill delays, slippage, fill rate (split by what triggered the
order: the slow cycle or the fast loop), and what unfilled orders would have done afterwards.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    try:
        return (b - a).total_seconds()
    except TypeError:  # naive vs aware
        return (b.replace(tzinfo=None) - a.replace(tzinfo=None)).total_seconds()


def _stats(vals: list[float]) -> dict:
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "median": None, "p90": None, "mean": None}
    s = sorted(vals)
    p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
    return {"n": len(s), "median": round(statistics.median(s), 3), "p90": round(p90, 3),
            "mean": round(statistics.fmean(s), 3)}


def summarize(proposals: list[dict], trades: dict[int, dict], signal_times: dict[int, str],
              run_triggers: dict[int, str]) -> dict:
    """Aggregate BUY proposals into delay / slippage / fill-rate statistics."""
    buys = [p for p in proposals if p.get("side") == "BUY"]
    placed = [p for p in buys if p.get("status") == "placed"]
    rows: list[dict] = []
    for p in placed:
        t = trades.get(p["id"])
        filled = bool(t and (t.get("quantity_filled") or 0) > 0 and t.get("entry_fill_price"))
        created, placed_at = _ts(p.get("created_at")), _ts(p.get("placed_at"))
        sig_at = _ts(signal_times.get(p.get("signal_id"))) if p.get("signal_id") else None
        fill_at = _ts(t.get("entry_fill_time")) if filled else None
        slip_pct = None
        if filled and p.get("ref_price"):
            slip_pct = (t["entry_fill_price"] - p["ref_price"]) / p["ref_price"] * 100
        rows.append({
            "id": p["id"], "symbol": p["symbol"], "created_at": p.get("created_at"),
            "source": "fast_loop" if run_triggers.get(p.get("run_id")) == "fast_loop" else "cycle",
            "decided_by": p.get("decided_by"), "order_type": p.get("order_type") or "limit",
            "filled": filled, "limit_price": p.get("limit_price"),
            "signal_to_proposal_s": _secs(sig_at, created),
            "proposal_to_order_s": _secs(created, placed_at),
            "order_to_fill_s": _secs(placed_at, fill_at),
            "slippage_vs_limit": (t["entry_fill_price"] - p["limit_price"]) if filled else None,
            "slippage_vs_ref_pct": slip_pct,
        })

    def block(sub: list[dict]) -> dict:
        n_f = sum(1 for r in sub if r["filled"])
        return {
            "orders": len(sub), "filled": n_f,
            "fill_rate": round(n_f / len(sub), 3) if sub else None,
            "signal_to_proposal_s": _stats([r["signal_to_proposal_s"] for r in sub]),
            "auto_proposal_to_order_s": _stats([r["proposal_to_order_s"] for r in sub
                                                if r["decided_by"] == "auto"]),
            "order_to_fill_s": _stats([r["order_to_fill_s"] for r in sub if r["filled"]]),
            "slippage_vs_ref_pct": _stats([r["slippage_vs_ref_pct"] for r in sub if r["filled"]]),
        }

    return {
        "proposals": {"buy_total": len(buys),
                      "rejected": sum(1 for p in buys if p.get("status") == "rejected"),
                      "pending": sum(1 for p in buys if p.get("status") == "pending"),
                      "placed": len(placed)},
        "overall": block(rows),
        "by_source": {src: block([r for r in rows if r["source"] == src])
                      for src in ("cycle", "fast_loop") if any(r["source"] == src for r in rows)},
        # slippage and fill rate per order type: limit | marketable | market | chase
        "by_order_type": {ot: block([r for r in rows if r["order_type"] == ot])
                          for ot in ("limit", "marketable", "market", "chase")
                          if any(r["order_type"] == ot for r in rows)},
        "unfilled": [r for r in rows if not r["filled"]],
        "note": ("Slippage is fill price minus the reference price at proposal time (negative is better). "
                 "Orders with no fill are DAY limits that expired or were cancelled."),
    }


def forward_returns(bars: list[dict], limit_price: float, proposal_day, horizons=(1, 3, 5)) -> dict:
    """Return from the limit price to the close N trading days after the proposal day."""
    from .broker import bar_day
    after = [b for b in bars if isinstance(b.get("time"), datetime) and bar_day(b["time"]) > proposal_day]
    out: dict[str, float | None] = {}
    for h in horizons:
        out[f"ret_{h}d_pct"] = (round((after[h - 1]["close"] / limit_price - 1) * 100, 3)
                                if len(after) >= h and limit_price > 0 else None)
    return out


async def add_missed_opportunities(broker, unfilled: list[dict], limit: int = 15) -> list[dict]:
    """For unfilled orders, what the price did afterwards (was it a winner we missed?)."""
    out = []
    for r in unfilled[:limit]:
        created = _ts(r.get("created_at"))
        entry = dict(r)
        try:
            bars = await broker.daily_bars(r["symbol"], 30)
            entry.update(forward_returns(bars, float(r["limit_price"]), created.date() if created else None))
            r5 = entry.get("ret_5d_pct")
            entry["missed_winner_5d"] = None if r5 is None else r5 > 0
        except Exception as e:  # noqa: BLE001 - one symbol failing must not break the report
            entry["error"] = str(e)[:120]
        out.append(entry)
    return out


def build_execution_quality(db, limit: int = 300) -> dict:
    proposals = db.proposals(limit)
    trades, signal_times, run_triggers = {}, {}, {}
    for p in proposals:
        if p.get("status") != "placed":
            continue
        t = db.get_trade(p["id"])
        if t:
            trades[p["id"]] = t
        sid = p.get("signal_id")
        if sid and sid not in signal_times:
            sig = db.get_signal(sid)
            if sig:
                signal_times[sid] = sig["created_at"]
    for r in db.runs(500):
        run_triggers[r["id"]] = r.get("trigger")
    return summarize(proposals, trades, signal_times, run_triggers)
