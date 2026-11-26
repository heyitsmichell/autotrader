"""Research packets, Yahoo Finance lookups and signal / sentiment refresh actions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .deps import background
from .models import RefreshIn

router = APIRouter()


def _yf(request: Request):
    yf = getattr(request.app.state, "yfinance", None)
    if yf is None:
        from ..yfinance_service import YahooFinanceService
        yf = request.app.state.yfinance = YahooFinanceService(request.app.state.settings)
    return yf


@router.get("/api/research")
def research_packets(request: Request, limit: int = 100):
    st = request.app.state
    auto_execution = bool(st.settings.auto_trade_research_candidates
                          and st.engine.trading_mode == "auto")
    return {"last_refresh": st.research.last_refresh, "refreshing": st.research.refreshing,
            "errors": st.research.errors, "approval_only": not auto_execution,
            "scan_interval_minutes": st.settings.research_scan_minutes,
            "auto_candidate_trading": st.settings.auto_trade_research_candidates,
            "auto_candidate_execution": auto_execution,
            "candidate_cap": 10, "manual_watchlist": st.research.manual_symbols,
            "auto_watchlist": st.research.auto_symbols,
            "auto_eligibility": {"required_screener_overlap": True,
                                 "minimum_price_usd": 5,
                                 "minimum_daily_volume": 250_000},
            "packets": st.db.research_packets(min(max(limit, 1), 200))}


@router.get("/api/yfinance/macro/context")
async def yfinance_macro(request: Request):
    """Yahoo Finance macroeconomic and index indicators (VIX, S&P500, Nasdaq, 10Y Treasury)."""
    return await _yf(request).get_macro_context()


@router.get("/api/yfinance/{symbol}")
async def yfinance_summary(request: Request, symbol: str):
    """Yahoo Finance fundamental analysis and analyst targets."""
    return await _yf(request).get_summary(symbol)


@router.post("/api/signals/refresh")
async def refresh_signals(request: Request, body: RefreshIn):
    st = request.app.state
    if not st.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    if st.signals.refreshing:
        raise HTTPException(409, "Signals are already refreshing")

    async def go():
        try:
            await st.signals.refresh(with_llm=body.with_llm)
        except Exception as e:  # noqa: BLE001
            st.db.log("error", f"Signal refresh failed: {e}")

    background(request, go())
    return {"started": True}


@router.post("/api/research/refresh")
async def refresh_research(request: Request):
    st = request.app.state
    if not st.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    if st.research.refreshing:
        return {"started": False, "refreshing": True}
    background(request, st.research.refresh())
    auto_execution = bool(st.settings.auto_trade_research_candidates
                          and st.engine.trading_mode == "auto")
    return {"started": True, "approval_only": not auto_execution,
            "auto_candidate_execution": auto_execution}


@router.post("/api/sentiment/score")
async def score_sentiment(request: Request):
    """Score unscored headlines with Groq now, then refresh signals."""
    st = request.app.state
    if st.sentiment.scoring:
        raise HTTPException(409, "Headline scoring is already running")

    async def go():
        try:
            await st.sentiment.score_with_llm()
            await st.signals.refresh(with_llm=False)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            st.db.log("error", f"Groq headline scoring failed: {err}")

    background(request, go())
    return {"started": True}
