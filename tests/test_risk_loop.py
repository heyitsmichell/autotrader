import time
from types import SimpleNamespace
import pytest
from app.broker import BrokerError
from app.engine import flatten as flatten_mod
from app.engine import risk_loop as rl_mod
from app.engine.risk_loop import RiskLoop

def _orders(*rows):
    out = ""
    for coid, sym, side, qty, status in rows:
        out += (f"Client Order ID: g{coid}\n  Client Order ID: {coid}\n  Symbol: {sym}\n  Side: {side}\n"
                f"  Status: {status}\n  Total Quantity: {qty}\n")
    return out


# -- S8 --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s8_rejection_reprotects_and_backs_off_per_symbol():
    calls = []

    async def cancel_protective(sym, coid, partial=None):
        return [9.0, 9.5]

    async def reprotect(p, shares, stops):
        calls.append((p["symbol"], shares, list(stops)))

    async def place_order(*a, **k):
        raise BrokerError("rejected", transport=False)

    db = SimpleNamespace(set_kv=lambda *a: None, add_protective=lambda *a, **k: 1,
                         set_protective=lambda *a, **k: None, settle_stops=lambda *a: None,
                         log=lambda *a, **k: None)
    eng = SimpleNamespace(db=db, _cancel_protective=cancel_protective, _reprotect=reprotect,
                          broker=SimpleNamespace(place_order=place_order))
    loop = RiskLoop(eng)
    loop._clock = lambda: 1000.0
    loop._below["XYZ"] = {"first": 0, "n": 3}
    t = {"id": 1, "symbol": "XYZ", "proposal_id": 5}
    t2 = {"id": 2, "symbol": "XYZ", "proposal_id": 6}
    assert await loop._fire_soft_stop(t, 10, 9.0, 9.5, "ALL", [t, t2]) is False
    assert calls == [("XYZ", 10, [9.0, 9.5])]
    assert loop._soft_retry_at["XYZ"] == 1015.0
    assert "XYZ" not in loop._below
    for _ in range(3):
        await loop._fire_soft_stop(t, 10, 9.0, 9.5, "ALL", [t, t2])
    assert loop._soft_retry_at["XYZ"] == float("inf")


# -- S10 -------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s10_cancel_open_orders_spares_sleeve_closing(test_engine, monkeypatch):
    eng = test_engine
    eng.db.add_sleeve_order("put_write", "AAPL250101P00100000", "option", "BUY", 1, 0.5,
                            "sl-close", position_intent="BUY_TO_CLOSE")
    cancelled = []

    async def oot():
        return _orders(("sl-close", "AAPL250101P00100000", "BUY", 1, "SUBMITTED"),
                       ("other", "MSFT", "BUY", 1, "SUBMITTED"))

    async def cancel(coid):
        cancelled.append(coid)
        return "Cancelled"

    monkeypatch.setattr(eng.broker, "open_orders_text", oot)
    monkeypatch.setattr(eng.broker, "cancel_order", cancel)
    monkeypatch.setattr(flatten_mod, "cancel_reply_confirmed", lambda r: True)
    await eng.cancel_open_orders(False, "t")
    assert cancelled == ["other"]


@pytest.mark.asyncio
async def test_s10_flatten_closed_persists_pending(test_engine, monkeypatch):
    eng = test_engine
    monkeypatch.setattr(flatten_mod, "order_session", lambda *a, **k: None)

    async def ks(*a, **k):
        return None
    monkeypatch.setattr(eng, "set_kill_switch", ks)
    with pytest.raises(ValueError):
        await eng.flatten("u", "t")
    assert eng.db.get_kv("pending_flatten")["reason"] == "t"

    ran = []

    async def fake_flatten(actor, reason):
        ran.append((actor, reason))
    eng.flatten = fake_flatten
    monkeypatch.setattr(rl_mod, "order_session", lambda *a, **k: "CORE")
    loop = RiskLoop(eng)
    assert await loop._run_pending_flatten() is True
    await loop._flatten_task
    assert ran == [("u", "t")] and eng.db.get_kv("pending_flatten") is None


# -- S11 -------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s11_unsold_shares_reprotected(test_engine, monkeypatch):
    eng = test_engine
    got = []

    async def sell_part(sym, qty, actor, reason, result):
        result["orders"].append({"symbol": sym, "quantity": 4, "limit_price": 1, "status": "placed"})
        result["orders"].append({"symbol": sym, "quantity": 6, "limit_price": 1, "status": "failed"})

    async def reprotect(p, shares, stops):
        got.append((p["symbol"], shares, stops))

    monkeypatch.setattr(eng, "_flatten_symbol_sell", sell_part)
    monkeypatch.setattr(eng, "_reprotect", reprotect)
    eng._flatten_stops = {"AAPL": [3, [90.0, 95.0]]}
    await eng._flatten_symbol("AAPL", 10, "u", "t", {"orders": [], "errors": []})
    assert got == [("AAPL", 6, [90.0, 95.0])]


