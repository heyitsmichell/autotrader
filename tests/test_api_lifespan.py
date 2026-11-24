"""Lifespan wiring (contracts section 6): start order, reverse shutdown, db.close last, token file."""

from __future__ import annotations

import dataclasses

from app.api import app as api_app
from app.config import load_settings
from tests.test_api_support import Rec, TOKEN


async def test_lifespan_order(tmp_path, monkeypatch):
    calls: list = []
    token_file = tmp_path / "data" / "api_token"
    settings = dataclasses.replace(load_settings(), api_token_file=token_file,
                                   auto_start_scheduler=False)

    db = Rec("db", calls, broker_mode="mock")
    broker = Rec("broker", calls, connected=True, tools=[], error=None)
    engine = Rec("engine", calls, kill_switch=False, kill_reason=None)
    feed = Rec("feed", calls, error=None, mode="poll")
    ml = Rec("ml", calls, last_error=None)
    research = Rec("research", calls, auto_symbols=[])
    alerts = Rec("alerts", calls, channels=[])
    parts = {
        "DB": db, "Broker": broker, "Engine": engine, "MarketFeed": feed, "MLFilter": ml,
        "ResearchService": research, "Alerts": alerts,
        "SentimentEngine": Rec("sentiment", calls), "SignalService": Rec("signals", calls),
        "Portfolio": Rec("portfolio", calls), "FastLoop": Rec("fastloop", calls),
        "Learner": Rec("learner", calls), "Supervisor": Rec("supervisor", calls),
        "YahooFinanceService": Rec("yf", calls),
    }
    for name, obj in parts.items():
        monkeypatch.setattr(api_app, name, lambda *a, _o=obj, **k: _o)
    monkeypatch.setattr(api_app, "load_settings", lambda: settings)
    monkeypatch.setattr(api_app, "_file_logging", lambda: None)

    app = api_app.create_app(token=TOKEN)
    async with app.router.lifespan_context(app):
        assert token_file.read_text() == TOKEN
        assert app.state.db is db and app.state.engine is engine
        started = [c for c in calls if c != "db.log"]
    names = [c for c in calls if c != "db.log"]

    def order(seq):
        idx = [names.index(x) for x in seq]
        assert idx == sorted(idx), (seq, names)

    order(["broker.start", "engine.startup_reconcile", "engine.start_workers",
           "engine.start_risk_loop", "feed.start", "learner.start", "supervisor.start"])
    order(["engine.start_risk_loop", "engine.stop_risk_loop", "engine.stop_workers",
           "supervisor.stop", "engine.stop_scheduler", "research.stop", "fastloop.stop",
           "signals.stop", "portfolio.stop", "learner.stop", "feed.stop", "sentiment.aclose",
           "broker.stop", "db.close"])
    assert names[-1] == "db.close"
    assert "engine.stop_risk_loop" not in started          # not stopped while running


async def test_shutdown_continues_when_a_step_fails(tmp_path, monkeypatch):
    calls: list = []
    settings = dataclasses.replace(load_settings(), api_token_file=tmp_path / "tok")

    class Boom(Rec):
        async def stop_workers(self):
            raise RuntimeError("boom")

    db = Rec("db", calls, broker_mode="mock")
    engine = Boom("engine", calls, kill_switch=False, kill_reason=None)
    parts = {
        "DB": db, "Broker": Rec("broker", calls, connected=False, tools=[], error=None),
        "Engine": engine, "MarketFeed": Rec("feed", calls, error=None, mode="poll"),
        "MLFilter": Rec("ml", calls, last_error=None), "ResearchService": Rec("research", calls),
        "Alerts": Rec("alerts", calls, channels=[]), "SentimentEngine": Rec("sentiment", calls),
        "SignalService": Rec("signals", calls), "Portfolio": Rec("portfolio", calls),
        "FastLoop": Rec("fastloop", calls), "Learner": Rec("learner", calls),
        "Supervisor": Rec("supervisor", calls), "YahooFinanceService": Rec("yf", calls),
    }
    for name, obj in parts.items():
        monkeypatch.setattr(api_app, name, lambda *a, _o=obj, **k: _o)
    monkeypatch.setattr(api_app, "load_settings", lambda: settings)
    monkeypatch.setattr(api_app, "_file_logging", lambda: None)
    app = api_app.create_app(token=TOKEN)
    async with app.router.lifespan_context(app):
        pass
    assert "broker.stop" in calls and calls[-1] == "db.close"


def test_main_shim_exposes_app_and_run():
    import app.main as m
    assert m.app.title == "AI Trader" and callable(m.run) and callable(m._acquire_lock)
