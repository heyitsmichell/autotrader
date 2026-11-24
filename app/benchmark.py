"""Daily benchmark recording (Phase 0).

Stores one row per completed US session: account net liquidation and cash at
the close snapshot, the benchmark (SPY) close, and an estimated external cash
flow. The performance API reads these rows; nothing here places orders and no
risk setting is touched.

Cash flows are estimated as ``net_liq_t - net_liq_{t-1} - day_pnl_t`` where
``day_pnl`` is the broker's own P&L at the close snapshot. That is a good
approximation when the app is running near the close; it is explicitly not
perfect, so the API reports the method and the total estimated flow.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from .broker import bar_day
from .config import Settings
from .db import DB
from .market import CLOSE, ET, now_et, session_close

log = logging.getLogger(__name__)

# A gap larger than this many calendar days means we cannot attribute the
# broker's single-day P&L to the whole gap, so the flow estimate is withheld.
MAX_FLOW_GAP_DAYS = 5
# Equity snapshots are pruned after 35 days (see Portfolio._loop).
SNAPSHOT_LOOKBACK_DAYS = 40


def _et_day(ts: str) -> date | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date()


def _snapshot_at_close(rows: list[dict], day: date) -> dict | None:
    """The equity snapshot closest to that session's close (ET)."""
    close_time = session_close(day) or CLOSE
    target = datetime.combine(day, close_time, ET)
    best, best_gap = None, None
    for row in rows:
        if _et_day(row["ts"]) != day:
            continue
        try:
            ts = datetime.fromisoformat(row["ts"])
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        gap = abs((ts.astimezone(ET) - target).total_seconds())
        if best is None or gap < best_gap:
            best, best_gap = row, gap
    return best


def _completed_bars(bars: list[dict], now: datetime) -> list[tuple[date, dict]]:
    """Bars for sessions that have finished by ``now`` (ET)."""
    today = now.date()
    close = session_close(today)
    today_done = close is not None and now.time() >= close
    out: list[tuple[date, dict]] = []
    for bar in bars:
        try:
            day = bar_day(bar["time"])
        except (KeyError, TypeError):
            continue
        if day < today or (today_done and day == today):
            out.append((day, bar))
    out.sort(key=lambda pair: pair[0])
    return out


def _estimate_flow(cur: dict, prev_net: float | None, prev_day: date | None,
                   gap_ok_days: int = MAX_FLOW_GAP_DAYS) -> float:
    net, pnl = cur.get("net_liq"), cur.get("day_pnl")
    if net is None or pnl is None or prev_net is None:
        return 0.0
    if prev_day is not None and (date.fromisoformat(cur["_day"]) - prev_day).days > gap_ok_days:
        return 0.0
    flow = float(net) - float(prev_net) - float(pnl)
    return 0.0 if abs(flow) < 0.01 else round(flow, 2)


async def record_daily_benchmark(db: DB, broker, settings: Settings, *,
                                 bars: list[dict] | None = None,
                                 now: datetime | None = None) -> dict | None:
    """Store any newly completed sessions. Returns the latest stored row, if any.

    ``bars`` and ``now`` are injectable for tests. The broker is only called
    when ``bars`` is omitted, and the parent (Supervisor) throttles that call.
    """
    now = (now or now_et()).astimezone(ET)
    symbol = settings.performance_benchmark_symbol
    if bars is None:
        bars = await broker.daily_bars(symbol, settings.performance_history_days)
    completed = _completed_bars(bars or [], now)
    if not completed:
        return None

    since = (now.astimezone(timezone.utc) - timedelta(days=SNAPSHOT_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    try:
        snapshots = db.equity_full(since)
    except Exception as e:  # noqa: BLE001 - measurement must not break the supervisor
        log.warning("Could not read equity snapshots for the daily benchmark: %s", e)
        return None
    if not snapshots:
        return None

    first_day = completed[0][0]
    existing = {row["day"] for row in db.benchmark_daily(since=first_day.isoformat())}
    prev_net: float | None = None
    prev_day: date | None = None
    latest: dict | None = None

    for day, bar in completed:
        snap = _snapshot_at_close(snapshots, day)
        if snap is None or snap.get("net_liq") is None:
            continue
        row = {
            "_day": day.isoformat(),
            "net_liq": snap.get("net_liq"),
            "cash": snap.get("cash"),
            "market_value": snap.get("market_value"),
            "day_pnl": snap.get("day_pnl"),
        }
        flow = _estimate_flow(row, prev_net, prev_day)
        db.upsert_benchmark_daily(
            day.isoformat(),
            net_liq=row["net_liq"],
            cash=row["cash"],
            market_value=row["market_value"],
            spy_close=bar.get("close"),
            day_pnl=row["day_pnl"],
            flow=flow,
        )
        prev_net, prev_day = row["net_liq"], day
        latest = {"day": day.isoformat(), "net_liq": row["net_liq"],
                  "spy_close": bar.get("close"), "flow": flow,
                  "created": day.isoformat() not in existing}

    if latest and latest["created"]:
        close = latest["spy_close"]
        close_text = f"{close:,.2f}" if close is not None else "n/a"
        db.log("info", f"Daily benchmark stored for {latest['day']}: "
                       f"net liq ${latest['net_liq']:,.2f}, {symbol} {close_text}")
    return latest
