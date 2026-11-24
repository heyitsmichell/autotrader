"""GET /api/status and the shared status snapshot builder (also used by the SSE publisher)."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request

from ..config import DATA_DIR, WEB_DIR
from ..market import market_is_open, now_et, now_utc, session_info
from .deps import today_start
from .models import StatusOut

router = APIRouter()

DAY_PNL_TTL = 5.0


def read_duplicate_flag() -> dict | None:
    """Read the duplicate instance flag file if it exists."""
    flag_file = DATA_DIR / "duplicate_instance.flag"
    if flag_file.exists():
        try:
            content = flag_file.read_text().strip()
            if content:
                parts = content.split(":", 1)
                if len(parts) == 2:
                    return {"timestamp": int(parts[0]), "message": parts[1]}
        except Exception:  # noqa: BLE001
            pass
    return None


async def _day_pnl(st) -> float | None:
    """broker.day_pnl(), cached for DAY_PNL_TTL seconds. Never raises."""
    cache = getattr(st, "_day_pnl_cache", None)
    now = time.monotonic()
    if cache and now - cache[0] < DAY_PNL_TTL:
        return cache[1]
    pnl = None
    try:
        if st.broker.connected:
            pnl = await asyncio.wait_for(st.broker.day_pnl(), 3.0)
    except Exception:  # noqa: BLE001 - the guard display must never fail /api/status
        pnl = None
    if pnl is None:
        try:
            cached = st.portfolio.cached
            pnl = cached["balance"].get("day_pnl") if cached else None
        except Exception:  # noqa: BLE001
            pnl = None
    st._day_pnl_cache = (now, pnl)
    return pnl


async def daily_loss_guard(st) -> dict:
    try:
        limit = float(getattr(st.settings, "max_daily_loss_usd", 0) or 0)
        pnl = await _day_pnl(st)
    except Exception:  # noqa: BLE001
        return {"enabled": False, "tripped": False, "pnl": None, "limit": None}
    enabled = limit > 0
    return {"enabled": enabled, "tripped": bool(enabled and pnl is not None and pnl <= -limit),
            "pnl": pnl, "limit": limit}


def data_age_seconds(st) -> float | None:
    """Age of the newest quote or portfolio read."""
    stamps: list[float] = []
    try:
        at = getattr(st.portfolio, "_cache_at", None)
        if at and st.portfolio.cached:
            stamps.append(float(at))
    except Exception:  # noqa: BLE001
        pass
    try:
        for sym in st.settings.watchlist:
            q = st.feed.quote(sym)
            if q is not None and getattr(q, "as_of", None):
                stamps.append(float(q.as_of))
    except Exception:  # noqa: BLE001
        pass
    return round(max(0.0, time.time() - max(stamps)), 1) if stamps else None


async def build_status(st) -> dict:
    s, broker, engine = st.settings, st.broker, st.engine
    s = getattr(engine, "s", None) or s   # includes lesson-applied values
    pnl = None
    cached = st.portfolio.cached
    if cached:
        pnl = cached["balance"].get("day_pnl")
    usage = st.db.usage_since(today_start())
    return {
        "broker_label": s.broker_label,
        "broker_mode": s.broker_mode,
        "is_live": s.is_live,
        "connected": broker.connected,
        "broker_error": broker.error,
        "trading_mode": engine.trading_mode,
        "allow_live_auto": s.allow_live_auto,
        "kill_switch": engine.kill_switch,
        "scheduler_on": engine.scheduler_on,
        "next_run_at": engine.next_run_at.isoformat() if engine.next_run_at else None,
        "running": engine.running,
        "market_open": market_is_open(),
        "now_et": now_et().strftime("%a %H:%M ET"),
        "server_time": now_utc().isoformat(timespec="seconds"),
        "provider": ("Groq → Groq alternate → NVIDIA NIM"
                     if s.nvidia_nim_api_key else "Groq → Groq alternate"),
        "model": s.llm_model,
        "watchlist": s.watchlist,
        "cycle_minutes": s.cycle_minutes,
        "limits": {
            "max_order_usd": s.max_order_usd, "max_position_usd": s.max_position_usd,
            "max_order_qty": s.max_order_qty, "max_trades_per_day": s.max_trades_per_day,
            "max_daily_loss_usd": s.max_daily_loss_usd, "price_band_pct": s.price_band_pct,
            "proposal_ttl_minutes": s.proposal_ttl_minutes,
            "market_hours_only": s.market_hours_only,
            "max_risk_per_trade_usd": s.max_risk_per_trade_usd,
        },
        "consensus": {
            "ta_min": s.consensus_ta_min, "sent_min": s.consensus_sent_min,
            "min_news": s.consensus_min_news, "ml_min_p": s.ml_min_p, "ml_enabled": s.ml_enabled,
            "sl_atr_mult": s.sl_atr_mult, "tp_atr_mult": s.tp_atr_mult,
            "bracket_orders": s.bracket_orders,
        },
        "sentiment": {"source": s.sentiment_source, "llm": s.sentiment_llm,
                      "scoring": st.sentiment.scoring, "last_scoring": st.sentiment.last_scoring},
        "signals": {"refreshing": st.signals.refreshing, "error": st.signals.error,
                    "last_refresh": st.signals.last_refresh.isoformat(timespec="seconds")
                    if st.signals.last_refresh else None,
                    "refresh_minutes": s.signal_refresh_minutes},
        "research": {"last_refresh": st.research.last_refresh,
                     "refreshing": st.research.refreshing,
                     "candidate_count": sum(p["packet_type"] == "candidate"
                                             for p in st.db.research_packets(100)),
                     "candidate_cap": 10,
                     "approval_only": not (s.auto_trade_research_candidates
                                           and engine.trading_mode == "auto"),
                     "scan_interval_minutes": s.research_scan_minutes,
                     "auto_candidate_trading": s.auto_trade_research_candidates,
                     "auto_candidate_execution": bool(s.auto_trade_research_candidates
                                                       and engine.trading_mode == "auto"),
                     "manual_watchlist": st.research.manual_symbols,
                     "auto_watchlist": st.research.auto_symbols},
        "ml": {"available": st.ml.available, "training": st.ml.training,
               "active_id": st.ml.model_id, "enabled": s.ml_enabled},
        "trades_today": st.db.count_placed_today(),
        "day_pnl": pnl,
        "usage_today": usage,
        "cost_today_usd": round(sum(u["cost_usd"] or 0 for u in usage), 4),
        "realtime": {"mode": st.feed.mode, "error": st.feed.error,
                     "poll_seconds": s.poll_seconds},
        "web_built": WEB_DIR.exists(),
        "session": session_info(),
        "settings": {
            "options_enabled": s.options_enabled,
            "options_allowed_strategies": s.options_allowed_strategies,
            "options_allowed_underlyings": s.options_allowed_underlyings,
            "options_max_contracts_per_order": s.options_max_contracts_per_order,
            "options_max_notional_usd": s.options_max_notional_usd,
            "options_min_dte": s.options_min_dte,
            "options_max_dte": s.options_max_dte,
            "options_require_approval": s.options_require_approval,
            "options_iv_rank_max": s.options_iv_rank_max,
            "options_iv_rank_min": s.options_iv_rank_min,
            "options_delta_min": s.options_delta_min,
            "options_delta_max": s.options_delta_max,
            "options_min_volume": s.options_min_volume,
            "options_min_open_interest": s.options_min_open_interest,
            "options_max_bid_ask_pct": s.options_max_bid_ask_pct,
        },
        "flattening": engine.flattening,
        "kill_reason": engine.kill_reason,
        "supervisor": st.supervisor.status(),
        "alerts": st.alerts.status(),
        "duplicate_instance": read_duplicate_flag(),
        # contract section 7 additions
        "data_age_seconds": data_age_seconds(st),
        "broker_mode_warning": getattr(broker, "mode_warning", None),
        "paused": bool((not engine.scheduler_on) and (not engine.kill_switch)),
        "daily_loss_guard": await daily_loss_guard(st),
        "token_required": True,
    }


@router.get("/api/status", response_model=StatusOut)
async def status(request: Request):
    return await build_status(request.app.state)
