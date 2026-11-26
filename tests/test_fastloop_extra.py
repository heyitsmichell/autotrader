import asyncio
from types import SimpleNamespace
import pytest
import app.fastloop as F
from app.risk import working_buy_symbols, working_buys
from app.fastloop import FastLoop

def _buy(price, qty=1):
    return dict(symbol="AAPL", side="BUY", quantity=qty, limit_price=price,
                confidence=0.5, rationale="test")


async def _pending(engine, mock_broker):
    price = await mock_broker.last_price("AAPL")
    run = engine.db.create_run("test")
    r = await engine._on_proposal(run, _buy(price))
    assert r.startswith("ACCEPTED"), r
    return run, price


@pytest.mark.asyncio
@pytest.mark.slow
async def test_auto_execute_run_places_in_auto_mode(test_engine, mock_broker, bullish_signal):
    run, _ = await _pending(test_engine, mock_broker)
    test_engine.trading_mode = "auto"
    await test_engine._auto_execute_run(run)
    p = test_engine.db.proposals(limit=1)[0]
    assert p["status"] != "pending" and p["decided_by"] == "auto"


@pytest.mark.asyncio
async def test_auto_execute_run_does_nothing_in_approve_mode(test_engine, mock_broker, bullish_signal):
    run, _ = await _pending(test_engine, mock_broker)
    test_engine.trading_mode = "approve"
    await test_engine._auto_execute_run(run)
    assert test_engine.db.proposals(limit=1)[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_auto_execute_run_blocked_by_kill_switch(test_engine, mock_broker, bullish_signal):
    run, _ = await _pending(test_engine, mock_broker)
    test_engine.trading_mode = "auto"
    test_engine.kill_switch = True
    await test_engine._auto_execute_run(run)
    p = test_engine.db.proposals(limit=1)[0]
    assert p["status"] == "rejected" and "kill switch" in p["reason"]


def _fast_loop(monkeypatch, order):
    async def on_proposal(run_id, p, sig_override=None):
        order.append("proposal")
        return "ok"

    async def auto(run_id):
        order.append(("auto", run_id))
    eng = SimpleNamespace(kill_switch=False, _run_lock=asyncio.Lock(), research=None,
                          _on_proposal=on_proposal, _auto_execute_run=auto)
    fl = F.FastLoop.__new__(F.FastLoop)
    fl.engine, fl.broker = eng, SimpleNamespace(connected=True)
    fl.s = SimpleNamespace(fast_loop_cooldown_minutes=5, ml_min_p=0.5)
    fl.db = SimpleNamespace(create_run=lambda *a: 7, finish_run=lambda *a, **k: None,
                            log=lambda *a, **k: None)
    fl._cooldown_until, fl._sell_cooldown_until, fl.triggers = {}, {}, 0
    fl.signals = SimpleNamespace(cached_bars=lambda *a: [{"close": 1.0}],
                                 ml=SimpleNamespace(predict=lambda f: 0.1))
    monkeypatch.setattr(F, "with_live_price", lambda bars, price, day: bars)
    monkeypatch.setattr(F, "indicators", SimpleNamespace(features=lambda b, bench=None: {"close": 1.0}, ml_features=lambda b, bench=None: {"close": 1.0}))
    monkeypatch.setattr(F, "ta", SimpleNamespace(analyze=lambda f, s: {
        "close": 1.0, "atr": 0.1, "trend": "up", "strength": 1, "vol": 1, "ta_score": 0.9,
        "ta_components": {}}))
    monkeypatch.setattr(F, "consensus", SimpleNamespace(evaluate=lambda p, sig, s: {"ok": True}))
    return fl


@pytest.mark.asyncio
async def test_fastloop_buy_and_exit_auto_execute_after_proposal(monkeypatch):
    order = []
    fl = _fast_loop(monkeypatch, order)
    assert await fl._check("AAPL", 10.0, {"symbol": "AAPL"})
    assert order == ["proposal", ("auto", 7)]
    order.clear()
    assert await fl._check_exit("AAPL", 10.0, 3.0, {"symbol": "AAPL"})
    assert order == ["proposal", ("auto", 7)]


@pytest.mark.asyncio
async def test_fastloop_proposal_stays_pending_in_approve_mode(test_engine, mock_broker, bullish_signal, monkeypatch):
    """Real engine helper: approve mode leaves what the fast loop proposed waiting for the user."""
    run, _ = await _pending(test_engine, mock_broker)
    test_engine.trading_mode = "approve"
    order = []
    fl = _fast_loop(monkeypatch, order)
    fl.engine._auto_execute_run = test_engine._auto_execute_run
    fl.engine._on_proposal = lambda *a, **k: asyncio.sleep(0, "ok")
    fl.db = SimpleNamespace(create_run=lambda *a: run, finish_run=lambda *a, **k: None,
                            log=lambda *a, **k: None, pending_for_run=test_engine.db.pending_for_run)
    test_engine.db.pending_for_run  # noqa: B018
    await fl._check("AAPL", 10.0, {"symbol": "AAPL"})
    assert test_engine.db.proposals(limit=1)[0]["status"] == "pending"


def _placed_unfilled(db, pid, symbol="AAPL"):
    db.add_trade_entry(pid, dict(symbol=symbol, side="BUY", quantity=4, limit_price=100.0))


@pytest.mark.asyncio
async def test_working_buy_helpers(test_engine, mock_broker, bullish_signal):
    db = test_engine.db
    assert working_buy_symbols(db) == set()
    _placed_unfilled(db, 101)
    assert working_buy_symbols(db) == {"AAPL"}
    assert working_buys(db, "AAPL", exclude_pid=101) == []
    db.update_trade_fill(101, 100.0, "2026-01-01T00:00:00", 4)
    assert working_buy_symbols(db) == set()       # filled: a held position, not working


@pytest.mark.asyncio
async def test_fastloop_tick_does_not_rearm_symbol_with_working_buy(test_engine, mock_broker, bullish_signal, monkeypatch):
    _placed_unfilled(test_engine.db, 101)
    seen = {}

    def select(rows, held, pending, cooling, s):
        seen["pending"] = set(pending)
        return []
    monkeypatch.setattr(F, "select_armed", select)
    fl = F.FastLoop(test_engine.s, mock_broker, test_engine.db, test_engine, test_engine.signals)
    monkeypatch.setattr(fl, "_session_open", lambda: True)
    await fl.tick()
    assert "AAPL" in seen["pending"]


@pytest.mark.asyncio
async def test_risk_rejects_second_buy_while_first_unfilled(test_engine, mock_broker, bullish_signal):
    price = await mock_broker.last_price("AAPL")
    _placed_unfilled(test_engine.db, 101)
    r = await test_engine._on_proposal(test_engine.db.create_run("t"), _buy(price))
    assert r.startswith("REJECTED") and "Working entry" in r and "#101" in r


@pytest.mark.asyncio
async def test_risk_does_not_block_the_proposals_own_execution(test_engine, mock_broker, bullish_signal):
    run, _ = await _pending(test_engine, mock_broker)
    pid = test_engine.db.proposals(limit=1)[0]["id"]
    _placed_unfilled(test_engine.db, pid)         # its own ledger row (e.g. re-check at execution)
    p = test_engine.db.get_proposal(pid)
    v = await test_engine.risk.check(p, mock_broker, test_engine.db, kill_switch=False,
                                     at_execution=True, watchlist=test_engine.s.watchlist)
    assert not any(c["name"] == "Working entry" for c in v.checks if not c["ok"])


def _fl():
    return SimpleNamespace(_reject_logged={}, _REJECT_LOG_EVERY_S=FastLoop._REJECT_LOG_EVERY_S)


def test_gap_reject_logged_once_per_30_min():
    fl = _fl()
    chk = lambda r, t: FastLoop._should_log_result(fl, "TXN", r, now=t)
    r1 = "REJECTED (eligibility): Gap filter: TXN gapped 5.20% (prev close $100.00, last $105.20, max 3%)"
    r2 = "REJECTED (eligibility): Gap filter: TXN gapped 5.30% (prev close $100.00, last $105.30, max 3%)"
    assert chk(r1, 0)
    assert not chk(r2, 60)             # same reason, drifting price
    assert chk("REJECTED (eligibility): First 15 minutes", 120)   # reason changed
    assert chk(r1, 1900)               # >30 min later
    assert FastLoop._should_log_result(fl, "TXN", "OK proposal", now=1901)   # non-rejects always log
