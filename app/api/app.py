"""FastAPI app factory: lifespan, auth middleware, routers and static dashboard serving."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..alerts import Alerts
from ..broker import Broker, BrokerError
from ..config import DATA_DIR, WEB_DIR, load_settings
from ..db import DB
from ..engine import Engine
from ..fastloop import FastLoop
from ..feed import MarketFeed
from ..learning import Learner
from ..logging_setup import setup_logging
from ..ml import MLFilter
from ..portfolio import Portfolio
from ..research import ResearchService
from ..sentiment import SentimentEngine
from ..signals import SignalService
from ..supervisor import Supervisor
from ..yfinance_service import YahooFinanceService
from . import auth, backtests_view, chart, controls, experiments, learning, ml, options, portfolio, proposals, research, promotion, research_views, results, risk, shadows, sleeves, status, stream
from .auth import install_auth, new_token, write_token_file

setup_logging(files=False)          # console logging + redaction as early as import time
log = logging.getLogger("trader")


def _file_logging() -> None:
    """Rotating log files (see app/logging_setup.py)."""
    setup_logging()


async def _safe(db, what: str, aw) -> None:
    """Await a shutdown step; one failing step must not skip the rest (db.close must run)."""
    try:
        await aw
    except Exception as e:  # noqa: BLE001
        log.warning("shutdown: %s failed: %s", what, e)
        try:
            db.log("warn", f"Shutdown step '{what}' failed: {e}")
        except Exception:  # noqa: BLE001
            pass


async def _startup(app: FastAPI, stack: AsyncExitStack) -> None:
    _file_logging()
    settings = load_settings()
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("HOST=%s is not loopback: this app has no login and can place trades "
                    "(run() refuses to start with it).", settings.host)
    write_token_file(settings.api_token_file, app.state.api_token)
    db = DB(DATA_DIR / "trader.db", settings.broker_mode, settings.webull_environment, settings.webull_account_id)

    async def _close_db():
        try:
            db.close()
        except Exception as e:  # noqa: BLE001
            log.warning("shutdown: db.close failed: %s", e)

    done: set[str] = set()

    def reg(what, fn):
        async def _step():
            if what in done:        # registered twice (early failsafe + ordered); run once
                return
            done.add(what)
            await _safe(db, what, fn())
        stack.push_async_callback(_step)

    stack.push_async_callback(_close_db)
    orphaned = db.close_orphan_runs()
    if orphaned:
        db.log("warn", f"Closed {orphaned} run(s) left 'running' by a previous crash")
    alerts = Alerts(settings, db)
    broker = Broker(settings)
    sentiment = SentimentEngine(settings, broker, db)
    ml_filter = MLFilter(settings, db)
    research_svc = ResearchService(settings, broker, db)
    signals = SignalService(settings, broker, db, sentiment, ml_filter, research_svc)
    portfolio_svc = Portfolio(settings, db, broker)
    engine = Engine(settings, db, broker, signals, portfolio_svc, alerts)
    engine.on_order = portfolio_svc.invalidate
    feed = MarketFeed(settings, broker)
    fastloop = FastLoop(settings, broker, db, engine, signals, feed)
    learner = Learner(settings, broker, db, ml_filter, research_svc)
    engine.research = research_svc
    supervisor = Supervisor(settings, db, broker, engine, portfolio_svc, alerts, learner)
    st = app.state
    st.yfinance = YahooFinanceService(settings)
    st.alerts, st.supervisor = alerts, supervisor
    st.settings, st.db, st.broker, st.engine, st.feed, st.learner = (
        settings, db, broker, engine, feed, learner)
    st.sentiment, st.ml, st.signals, st.portfolio = sentiment, ml_filter, signals, portfolio_svc
    st.research = research_svc
    st.fastloop = fastloop
    st.tasks = set()
    reg("broker", broker.stop)
    reg("sentiment", sentiment.aclose)

    async def _cancel_tasks():
        for task in list(st.tasks):
            task.cancel()
    reg("tasks", _cancel_tasks)
    db.log("info", f"App started. Broker: {settings.broker_label}. Alerts: "
                   f"{', '.join(alerts.channels) or 'none configured'}", alert=True)
    if engine.kill_switch:
        db.log("warn", f"Kill switch is still on from before the restart: {engine.kill_reason}",
               alert=True)
    try:
        await broker.start()
        db.log("info", f"Connected to MCP server ({len(broker.tools)} tools)")
    except BrokerError as e:
        db.log("error", str(e))
    # Contract section 6: reconcile first, then the cold-path workers, then the risk loop.
    try:
        if await engine.startup_reconcile():
            portfolio_svc.invalidate()
    except Exception as e:  # noqa: BLE001 - never block startup; the risk loop still protects positions
        db.log("error", f"Startup reconcile failed: {e}")
    reg("workers", engine.stop_workers)
    await engine.start_workers()
    reg("risk loop", engine.stop_risk_loop)
    await engine.start_risk_loop()
    if broker.connected:
        # Throttle startup research scan: skip if last scan was recent (within 10 minutes)
        try:
            last_refresh = db.get_kv("research_last_refresh")
            should_scan = True
            if last_refresh:
                try:
                    last_time = datetime.fromisoformat(last_refresh)
                    if datetime.now(timezone.utc) - last_time < timedelta(minutes=10):
                        should_scan = False
                        db.log("info", f"Skipping startup research scan: last scan was at {last_refresh} (within 10 min)")
                except Exception:  # noqa: BLE001
                    pass
            if should_scan:
                try:
                    await research_svc.refresh_if_due(force=True)
                    db.log("info", f"Initial automated research scan complete: "
                           f"{len(research_svc.auto_symbols)} eligible equity candidate(s)")
                except Exception as e:  # noqa: BLE001 - research outage must not stop app startup
                    db.log("warn", f"Initial automated research scan failed: {e}")
            else:
                db.log("info", "Startup research scan skipped (recent scan exists)")
        except BrokerError as e:
            db.log("error", str(e))
    supervisor.check_token(force=True)
    reg("feed", feed.stop)
    await feed.start()
    db.log("warn" if feed.error else "info",
           feed.error or f"Live prices: {feed.mode} "
                         f"({'push' if feed.mode == 'stream' else f'every {settings.poll_seconds:g}s'})")
    if ml_filter.last_error:
        db.log("warn", ml_filter.last_error)
    reg("learner", learner.stop)
    learner.start()
    reg("portfolio", portfolio_svc.stop)
    portfolio_svc.start()
    reg("signals", signals.stop)
    signals.start()
    reg("fastloop", fastloop.stop)
    fastloop.start()
    reg("research", research_svc.stop)
    research_svc.start()
    reg("scheduler", engine.stop_scheduler)
    if settings.auto_start_scheduler and broker.connected and not engine.kill_switch:
        engine.start_scheduler()
    reg("supervisor", supervisor.stop)
    supervisor.start()
    # Re-register so a normal shutdown stops the risk loop/workers first (original order);
    # the early registrations above remain as failsafes and are skipped once run.
    reg("workers", engine.stop_workers)
    reg("risk loop", engine.stop_risk_loop)
    publisher = asyncio.create_task(stream.publisher_loop(app))
    reg("publisher", lambda: _cancel_and_wait(publisher))
    if settings.broker_mode != "mock":     # warm the slow Yahoo caches behind the Research tab
        from .. import research_views as _rv
        warm = asyncio.create_task(_rv.warm_cache(30.0, every=_rv.CACHE_SECONDS - 300))
        reg("research warm-up", lambda: _cancel_and_wait(warm))


async def _cancel_and_wait(task):
    task.cancel()
    await _await_cancelled(task)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup steps register their own teardown on an AsyncExitStack (LIFO), so a failure
    mid-startup still shuts down everything that already started."""
    async with AsyncExitStack() as stack:
        await _startup(app, stack)
        yield


