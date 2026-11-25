"""Portfolio, signals, trade ledger, autopsies, performance and activity read endpoints."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request

from ..db import decode
from ..execution_quality import add_missed_opportunities, build_execution_quality
from ..market import now_utc, order_session
from ..performance import build_performance
from .models import TradeRow

router = APIRouter()


def protection_states(positions, open_trades, protective_rows, session, settings, kv_get) -> dict:
    """Per-held-symbol stop protection, mirroring risk_loop._soft_stops semantics (pure)."""
    soft_on = bool(getattr(settings, "extended_hours_soft_stop", False))
    night_ok = bool(getattr(settings, "overnight_trading", False))
    night_syms = set(getattr(settings, "overnight_symbols", None) or [])
    trades: dict[str, list[dict]] = {}
    for t in open_trades or []:
        if (t.get("side") == "BUY" and t.get("entry_fill_price") is not None
                and t.get("exit_fill_price") is None):
            trades.setdefault(t.get("symbol"), []).append(t)
    rows: dict[str, list[dict]] = {}
    for r in protective_rows or []:
        # protective_orders stores the stop leg as kind "sl" (the target leg is "tp")
        if str(r.get("kind") or "sl").lower() in ("sl", "stop", "stop_loss"):
            rows.setdefault(r.get("symbol"), []).append(r)
    out: dict[str, dict] = {}
    for p in positions or []:
        sym = p.get("symbol")
        if not sym or sym in out:
            continue
        ts = trades.get(sym, [])
        with_stop = [t for t in ts if t.get("stop_price")]
        stop = (max(float(t["stop_price"]) for t in with_stop) if with_stop
                else next((float(r["price"]) for r in rows.get(sym, []) if r.get("price")), None))
        if any(kv_get(f"soft_stop:{t['id']}") for t in ts):
            out[sym] = {"state": "soft_fired", "stop": stop,
                        "detail": "app soft stop already fired; exit order sent"}
        elif not with_stop and not rows.get(sym):
            out[sym] = {"state": "missing", "stop": None, "detail": "no stop on record"}
        elif session == "CORE":
            if rows.get(sym):
                out[sym] = {"state": "broker", "stop": stop, "detail": "resting stop at the broker"}
            else:
                out[sym] = {"state": "missing", "stop": stop,
                            "detail": "no resting stop at the broker"}
        elif session == "ALL" or (session == "NIGHT" and night_ok and sym in night_syms):
            if soft_on and with_stop:
                out[sym] = {"state": "soft", "stop": stop,
                            "detail": "app watches the stop until 09:30 ET; broker stop resumes at the open"}
            else:
                out[sym] = {"state": "inactive", "stop": stop,
                            "detail": "broker stop only works 09:30-16:00 ET"}
        else:
            out[sym] = {"state": "closed", "stop": stop,
                        "detail": "market closed for this symbol; broker stop resumes at 09:30 ET"}
    return out


async def build_portfolio(st) -> dict:
    """Shared by GET /api/portfolio and the SSE publisher. Raises HTTPException(409) if offline."""
    if not st.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    snap = await st.portfolio.snapshot()
    protective = st.db.protective(open_only=True)
    session = order_session()
    try:
        prot = protection_states(snap.get("positions") or [], st.db.open_trades(), protective,
                                 session, st.settings, st.db.get_kv)
    except Exception:  # noqa: BLE001
        prot = {}
    return {**snap, "equity": st.portfolio.equity_today(), "protective": protective,
            "protection": prot, "session": session}


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
