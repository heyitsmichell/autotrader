"""Ledger exit attribution (two trades, one symbol), exit backoff, soft-stop reuse."""
import time

import pytest

from app.portfolio import Portfolio
from app.broker import BrokerError


def _order(coid, side, status, price, filled_at, sym="ADI"):
    return (f"Client Order ID: {coid}\n  Client Order ID: {coid}\n  Symbol: {sym}\n  Side: {side}\n"
            f"  Status: {status}\n  Total Quantity: 1\n  Filled Quantity: 1\n"
            f"  Average Fill Price: {price}\n  Filled Time: {filled_at}\n")


class _B:
    def __init__(self, hist, held=0.0):
        self.hist, self.held = hist, held

    async def open_orders_text(self):
        return ""

    async def order_history_text(self):
        return self.hist

    async def positions(self):
        return {"ADI": self.held}

    async def bars(self, *a, **k):
        return []


def _mk_trade(db, coid, entry, entry_time, stop=308.36):
    p = {"symbol": "ADI", "side": "BUY", "quantity": 1, "limit_price": entry,
         "stop_price": stop, "target_price": 355.0}
    pid = db.add_proposal(None, p, "placed", client_order_id=coid)
    db.add_trade_entry(pid, p)
    db.update_trade_fill(pid, entry, entry_time, 1, 0.0, 0.0)
    return pid


def _setup(db, sl_status="filled"):
    p1 = _mk_trade(db, "e1", 348.0, "2026-11-24T13:00:00Z")
    r1 = db.add_protective(None, "ADI", "sl", "s1", 1, 348.45, "filled", "re-placed")
    p2 = _mk_trade(db, "e2", 331.52, "2026-11-24T14:31:44Z")
    r2 = db.add_protective(p2, "ADI", "sl", "s2", 1, 308.36, sl_status, "own stop")
    return p1, p2


HIST = (_order("s1", "SELL", "FILLED", 328.0, "2026-11-24T14:30:00Z")
        + _order("s2", "SELL", "FILLED", 308.24, "2026-11-24T16:01:10Z"))


@pytest.mark.asyncio
async def test_two_sequential_trades_second_uses_own_stop(test_settings, test_db):
    p1, p2 = _setup(test_db)
    test_db.update_trade_exit(p1, 328.0, "2026-11-24T14:30:00Z", "stop")   # first already closed
    pf = Portfolio(test_settings, test_db, _B(HIST))
    assert await pf.track_trade_fills() == 1
    t = test_db.get_trade(p2)
    assert t["exit_fill_price"] == 308.24
    assert t["exit_reason"] == "stop"
    assert round(t["realized_pnl"], 2) == -23.28


@pytest.mark.asyncio
async def test_fallback_newest_sell_after_entry(test_settings, test_db):
    _, p2 = _setup(test_db, sl_status="cancelled")      # own row not 'filled': no protective link
    p1 = test_db.get_trade(p2)
    test_db.update_trade_exit(1 if False else _first(test_db), 328.0, "x", "stop")
    pf = Portfolio(test_settings, test_db, _B(HIST))
    await pf.track_trade_fills()
    t = test_db.get_trade(p2)
    assert t["exit_fill_price"] == 308.24        # newest after entry; not the 09:30 fill
    assert t["exit_reason"] == "stop"            # s2 is its own protective row


def _first(db):
    return min(t["proposal_id"] for t in db.open_trades())


@pytest.mark.asyncio
async def test_sell_before_entry_is_ignored(test_settings, test_db):
    p1 = _mk_trade(test_db, "e1", 331.52, "2026-11-24T14:31:44Z")
    hist = _order("old", "SELL", "FILLED", 328.0, "2026-11-24T14:30:00Z")
    pf = Portfolio(test_settings, test_db, _B(hist))
    assert await pf.track_trade_fills() == 0
    assert test_db.get_trade(p1)["exit_fill_price"] is None


@pytest.mark.asyncio
async def test_manual_when_no_protective_row(test_settings, test_db):
    p1 = _mk_trade(test_db, "e1", 331.52, "2026-11-24T14:31:44Z")
    hist = _order("m1", "SELL", "FILLED", 335.0, "2026-11-24T15:00:00Z")
    pf = Portfolio(test_settings, test_db, _B(hist))
    await pf.track_trade_fills()
    t = test_db.get_trade(p1)
    assert (t["exit_fill_price"], t["exit_reason"]) == (335.0, "manual")


class _Rej:
    def __init__(self):
        self.n = 0

    async def place_order(self, *a, **k):
        self.n += 1
        raise BrokerError("rejected")


@pytest.mark.asyncio
async def test_exit_backoff_doubles_and_resets(test_settings, test_db):
    b = _Rej()
    pf = Portfolio(test_settings, test_db, b)
    assert await pf.exit_position("ADI", 1, 100.0, "x") is False
    assert b.n == 1
    assert await pf.exit_position("ADI", 1, 100.0, "x") is False
    assert b.n == 1                                   # backed off: no second order, no alert
    assert pf._exit_backoff["ADI"]["next"] - time.time() == pytest.approx(150, abs=5)
    pf._exit_backoff["ADI"]["next"] = 0
    await pf.exit_position("ADI", 1, 100.0, "x")
    assert b.n == 2
    assert pf._exit_backoff["ADI"]["next"] - time.time() == pytest.approx(300, abs=5)
    pf._exit_backoff["ADI"]["fails"] = 20
    pf._exit_backoff["ADI"]["next"] = 0
    await pf.exit_position("ADI", 1, 100.0, "x")
    assert pf._exit_backoff["ADI"]["next"] - time.time() == pytest.approx(1800, abs=5)


@pytest.mark.asyncio
async def test_exit_backoff_resets_on_success(test_settings, test_db):
    class Ok:
        async def place_order(self, *a, **k):
            return None
    pf = Portfolio(test_settings, test_db, Ok())
    pf._exit_backoff = {"ADI": {"fails": 3, "next": 0}}
    assert await pf.exit_position("ADI", 1, 100.0, "x") is True
    assert "ADI" not in pf._exit_backoff


@pytest.mark.asyncio
async def test_soft_stop_reusable_after_settle_and_core_restored(test_engine, test_db):
    rl = test_engine.risk_loop
    pid = _mk_trade(test_db, "e1", 100.0, "2026-11-24T14:00:00Z", stop=95.0)
    t = test_db.get_trade(pid)
    test_db.set_kv(f"soft_stop:{t['id']}", {"coid": "c", "settled": True})
    assert rl._soft_stop_reusable(t, {}) is False            # no CORE stop yet
    test_db.add_protective(pid, "ADI", "sl", "c", 1, 95.0, "closed", "extended-hours soft stop: ALL")
    test_db.add_protective(pid, "ADI", "sl", "k", 1, 95.0, "active", "CORE stop restored")
    assert rl._soft_stop_reusable(t, {}) is True
    test_db.set_kv(f"soft_stop:{t['id']}", {"coid": "c"})    # unsettled: still blocks
    assert rl._soft_stop_reusable(t, {}) is False
