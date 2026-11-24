"""Portfolio, signals, trade ledger, autopsies, performance and activity read endpoints."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request

from ..db import decode
from ..execution_quality import add_missed_opportunities, build_execution_quality
from ..market import now_utc
from ..performance import build_performance
from .models import TradeRow

router = APIRouter()


async def build_portfolio(st) -> dict:
    """Shared by GET /api/portfolio and the SSE publisher. Raises HTTPException(409) if offline."""
    if not st.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    snap = await st.portfolio.snapshot()
    return {**snap, "equity": st.portfolio.equity_today(),
            "protective": st.db.protective(open_only=True)}


@router.get("/api/portfolio")
async def portfolio(request: Request):
    return await build_portfolio(request.app.state)


@router.get("/api/signals")
async def signals(request: Request):
    return [decode(r) for r in request.app.state.db.latest_signals()]


@router.get("/api/fastloop")
async def fastloop_status(request: Request):
    return request.app.state.fastloop.status()


@router.get("/api/signals/{symbol}")
async def signal_history(request: Request, symbol: str, limit: int = 50):
    return [decode(r) for r in request.app.state.db.signals(symbol.upper(), min(limit, 500))]


@router.get("/api/news")
async def news(request: Request, symbol: str | None = None, limit: int = 30):
    since = (now_utc() - timedelta(hours=request.app.state.settings.news_lookback_hours)
             ).isoformat(timespec="seconds")
    return request.app.state.db.news(symbol.upper() if symbol else None, since, min(limit, 200))


@router.get("/api/sentiment")
async def sentiment(request: Request):
    st = request.app.state
    return {"fear_greed": st.sentiment.fear_greed, "source": st.settings.sentiment_source,
            "llm": st.settings.sentiment_llm, "scoring": st.sentiment.scoring,
            "symbols": {sym: st.sentiment.summary(sym) for sym in st.settings.watchlist}}


@router.get("/api/runs")
async def runs(request: Request, limit: int = 10):
    return request.app.state.db.runs(min(limit, 100))


@router.get("/api/events")
async def events(request: Request, limit: int = 100):
    return request.app.state.db.events(min(limit, 500))


@router.get("/api/execution-quality")
async def execution_quality(request: Request):
    """Signal->order->fill delays, slippage, fill rate and what unfilled orders did afterwards."""
    st = request.app.state
    report = build_execution_quality(st.db)
    try:
        report["missed_opportunities"] = await add_missed_opportunities(st.broker, report["unfilled"])
    except Exception as e:  # noqa: BLE001
        report["missed_opportunities"] = []
        report["missed_error"] = str(e)[:120]
    return report


@router.get("/api/performance")
async def performance(request: Request):
    """Account vs the benchmark (SPY) on live daily closes, with significance."""
    s = request.app.state.settings
    return build_performance(
        request.app.state.db,
        benchmark_symbol=s.performance_benchmark_symbol,
        min_days=s.performance_min_days,
        risk_free_rate=s.performance_risk_free_rate,
    )


# -- autopsy & trade ledger ------------------------------------------------------------
@router.get("/api/autopsies")
async def autopsies(request: Request, limit: int = 50, symbol: str | None = None):
    return request.app.state.db.autopsies(min(limit, 200), symbol.upper() if symbol else None)


@router.get("/api/autopsies/{trade_id}")
async def autopsy_detail(request: Request, trade_id: int):
    autopsy = request.app.state.db.get_autopsy(trade_id)
    if autopsy is None:
        raise HTTPException(404, f"Autopsy for trade #{trade_id} not found")
    return autopsy


def trade_row(row: dict) -> dict:
    """Normalise a trade_ledger row (JSON columns parsed) into the TradeRow shape."""
    return TradeRow(**decode(row)).model_dump()


@router.get("/api/trades", response_model=list[TradeRow])
async def trades(request: Request, limit: int = 50, status: str | None = None,
                 symbol: str | None = None):
    """status=open|closed|all (default all). Every filter returns the same TradeRow shape (L11)."""
    db = request.app.state.db
    want = status if status in ("open", "closed", "cancelled") else None
    cap = 1000 if want == "open" else min(max(limit, 1), 200)   # open trades were never capped
    rows = db.trades(cap, symbol.upper() if symbol else None, want)
    return [trade_row(r) for r in rows]


@router.get("/api/trades/{proposal_id}")
async def trade_detail(request: Request, proposal_id: int):
    trade = request.app.state.db.get_trade(proposal_id)
    if trade is None:
        raise HTTPException(404, f"Trade for proposal #{proposal_id} not found")
    return decode(trade)