async def _await_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass


def inject_token(page: str, token: str) -> str:
    """Insert the per-launch token meta tag before </head> (or at the top if there is none)."""
    tag = f'<meta name="trader-token" content="{html.escape(token, quote=True)}">'
    new, n = re.subn(r"</head>", lambda m: tag + "</head>", page, count=1, flags=re.IGNORECASE)
    return new if n else tag + page


class TokenStaticFiles(StaticFiles):
    """StaticFiles that injects the API token into every served .html file."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        fpath = getattr(resp, "path", None)
        if isinstance(resp, FileResponse) and fpath and str(fpath).lower().endswith(".html") \
                and resp.status_code != 304:
            token = getattr(scope["app"].state, "api_token", "")
            page = Path(fpath).read_text(encoding="utf-8")
            return HTMLResponse(inject_token(page, token), status_code=resp.status_code,
                                headers={"Cache-Control": "no-store"})
        return resp


def create_app(lifespan=lifespan, web_dir: Path | None = None, token: str | None = None) -> FastAPI:
    app = FastAPI(title="AI Trader", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.api_token = token or new_token()
    try:
        trusted = load_settings().trusted_hosts
    except Exception:  # noqa: BLE001 - never let a settings problem widen access
        trusted = []
    install_auth(app, trusted)
    for r in (auth.router, status.router, portfolio.router, proposals.router, options.router,
              learning.router, ml.router, research.router, controls.router, chart.router,
              stream.router, experiments.router, sleeves.router, shadows.router, results.router,
              research_views.router, backtests_view.router, promotion.router, risk.router):
        app.include_router(r)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
    async def api_not_found(path: str):
        raise HTTPException(404, f"no API route /api/{path}")

    web = Path(web_dir) if web_dir is not None else WEB_DIR
    if web.exists():
        app.mount("/", TokenStaticFiles(directory=web, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        async def not_built():
            return ("<!doctype html><meta charset=utf-8><title>AI Trader</title>"
                    "<body style='font:15px system-ui;background:#0b0e13;color:#e6e9ee;padding:40px'>"
                    "<h2>The dashboard isn't built yet</h2><p>Run <code>npm install</code> and "
                    "<code>npm run build</code> in the <code>web</code> folder (or run "
                    "<code>setup.sh</code>), then restart. The API is running.</p>")
    return app
