"""Assemble the account-vs-benchmark performance report from stored daily rows.

Pure math lives in ``app/metrics.py``; this module only reads the database and
formats the result for ``/api/performance`` and the dashboard. Return figures
are percentages (12.3 = 12.3%); ratios such as Sharpe and beta are plain
numbers. Every figure comes from live daily closes, never from a backtest.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from . import metrics
from .db import DB
from .market import ET


def _pct(value: float | None) -> float | None:
    return None if value is None else round(value * 100.0, 4)


def _ratio(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _day_of(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date()


def _daily_notional(db: DB, rows: list[dict], days: list[date]) -> list[float]:
    """Absolute traded notional per session from the trade ledger."""
    by_day = {d: 0.0 for d in days}
    if not rows:
        return []
    try:
        trades = db.benchmark_trades(rows[0]["day"], rows[-1]["day"])
    except Exception:  # noqa: BLE001 - turnover is optional, never fail the report
        return []
    for t in trades:
        qty = float(t.get("quantity_filled") or t.get("quantity_requested") or 0)
        entry_day = _day_of(t.get("entry_fill_time"))
        if entry_day in by_day:
            by_day[entry_day] += abs(qty * float(t.get("entry_fill_price") or 0))
        exit_day = _day_of(t.get("exit_fill_time"))
        if exit_day in by_day:
            by_day[exit_day] += abs(qty * float(t.get("exit_fill_price") or 0))
    # The return series has one fewer observation than the value series.
    return [by_day[d] for d in days[1:]]


def build_performance(db: DB, *, benchmark_symbol: str = "SPY",
                      min_days: int = 60, risk_free_rate: float = 0.0) -> dict:
    rows = [r for r in db.benchmark_daily()
            if r.get("net_liq") is not None and r.get("spy_close") is not None]
    base = {
        "source": "live",
        "method": ("time-weighted daily returns; external flows are estimated from the "
                   "broker's day P&L at the close snapshot"),
        "benchmark_symbol": benchmark_symbol,
        "min_days": min_days,
        "risk_free_rate_pct": round(risk_free_rate * 100.0, 3),
        "as_of": rows[-1]["day"] if rows else None,
    }
    if len(rows) < 2:
        return {
            **base,
            "state": "no_data",
            "days": max(0, len(rows) - 1),
            "enough_data": False,
            "message": ("No completed daily rows yet. The app stores one row per "
                        "session after the close; check again after the next close."),
            "series": [],
            "significance": {
                "state": "insufficient_data",
                "n": max(0, len(rows) - 1),
                "min_days": min_days,
                "days_needed": max(0, min_days - (len(rows) - 1)),
                "message": "Not enough data yet.",
            },
        }

    # Consecutive stored sessions, oldest first.
    values = [float(r["net_liq"]) for r in rows]
    flows = [float(r.get("flow") or 0.0) for r in rows]
    spy = [float(r["spy_close"]) for r in rows]
    days = [date.fromisoformat(r["day"]) for r in rows]
    summary = metrics.summarize(
        values, flows, spy,
        turnover_notional=_daily_notional(db, rows, days),
        risk_free=risk_free_rate,
        min_days=min_days,
    )

    sig = summary["significance"]
    significance = {
        "state": sig["state"],
        "n": sig["n"],
        "min_days": sig["min_days"],
        "confidence": sig.get("confidence"),
        "lags": sig.get("lags"),
        "message": sig.get("message"),
        "p_value": _ratio(sig.get("p_value")),
        "t_stat": _ratio(sig.get("t_stat")),
        "significant": sig.get("significant"),
        "mean_daily_pct": _pct(sig.get("mean_daily")),
        "mean_annualized_pct": _pct(sig.get("mean_annualized")),
        "ci_low_annualized_pct": _pct(sig.get("ci_low_annualized")),
        "ci_high_annualized_pct": _pct(sig.get("ci_high_annualized")),
        "days_needed": sig.get("days_needed"),
    }

    # Cumulative index for a small dashboard chart (both start at 1.0).
    account_returns = metrics.daily_returns(values, flows)
    spy_returns = metrics.returns_from_prices(spy)
    n = min(len(account_returns), len(spy_returns))
    account_index, spy_index = 1.0, 1.0
    series = []
    for i in range(n):
        account_index *= 1.0 + account_returns[i]
        spy_index *= 1.0 + spy_returns[i]
        series.append({
            "day": days[i + 1].isoformat(),
            "account": round(account_index, 6),
            "benchmark": round(spy_index, 6),
        })

    a, b = summary["account"], summary["benchmark"]
    return {
        **base,
        "state": "ok",
        "days": summary["days"],
        "start": rows[0]["day"],
        "end": rows[-1]["day"],
        "enough_data": significance["state"] == "ok",
        "first_net_liq": values[0],
        "last_net_liq": values[-1],
        "flows_total": round(sum(flows), 2),
        "flows_detected": any(abs(f) >= 0.01 for f in flows),
        "account": {
            "total_return_pct": _pct(a["total_return"]),
            "annualized_return_pct": _pct(a["annualized_return"]),
            "volatility_pct": _pct(a["volatility"]),
            "max_drawdown_pct": _pct(a["max_drawdown"]),
            "sharpe": _ratio(a["sharpe"]),
            "beta": _ratio(a["beta"]),
            "alpha_pct": _pct(a["alpha"]),
        },
        "benchmark": {
            "total_return_pct": _pct(b["total_return"]),
            "annualized_return_pct": _pct(b["annualized_return"]),
            "volatility_pct": _pct(b["volatility"]),
            "max_drawdown_pct": _pct(b["max_drawdown"]),
            "sharpe": _ratio(b["sharpe"]),
        },
        "excess_return_pct": _pct(summary["excess_return"]),
        "tracking_error_pct": _pct(summary["tracking_error"]),
        "information_ratio": _ratio(summary["information_ratio"]),
        "hit_rate": _ratio(summary["hit_rate"]),
        "turnover": _ratio(summary["turnover"]),
        "significance": significance,
        "series": series,
    }
