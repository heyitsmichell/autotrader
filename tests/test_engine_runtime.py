"""Wave 2A-1: engine runtime and options safety (QA H1, H2, H3, M1, M3, M4, M7, M9).

Covers: kill check scope, client_order_id pre-persist, duplicate handling, the fail-closed option
gate, IV rank, option exit triggers (frozen clock), the OptionExitManager in approve mode and
under the kill switch, RiskLoop resilience, SignalStore TTL, orphan adoption, lesson apply/skip,
the daily prune schedule and the fast loop's kill-switch re-check.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import datetime, timedelta

import pytest

from app.broker import BrokerError
from app.engine import (PROPOSAL_STATUSES, HeldOption, evaluate_exit, iv_rank, normalize_iv,
                        parse_held_options)
from app.market import ET
from app.signal_store import SignalStore

OPT = "AAPL261218C00150000"       # AAPL 2026-12-18 150 call


def _settings(engine, **kw):
    engine.s = dataclasses.replace(engine.s, **kw)
    return engine.s


def _snap(**kw):
    d = {"bid": 2.00, "ask": 2.10, "last": 2.05, "as_of": time.time(), "iv": 0.30,
         "delta": 0.50, "volume": 5000, "open_interest": 10000}
    d.update(kw)
    return d


def _fake_snapshots(engine, monkeypatch, snap):
    async def snaps(symbols):
        return {s: snap for s in symbols} if snap is not None else {}
    monkeypatch.setattr(engine.broker, "parsed_option_snapshots", snaps)

    async def no_atm(_u):
        return None
    monkeypatch.setattr(engine, "record_atm_iv", no_atm)


def _check(gate, name):
    return next(c for c in gate["checks"] if c["name"].startswith(name))


# -- statuses -------------------------------------------------------------------------------

def test_proposal_statuses_exact_set():
    assert PROPOSAL_STATUSES == {"pending", "submitting", "placed", "rejected", "dismissed",
                                 "failed", "unknown", "orphan"}


# -- IV rank (H3) ---------------------------------------------------------------------------

def test_normalize_iv_accepts_fraction_and_percent():
    assert normalize_iv(0.3) == pytest.approx(0.3)
    assert normalize_iv(30) == pytest.approx(0.3)
    assert normalize_iv(None) is None and normalize_iv(0) is None and normalize_iv("x") is None
    assert normalize_iv(float("nan")) is None


def test_iv_rank_and_percentile():
    series = [0.2, 0.3, 0.4, 0.5, 0.6]
    rank, pct = iv_rank(series, 0.4)
    assert rank == pytest.approx(0.5) and pct == pytest.approx(0.6)
    assert iv_rank(series, 0.9)[0] == 1.0 and iv_rank(series, 0.1)[0] == 0.0
    assert iv_rank([], 0.3) is None and iv_rank([0.3, 0.3], 0.3) is None


async def test_iv_check_raw_fallback_is_labelled(test_engine):
    checks = []
    test_engine._iv_check(lambda name, ok, detail, **k: checks.append((name, ok, detail)),
                          "AAPL", 0.30, True)
    name, ok, detail = checks[0]
    need = test_engine.s.options_iv_rank_min_history
    assert name == f"IV (raw, insufficient history 0/{need})"
    assert "raw IV" in detail


async def test_iv_check_true_rank_with_history(test_engine):
    _settings(test_engine, options_iv_rank_min_history=10, options_iv_rank_max=0.5)
    base = datetime(2026, 1, 1)
    for i in range(20):            # 0.20 .. 0.39
        test_engine.db.record_atm_iv("AAPL", (base + timedelta(days=i)).date().isoformat(),
                                     0.20 + i * 0.01)
    checks = []
    add = lambda name, ok, detail, **k: checks.append((name, ok, detail))  # noqa: E731
    test_engine._iv_check(add, "AAPL", 0.38, True)      # high in its range: fails a long entry
    test_engine._iv_check(add, "AAPL", 0.22, True)      # low in its range: passes
    assert checks[0][0] == "IV Rank (long)" and checks[0][1] is False
    assert checks[1][1] is True and "20d ATM IV history" in checks[1][2]


# -- fail-closed option gate (H2/M7) --------------------------------------------------------

async def test_gate_passes_on_a_clean_fresh_quote(test_engine, monkeypatch):
    _fake_snapshots(test_engine, monkeypatch, _snap())
    gate = await test_engine._option_consensus_gate({"symbol": OPT, "position_intent": "BUY_TO_OPEN"})
    assert gate["ok"], gate["checks"]


@pytest.mark.parametrize("snap,check,needle", [
    (None, "Option quote", "failing closed"),
    (_snap(as_of=0), "Quote age", "no as_of"),
    (_snap(as_of=time.time() - 3600), "Quote age", ">"),
    (_snap(bid=None), "Bid-Ask", "missing"),
    (_snap(bid=0.0), "Bid-Ask", "zero"),
    (_snap(bid=2.20, ask=2.10), "Bid-Ask", "crossed or locked"),
    (_snap(bid=2.10, ask=2.10), "Bid-Ask", "crossed or locked"),
    (_snap(bid=1.00, ask=2.00), "Bid-Ask", ">"),
    (_snap(iv=None), "IV", "failing closed"),
    (_snap(delta=None), "Delta", "failing closed"),
    (_snap(volume=None), "Volume", "failing closed"),
    (_snap(open_interest=None), "Open Interest", "failing closed"),
])
async def test_gate_fails_closed(test_engine, monkeypatch, snap, check, needle):
    _fake_snapshots(test_engine, monkeypatch, snap)
    gate = await test_engine._option_consensus_gate({"symbol": OPT, "position_intent": "BUY_TO_OPEN"})
    assert not gate["ok"]
    c = _check(gate, check)
    assert not c["ok"] and needle in c["detail"]


async def test_gate_fails_closed_when_the_snapshot_call_raises(test_engine, monkeypatch):
    async def boom(symbols):
        raise BrokerError("down", transport=True)
    monkeypatch.setattr(test_engine.broker, "parsed_option_snapshots", boom)

    async def no_atm(_u):
        return None
    monkeypatch.setattr(test_engine, "record_atm_iv", no_atm)
    gate = await test_engine._option_consensus_gate({"symbol": OPT})
    assert not gate["ok"] and "failing closed" in _check(gate, "Option quote")["detail"]


async def test_gate_stale_cutoff_follows_setting(test_engine, monkeypatch):
    _settings(test_engine, options_quote_max_age_seconds=60.0)
    _fake_snapshots(test_engine, monkeypatch, _snap(as_of=time.time() - 30))
    gate = await test_engine._option_consensus_gate({"symbol": OPT})
    assert _check(gate, "Quote age")["ok"]
    _settings(test_engine, options_quote_max_age_seconds=10.0)
    gate = await test_engine._option_consensus_gate({"symbol": OPT})
    assert not _check(gate, "Quote age")["ok"]


# -- exit triggers (H1), frozen clock -------------------------------------------------------

def _et(y, m, d, hh=11, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


@pytest.fixture
def exit_settings(test_settings):
    return dataclasses.replace(test_settings, options_exit_stop_pct=0.5,
                               options_exit_take_profit_pct=0.5, options_exit_dte=2,
                               options_expiry_close_time="15:45")


def test_exit_stop_and_take_profit(exit_settings):
    now = _et(2026, 11, 24)
    assert evaluate_exit(exit_settings, entry=2.0, mark=1.0, expiration="2026-12-18",
                         now=now)[0] == "stop"
    assert evaluate_exit(exit_settings, entry=2.0, mark=1.01, expiration="2026-12-18",
                         now=now) is None
    assert evaluate_exit(exit_settings, entry=2.0, mark=3.0, expiration="2026-12-18",
                         now=now)[0] == "take_profit"


def test_take_profit_needs_a_fresh_mark_but_stop_does_not(exit_settings):
    now = _et(2026, 11, 24)
    assert evaluate_exit(exit_settings, entry=2.0, mark=3.0, expiration="2026-12-18", now=now,
                         fresh_mark=False) is None
    assert evaluate_exit(exit_settings, entry=2.0, mark=0.9, expiration="2026-12-18", now=now,
                         fresh_mark=False)[0] == "stop"


def test_dte_and_expiry_day_close(exit_settings):
    assert evaluate_exit(exit_settings, entry=None, mark=None, expiration="2026-11-26",
                         now=_et(2026, 11, 24))[0] == "dte"
    assert evaluate_exit(exit_settings, entry=None, mark=None, expiration="2026-11-27",
                         now=_et(2026, 11, 24)) is None
    assert evaluate_exit(exit_settings, entry=None, mark=None, expiration="2026-11-24",
                         now=_et(2026, 11, 24, 15, 44))[0] == "dte"
    assert evaluate_exit(exit_settings, entry=None, mark=None, expiration="2026-11-24",
                         now=_et(2026, 11, 24, 15, 45))[0] == "expiry_close"
    # expiry close outranks a simultaneous stop
    assert evaluate_exit(exit_settings, entry=2.0, mark=0.5, expiration="2026-11-24",
                         now=_et(2026, 11, 24, 15, 50))[0] == "expiry_close"


def test_parse_held_options_ignores_equities():
    text = f"Symbol: AAPL Quantity: 10\nSymbol: {OPT} Quantity: 2 Cost: 2.00 Last: 1.10\n"
    rows = parse_held_options(text)
    assert all(r.symbol != "AAPL" for r in rows)


# -- OptionExitManager: acts in approve mode and under the kill switch ----------------------

@pytest.fixture
def held_option(test_engine, monkeypatch):
    mgr = test_engine.option_exits
    pos = HeldOption(OPT, 2.0, cost=2.0, mark=0.9)

    async def held():
        return [pos]
    monkeypatch.setattr(mgr, "held_options", held)

    async def quotes(symbols):
        return {}                      # stale/missing quotes must not block a stop
    monkeypatch.setattr(mgr, "quotes", quotes)
    monkeypatch.setattr("app.engine.option_exits.market_is_open", lambda *a: True)
    sent = []

    async def place(sym, side, qty, otype, strike, exp, limit, coid, **kw):
        row = test_engine.db.open_proposals(sym, "SELL")
        sent.append({"sym": sym, "side": side, "qty": qty, "limit": limit, "coid": coid,
                     "allow_when_killed": kw.get("allow_when_killed"),
                     "persisted_coid": row[0]["client_order_id"] if row else None})
        return "Order placed"
    monkeypatch.setattr(test_engine.broker, "place_option_single_order", place)

    async def preview(*a, **k):
        return "preview ok"
    monkeypatch.setattr(test_engine.broker, "preview_option_order", preview)
    return pos, sent


async def test_exit_manager_sells_to_close_under_kill_switch_in_approve_mode(test_engine, held_option):
    _, sent = held_option
    test_engine.trading_mode = "approve"
    await test_engine.set_kill_switch(True, "test")
    actions = await test_engine.option_exits.run_once()
    assert len(actions) == 1 and actions[0]["trigger"] == "stop"
    assert actions[0]["status"] == "placed"
    assert sent and sent[0]["side"] == "SELL" and sent[0]["qty"] == 2
    assert sent[0]["allow_when_killed"] is True
    # M3/item 3: the coid was on the row before the broker saw the order
    assert sent[0]["persisted_coid"] == sent[0]["coid"]
    row = test_engine.db.get_proposal(actions[0]["proposal_id"])
    assert row["position_intent"] == "SELL_TO_CLOSE" and row["decided_by"] == "risk_loop"


async def test_exit_manager_one_exit_in_flight_per_contract(test_engine, held_option):
    _, sent = held_option
    first = await test_engine.option_exits.run_once()
    test_engine.option_exits._last_attempt.clear()     # skip the retry back-off
    second = await test_engine.option_exits.run_once()
    assert len(first) == 1 and second == [] and len(sent) == 1


async def test_exit_manager_does_nothing_outside_the_session(test_engine, held_option, monkeypatch):
    monkeypatch.setattr("app.engine.option_exits.market_is_open", lambda *a: False)
    assert await test_engine.option_exits.run_once() == []


# -- RiskLoop ------------------------------------------------------------------------------

async def test_risk_loop_survives_errors_and_never_takes_run_lock(test_engine, monkeypatch):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return []
    monkeypatch.setattr(test_engine.option_exits, "run_once", flaky)
    _settings(test_engine, options_enabled=True, risk_loop_seconds=0.25)
    loop = test_engine.risk_loop
    async with test_engine._run_lock:          # a long slow cycle must not stall exits
        await test_engine.start_risk_loop()
        await asyncio.sleep(0.8)
        assert loop.running
    await test_engine.stop_risk_loop()
    assert calls["n"] >= 2 and loop.errors == 1 and "boom" in (loop.status()["last_error"] or "boom")
    assert not loop.running


# -- SignalStore TTL (M4) ------------------------------------------------------------------

def test_signal_store_ttl():
    now = [1000.0]
    st = SignalStore(ttl_seconds=60, clock=lambda: now[0])
    st.put_many([{"symbol": "aapl", "x": 1}, {"symbol": "MSFT"}, {}])
    assert len(st) == 2 and st.get("AAPL")["x"] == 1
    now[0] += 59
    assert st.get("AAPL") is not None and st.get("AAPL", max_age=30) is None
    now[0] += 2
    assert st.get("AAPL") is None and st.missing(["AAPL", "MSFT"]) == ["AAPL", "MSFT"]
    assert st.snapshot() == [] and st.newest_age() == pytest.approx(61)


# -- kill switch scope (M1) ----------------------------------------------------------------

async def test_kill_blocks_opening_order_at_execution_before_the_broker(test_engine, monkeypatch):
    called = []

    async def place(*a, **k):
        called.append(a)
        return "placed"
    monkeypatch.setattr(test_engine.broker, "place_order", place)
    pid = test_engine.db.add_proposal(None, {"symbol": "AAPL", "side": "BUY", "quantity": 1,
                                             "limit_price": 100.0}, "pending")
    test_engine.kill_switch = True       # flipped after the proposal was made
    out = await test_engine.execute(pid, "user")
    assert out["status"] == "rejected" and "kill switch" in out["reason"]
    assert called == []


def test_is_opening_classification(test_engine):
    op = test_engine._is_opening
    assert op({"symbol": "AAPL", "side": "BUY"}) and not op({"symbol": "AAPL", "side": "SELL"})
    assert op({"symbol": OPT, "side": "BUY", "position_intent": "BUY_TO_OPEN"})
    assert not op({"symbol": OPT, "side": "SELL", "position_intent": "SELL_TO_CLOSE"})
    assert not op({"symbol": OPT, "side": "BUY", "position_intent": "BUY_TO_CLOSE"})


async def test_kill_switch_publishes_event(test_engine):
    from app.eventhub import hub
    q = hub.subscribe()
    try:
        await test_engine.set_kill_switch(True, "why")
        evs = []
        while not q.empty():
            evs.append(q.get_nowait())
        kill = [e for e in evs if e.type == "kill"]
        assert kill and kill[-1].data["on"] is True and kill[-1].data["reason"] == "why"
        assert kill[-1].data["at"]
    finally:
        hub.unsubscribe(q)


# -- coid pre-persist, duplicate handling (M3) ---------------------------------------------

async def test_option_coid_persisted_before_send_and_reused(test_engine, monkeypatch):
    seen = []

    async def preview(*a, **k):
        return "ok"

    async def place(sym, side, qty, otype, strike, exp, limit, coid, **kw):
        seen.append((coid, test_engine.db.get_proposal(pid)["client_order_id"]))
        raise BrokerError("timeout", transport=True)
    monkeypatch.setattr(test_engine.broker, "preview_option_order", preview)
    monkeypatch.setattr(test_engine.broker, "place_option_single_order", place)
    p = {"symbol": OPT, "side": "SELL", "quantity": 1, "limit_price": 1.0}
    pid = test_engine.db.add_proposal(None, p, "submitting", asset_class="option",
                                      option_type="CALL", strike_price=150.0,
                                      expiration_date="2026-12-18",
                                      position_intent="SELL_TO_CLOSE")
    row = test_engine.db.get_proposal(pid)
    await test_engine._send(pid, row, "user", "test")
    row = test_engine.db.get_proposal(pid)
    assert row["status"] == "unknown"
    coid, persisted = seen[0]
    assert coid == persisted == row["client_order_id"]
    # a re-send of the same row reuses the coid
    test_engine.db.update_proposal(pid, status="submitting")
    await test_engine._send(pid, test_engine.db.get_proposal(pid), "user", "retry")
    assert seen[1][0] == coid


async def test_duplicate_open_proposal_is_a_skip_not_a_crash(test_engine, mock_broker):
    from app.db import DuplicateProposalError
    price = await mock_broker.last_price("AAPL")
    test_engine.db.add_proposal(None, {"symbol": "AAPL", "side": "BUY", "quantity": 1,
                                       "limit_price": price}, "pending")
    with pytest.raises(DuplicateProposalError):
        test_engine.db.add_proposal(None, {"symbol": "AAPL", "side": "BUY", "quantity": 1,
                                           "limit_price": price}, "pending")
    run_id = test_engine.db.create_run("test")
    r = await test_engine._on_proposal(run_id, dict(symbol="AAPL", side="BUY", quantity=1,
                                                    limit_price=price, confidence=0.5,
                                                    rationale="t"))
    assert r.startswith("REJECTED") and "duplicate" in r.lower()


async def test_on_proposal_is_serialised(test_engine, monkeypatch):
    """Two concurrent callers (slow cycle + fast loop) never interleave inside _on_proposal."""
    active, peak = [0], [0]

    async def body(run_id, args, sig_override=None):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.02)
        active[0] -= 1
        return "ok"
    monkeypatch.setattr(test_engine, "_on_proposal_locked", body)
    await asyncio.gather(*(test_engine._on_proposal(1, {}) for _ in range(5)))
    assert peak[0] == 1


async def test_on_proposal_catches_duplicate_error(test_engine, monkeypatch):
    from app.db import DuplicateProposalError

    async def body(run_id, args, sig_override=None):
        raise DuplicateProposalError("open row exists")
    monkeypatch.setattr(test_engine, "_on_proposal_locked", body)
    r = await test_engine._on_proposal(1, {})
    assert r.startswith("REJECTED")


# -- startup reconcile: orphan adoption ----------------------------------------------------

def _order_text(coid, status="WORKING", sym="TSLA"):
    return "\n".join([f"Client Order ID: {coid}", f"  Client Order ID: {coid}",
                      f"  Order ID: O{coid}", f"  Symbol: {sym}", "  Side: BUY",
                      f"  Status: {status}", "  Total Quantity: 3", "  Limit Price: 99.5"]) + "\n"


async def test_startup_reconcile_adopts_orphans_once(test_engine, monkeypatch):
    async def open_orders():
        return _order_text("EXT1") + _order_text("EXT2", status="FILLED")
    monkeypatch.setattr(test_engine.broker, "open_orders_text", open_orders)
    summary = await test_engine.startup_reconcile()
    assert summary["orphans"] == 1
    rows = [r for r in test_engine.db.proposals(50) if r["status"] == "orphan"]
    assert len(rows) == 1 and rows[0]["client_order_id"] == "EXT1" and rows[0]["symbol"] == "TSLA"
    again = await test_engine._adopt_orphan_orders()
    assert again == 0


async def test_startup_reconcile_never_raises(test_engine, monkeypatch):
    async def boom():
        raise BrokerError("down", transport=True)
    monkeypatch.setattr(test_engine.broker, "open_orders_text", boom)
    summary = await test_engine.startup_reconcile()
    assert any("adopt_orphans" in e for e in summary["errors"])


# -- lessons (M9): applied or skipped, with the reason -------------------------------------

def _lesson(db, **kw):
    fields = dict(param_name="sl_atr_mult", param_value=2.5, param_scope="global",
                  sample_size=40, min_sample_size=30, p_value=0.01, status="active",
                  created_by="human")
    fields.update(kw)
    return db.add_lesson(**fields)


async def test_lessons_apply_and_skip_with_reasons(test_engine):
    db = test_engine.db
    ok = _lesson(db, param_value=2.5)
    risk = _lesson(db, param_name="max_order_usd", param_value=10000)
    small = _lesson(db, param_name="tp_atr_mult", param_value=3.0, sample_size=5)
    test_engine._apply_active_lessons()
    by_id = {r["id"]: r for r in db.lessons(limit=10)}
    assert by_id[ok]["apply_state"] == "applied"
    assert by_id[risk]["apply_state"] == "skipped" and "risk limit" in by_id[risk]["apply_reason"]
    assert by_id[small]["apply_state"] == "skipped" and "below minimum" in by_id[small]["apply_reason"]
    # the frozen Settings was replaced, not mutated in place
    assert test_engine.s.sl_atr_mult == pytest.approx(2.5)
    assert test_engine.risk.s is test_engine.s


async def test_run_params_saved_per_run(test_engine):
    run_id = test_engine.db.create_run("test")
    test_engine.save_run_params(run_id)
    params = test_engine.db.get_run_params(run_id)
    assert params["sl_atr_mult"] == test_engine.s.sl_atr_mult
    assert "kill_action" in params and "options_enabled" in params


# -- workers: daily prune ------------------------------------------------------------------

async def test_maintenance_prunes_once_per_day(test_engine, monkeypatch):
    calls = []
    monkeypatch.setattr(test_engine.db, "prune", lambda days: calls.append(days) or {"events": 0})
    _settings(test_engine, options_enabled=False, db_retention_days=45)
    first = await test_engine.workers.maintenance_pass()
    second = await test_engine.workers.maintenance_pass()
    assert calls == [45] and "prune" in first and "prune" not in second


# -- fast loop: SignalStore read and kill TOCTOU -------------------------------------------

async def test_fastloop_reads_signal_store_fail_closed():
    from types import SimpleNamespace
    from app.fastloop import FastLoop
    store = SignalStore(ttl_seconds=60)
    eng = SimpleNamespace(kill_switch=False, signal_store=store)
    fl = FastLoop.__new__(FastLoop)
    fl.engine, fl.signals = eng, SimpleNamespace(latest=lambda s: {"symbol": s, "from": "db"})
    assert fl._signal("AAPL") is None              # nothing fresh in the store: no entry
    store.put("AAPL", {"symbol": "AAPL", "from": "store"})
    assert fl._signal("AAPL")["from"] == "store"


async def test_fastloop_rechecks_kill_after_awaits(monkeypatch):
    from types import SimpleNamespace
    import app.fastloop as F
    proposed = []

    async def on_proposal(run_id, p, sig_override=None):
        proposed.append(p)
        return "ok"
    eng = SimpleNamespace(kill_switch=False, _run_lock=asyncio.Lock(), research=None,
                          _on_proposal=on_proposal)
    broker = SimpleNamespace(connected=True)
    fl = F.FastLoop.__new__(F.FastLoop)
    fl.engine, fl.broker = eng, broker
    fl.s = SimpleNamespace(fast_loop_cooldown_minutes=5)
    fl.db = SimpleNamespace(create_run=lambda *a: 1, finish_run=lambda *a, **k: None,
                            log=lambda *a, **k: None)
    fl._cooldown_until, fl.triggers = {}, 0
    fl.signals = SimpleNamespace(cached_bars=lambda *a: [{"close": 1.0}],
                                 ml=SimpleNamespace(predict=lambda f: None))
    monkeypatch.setattr(F, "with_live_price", lambda bars, price, day: bars)
    monkeypatch.setattr(F, "indicators", SimpleNamespace(features=lambda b, bench=None: {"close": 1.0}, ml_features=lambda b, bench=None: {"close": 1.0}))
    monkeypatch.setattr(F, "ta", SimpleNamespace(analyze=lambda f, s: {
        "close": 1.0, "atr": 0.1, "trend": "up", "strength": 1, "vol": 1, "ta_score": 0.9,
        "ta_components": {}}))

    def gate(p, sig, s):
        eng.kill_switch = True           # the kill switch flips while the tick is in flight
        return {"ok": True}
    monkeypatch.setattr(F, "consensus", SimpleNamespace(evaluate=gate))
    out = await fl._check("AAPL", 10.0, {"symbol": "AAPL"})
    assert out is None and proposed == []
