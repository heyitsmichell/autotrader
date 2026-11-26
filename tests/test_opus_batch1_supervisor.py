"""Opus review batch 1: supervisor / alerts fixes S1 S2 S3 S5 S6 S7 S12 S13 S15 S17."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from app import supervisor as supmod
from app.alerts import Alerts
from app.market import now_et
from app.supervisor import Supervisor


class FakeEngine:
    def __init__(self):
        self.kill_switch = False
        self.kill_reason = None
        self.run_failures = self.order_failures = 0
        self.last_failure = None
        self.running = False
        self.flattening = False
        self._exec_lock = asyncio.Lock()
        self.tripped = []
        self.reconcile_calls = 0

    async def set_kill_switch(self, on, reason=""):
        self.kill_switch, self.kill_reason = on, reason if on else None
        self.tripped.append(reason)

    async def reconcile_orders(self):
        self.reconcile_calls += 1

    async def check_core_rebalance(self):
        pass

    async def _verify_protective_stops(self):
        pass


class FakeBroker:
    def __init__(self):
        self.connected = True
        self.error = None
        self.tools = []
        self.stops = self.starts = 0

    async def stop(self):
        self.stops += 1

    async def start(self):
        self.starts += 1
        self.connected = True


class FakePortfolio:
    def __init__(self, snaps):
        self.snaps = list(snaps)

    async def snapshot(self, max_age=0):
        s = self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]
        return dict(s) if False else s  # distinct objects are supplied by the caller

    def invalidate(self):
        pass


def snap(net=10_000.0, day=0.0, err=None, **bal):
    return {"balance": {"net_liq": net, "day_pnl": day, **bal}, "errors": {"balance": err} if err else {}}


def make(test_settings, test_db, snaps=(), **over):
    s = dataclasses.replace(test_settings, **over) if over else test_settings
    eng, br = FakeEngine(), FakeBroker()
    sup = Supervisor(s, test_db, br, eng, FakePortfolio(snaps or [snap()]), Alerts(s, test_db))
    return sup, eng, br


# ---- S1
def test_s1_release_acks_only_matching_trigger(test_settings, test_db):
    sup, eng, _ = make(test_settings, test_db)
    test_db.set_kv("equity_peak", 10_000.0)
    test_db.set_kv("daily_loss_ack", None)
    eng.kill_reason = "automatic: 3 failed orders in a row (last: x)"
    sup.on_release()
    assert test_db.get_kv("daily_loss_ack") is None
    assert test_db.get_kv("equity_peak") == 10_000.0
    eng.kill_reason = "automatic: daily loss: day P&L $-5"
    sup.on_release()
    assert test_db.get_kv("daily_loss_ack") == now_et().date().isoformat()
    assert test_db.get_kv("equity_peak") == 10_000.0
    eng.kill_reason = "automatic: drawdown: net liquidation"
    sup.on_release()
    assert test_db.get_kv("equity_peak") is None


# ---- S2
@pytest.mark.asyncio
async def test_s2_kill_alert_bypasses_cap_and_dedupe(test_settings, test_db):
    s = dataclasses.replace(test_settings, alert_max_per_hour=1, alert_dedupe_minutes=30)
    al = Alerts(s, test_db)
    sent = []

    async def fake_send(message, level, title):
        sent.append(message)
        return {}
    al.send = fake_send
    assert al.notify("routine one")
    assert not al.notify("routine two")          # cap reached
    al._on_event("error", "KILL SWITCH ON (x): stopped", True)
    al._on_event("error", "KILL SWITCH ON (x): stopped", True)   # duplicate still goes
    await asyncio.sleep(0.02)
    assert sent.count("KILL SWITCH ON (x): stopped") == 2


# ---- S3
@pytest.mark.asyncio
async def test_s3_toast_off_loop_and_injection_safe(monkeypatch):
    import app.platform as plat
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"], calls["env"] = cmd, kw.get("env")
        return SimpleNamespace()
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat.subprocess, "run", fake_run)
    evil = 'x"); calc; $("'
    assert await asyncio.to_thread(plat.send_desktop_notification, evil, evil)
    assert evil not in " ".join(calls["cmd"])
    assert calls["env"]["TRADER_TOAST_TITLE"] == evil

    from app.alerts import Alerts as A
    import inspect
    assert "to_thread" in inspect.getsource(A._local)


# ---- S5
@pytest.mark.asyncio
async def test_s5_forced_reconnect_after_n_transport_failures(test_settings, test_db):
    bad = [snap(err="timeout") for _ in range(3)]
    sup, eng, br = make(test_settings, test_db, snaps=bad)
    for s in bad:
        sup.portfolio.snaps = [s]
        await sup.check_kill_triggers()
    assert sup._transport_fails == 3
    eng._exec_lock = asyncio.Lock()
    await eng._exec_lock.acquire()               # order mid-flight: no reconnect
    await sup.broker_health()
    assert br.stops == 0
    eng._exec_lock.release()
    await sup.broker_health()
    assert (br.stops, br.starts) == (1, 1)
    assert sup._transport_fails == 0


# ---- S6
@pytest.mark.asyncio
async def test_s6_kill_checks_survive_failing_reconcile(test_settings, test_db):
    sup, eng, br = make(test_settings, test_db)
    eng.run_failures = 3
    order = []

    async def boom():
        order.append("reconcile")
        raise RuntimeError("bad")
    eng.reconcile_orders = boom
    sup.s = dataclasses.replace(sup.s, kill_max_consecutive_errors=3, performance_daily_enabled=False)
    await sup.tick()
    assert eng.tripped, "kill check must run even though reconcile raised"
    for _ in range(supmod.STEP_FAIL_ALERT_TICKS):
        await sup._step("x", boom)
    assert sup._step_fail["x"] == supmod.STEP_FAIL_ALERT_TICKS


# ---- S7
@pytest.mark.asyncio
async def test_s7_stale_skipped_and_two_readings_needed(test_settings, test_db):
    sup, eng, _ = make(test_settings, test_db, kill_max_drawdown_pct=10.0, kill_daily_loss_usd=0.0)
    test_db.set_kv("equity_peak", 10_000.0)
    sup.portfolio.snaps = [snap(5_000.0, stale=True)]
    assert await sup.check_kill_triggers() is None
    sup.portfolio.snaps = [snap(5_000.0, net_liq_derived=True)]
    assert await sup.check_kill_triggers() is None
    sup.portfolio.snaps = [snap(5_000.0, err="boom")]
    assert await sup.check_kill_triggers() is None
    sup.portfolio.snaps = [snap(8_000.0)]
    assert await sup.check_kill_triggers() is None   # first breach
    sup.portfolio.snaps = [snap(8_000.0)]
    assert "drawdown" in await sup.check_kill_triggers()  # second


@pytest.mark.asyncio
async def test_s7_cached_snapshot_counts_once(test_settings, test_db):
    sup, eng, _ = make(test_settings, test_db, kill_max_drawdown_pct=10.0, kill_daily_loss_usd=0.0)
    test_db.set_kv("equity_peak", 10_000.0)
    same = snap(8_000.0)
    sup.portfolio.snaps = [same]
    for _ in range(4):                       # same cached object every tick: one reading
        assert await sup.check_kill_triggers() is None
    sup.portfolio.snaps = [snap(8_000.0)]    # new object: second reading
    assert "drawdown" in await sup.check_kill_triggers()
    # peak: cached high reading must not raise the peak
    sup2, _, _ = make(test_settings, test_db, kill_max_drawdown_pct=10.0, kill_daily_loss_usd=0.0)
    test_db.set_kv("equity_peak", 10_000.0)
    hi = snap(20_000.0)
    sup2.portfolio.snaps = [hi]
    for _ in range(4):
        await sup2.check_kill_triggers()
    assert test_db.get_kv("equity_peak") == 10_000.0
    sup2.portfolio.snaps = [snap(20_000.0)]
    await sup2.check_kill_triggers()
    assert test_db.get_kv("equity_peak") == 20_000.0


@pytest.mark.asyncio
async def test_s5_backoff_and_single_alert(test_settings, test_db):
    sup, eng, br = make(test_settings, test_db)
    logs = []
    test_db.log = lambda lvl, msg, alert=False: logs.append((lvl, alert))
    sup._transport_fails = 3
    await sup.broker_health()
    sup._transport_fails = 3
    await sup.broker_health()                  # inside the backoff gap: no second restart
    assert br.stops == 1
    sup._next_forced = 0.0
    sup._transport_fails = 3
    await sup.broker_health()
    assert br.stops == 2 and sup._forced_gap == 180.0
    alerts = [x for x in logs if x[1]]
    assert len(alerts) == 1
    sup._note_transport(snap())                # good read resets
    assert sup._forced_gap == 0.0 and sup._forced_streak == 0


@pytest.mark.asyncio
async def test_step_sets_last_error_and_trip_skips_when_on(test_settings, test_db):
    sup, eng, _ = make(test_settings, test_db, kill_action="flatten")
    calls = []

    async def fl(*a, **k):
        calls.append(a)
    eng.flatten = fl

    async def boom():
        raise RuntimeError("bad")
    await sup._step("x", boom)
    assert "bad" in sup.last_error
    await sup._step("x", lambda: None)
    assert sup.last_error is None
    await sup.trip("r")
    await sup.trip("r2")                       # already on: no second action
    assert len(calls) == 1


def test_s7_peak_needs_two_readings(test_settings, test_db):
    sup, _, _ = make(test_settings, test_db)
    test_db.set_kv("equity_peak", 10_000.0)
    sup._track_drawdown({"net_liq": 50_000.0})
    assert test_db.get_kv("equity_peak") == 10_000.0
    sup._track_drawdown({"net_liq": 10_500.0})
    assert test_db.get_kv("equity_peak") == 10_500.0


def test_s7_parse_balance_flags_derived():
    from app.portfolio_parsing import parse_balance
    assert parse_balance("Total Cash Balance: 100\n").get("net_liq_derived") is True
    assert "net_liq_derived" not in parse_balance("Net Liquidation: 500\nTotal Cash Balance: 100\n")


# ---- S12
def test_s12_spy_warn_once_per_day_and_ack(test_settings, test_db):
    import time
    sup, eng, _ = make(test_settings, test_db, spy_benchmark_enabled=True, spy_benchmark_action="warn",
                       spy_benchmark_min_outperformance_pct=5.0)
    n = {"i": 0}

    async def vs():
        n["i"] += 1
        return {"alpha_pct": -1.0 - n["i"], "account_pct": 1.0, "spy_pct": 2.0, "days": 30}
    sup.portfolio_vs_spy = vs
    logged = []
    test_db.log = lambda lvl, msg, alert=False: logged.append(msg)

    async def go():
        for _ in range(3):
            sup._spy_benchmark_cache = None
            await sup.check_spy_benchmark()
    asyncio.run(go())
    assert len(logged) == 1
    eng.kill_reason = "automatic SPY benchmark: x"
    sup.on_release()
    assert test_db.get_kv("spy_ack") == now_et().date().isoformat()


# ---- S13
@pytest.mark.asyncio
async def test_s13_nightly_job_does_not_block(test_settings, test_db, monkeypatch):
    sup, _, _ = make(test_settings, test_db, alert_daily_summary=True)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
    sup.run_nightly_job = slow
    sup.learner = object()
    sup.alerts = SimpleNamespace(channels=["x"], notify=lambda *a, **k: None)

    async def text():
        return "s"
    sup.summary_text = text
    monkeypatch.setattr(supmod, "session_close", lambda d: __import__("datetime").time(0, 1))
    await asyncio.wait_for(sup.daily_summary(), 1)
    assert sup._nightly_task is not None and not sup._nightly_task.done()
    gate.set()
    await sup._nightly_task


# ---- S15
@pytest.mark.asyncio
async def test_s15_reconnect_when_run_stuck_too_long(test_settings, test_db):
    import time
    sup, eng, br = make(test_settings, test_db)
    br.connected = False
    eng.running = True
    sup.down_since = time.time() - 60
    await sup.broker_health()
    assert br.starts == 0                      # recent: still waits for the run
    sup.down_since = time.time() - supmod.RUN_STUCK_SECONDS - 5
    sup._next_reconnect = 0
    await sup.broker_health()
    assert br.starts == 1


# ---- S17
@pytest.mark.asyncio
async def test_s17_retry_and_unfilter_on_total_failure(test_settings, test_db, monkeypatch):
    import app.alerts as am
    monkeypatch.setattr(am, "SEND_BACKOFF", (0.0, 0.0))
    s = dataclasses.replace(test_settings, alert_ntfy_topic="t")
    al = Alerts(s, test_db)
    tries = {"n": 0}

    async def flaky(title, body, level):
        tries["n"] += 1
        if tries["n"] < 3:
            raise RuntimeError("down")
    al._ntfy = flaky

    async def no_local(title, body, level):
        return None
    al._local = no_local                       # never fire the real toast
    r = await al.send("hello", "info")
    assert r["ntfy"] == "ok" and tries["n"] == 3

    async def dead(title, body, level):
        raise RuntimeError("down")
    al._ntfy = dead
    al._recent["m2"] = 1.0
    r = await al.send("m2", "info")
    assert "m2" not in al._recent