# -- S19 -------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s19_no_restore_while_sell_still_open(monkeypatch):
    restored = []
    kv = {"soft_stop:1": {"coid": "c1", "at": time.time() - 700}}
    opens = {"n": 0}

    async def oot():     # first read (followup snapshot): gone; second (confirm): still working
        opens["n"] += 1
        return "" if opens["n"] == 1 else _orders(("c1", "XYZ", "SELL", 10, "SUBMITTED"))

    async def oht():
        return ""

    db = SimpleNamespace(open_trades=lambda: [{"id": 1, "symbol": "XYZ", "exit_fill_price": None}],
                         get_kv=lambda k, d=None: kv.get(k, d),
                         set_kv=lambda k, v: kv.__setitem__(k, v), log=lambda *a, **k: None)
    eng = SimpleNamespace(db=db, broker=SimpleNamespace(open_orders_text=oot, order_history_text=oht))
    loop = RiskLoop(eng)
    loop._clock = lambda: 1e6

    async def positions():
        return {"XYZ": 10.0}

    async def restore(*a, **k):
        restored.append(1)
    loop._positions = positions
    loop._restore_after_soft = restore
    await loop._soft_followups("CORE")
    assert not restored and not kv["soft_stop:1"].get("settled")
    # order truly gone from both lists: settle + restore
    async def oot_empty():
        return ""
    eng.broker.open_orders_text = oot_empty
    loop._followup_at = -1e9
    await loop._soft_followups("CORE")
    assert restored and kv["soft_stop:1"]["settled"]


# -- S20 -------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s20_unknown_status_gives_up(test_engine, monkeypatch):
    eng = test_engine
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(minutes=500)).isoformat(timespec="seconds")
    p = {"id": 7, "side": "BUY", "quantity": 1, "symbol": "AAPL", "limit_price": 1.0,
         "client_order_id": "cx", "placed_at": old, "created_at": old}
    updates = []
    monkeypatch.setattr(eng.db, "unresolved_proposals", lambda: [p])
    monkeypatch.setattr(eng.db, "update_proposal", lambda pid, **kw: updates.append((pid, kw)))

    async def oot():
        return _orders(("cx", "AAPL", "BUY", 1, "WEIRDSTATE"))

    async def oht():
        return ""
    monkeypatch.setattr(eng.broker, "open_orders_text", oot)
    monkeypatch.setattr(eng.broker, "order_history_text", oht)
    assert await eng.reconcile_orders() == 1
    assert updates and updates[0][1]["status"] == "failed"


# -- review fixes ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pending_flatten_cleared_on_release_and_not_run_at_night(test_engine, monkeypatch):
    eng = test_engine
    eng.db.set_kv("pending_flatten", {"actor": "u", "reason": "t"})
    monkeypatch.setattr(rl_mod, "order_session", lambda *a, **k: "NIGHT")
    loop = RiskLoop(eng)
    assert await loop._run_pending_flatten() is False
    assert eng.db.get_kv("pending_flatten") is not None
    await eng.set_kill_switch(True, "x")
    await eng.set_kill_switch(False)
    assert eng.db.get_kv("pending_flatten") is None


@pytest.mark.asyncio
async def test_s8_backoff_resets_on_session_change_and_success(monkeypatch):
    phase = {"v": "premarket"}
    monkeypatch.setattr(rl_mod, "session_phase", lambda *a, **k: phase["v"])
    monkeypatch.setattr(rl_mod, "order_session", lambda *a, **k: "ALL")

    async def cancel_protective(sym, coid, partial=None):
        return []

    async def place_order(*a, **k):
        raise BrokerError("rejected", transport=False)

    db = SimpleNamespace(set_kv=lambda *a: None, get_kv=lambda *a: None,
                         add_protective=lambda *a, **k: 1, open_trades=lambda: [],
                         set_protective=lambda *a, **k: None, settle_stops=lambda *a: None,
                         log=lambda *a, **k: None)
    eng = SimpleNamespace(db=db, _cancel_protective=cancel_protective,
                          s=SimpleNamespace(extended_hours_soft_stop=True),
                          broker=SimpleNamespace(place_order=place_order))
    loop = RiskLoop(eng)
    loop._clock = lambda: 1000.0
    t = {"id": 1, "symbol": "XYZ", "proposal_id": 5}
    await loop._fire_soft_stop(t, 10, 9.0, 9.5, "ALL", [t])
    assert loop._soft_rejects["XYZ"] == 1
    phase["v"] = "afterhours"
    await loop._soft_stops()
    assert "XYZ" not in loop._soft_rejects

    async def ok(*a, **k):
        return "ok"
    eng.broker.place_order = ok
    loop._soft_rejects["XYZ"] = 2
    assert await loop._fire_soft_stop(t, 10, 9.0, 9.5, "ALL", [t]) is True
    assert "XYZ" not in loop._soft_rejects
