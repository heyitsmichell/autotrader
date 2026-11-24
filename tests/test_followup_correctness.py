"""Follow-up correctness fixes: working-entry hole, signal bars, ML override, mock, backoff."""

import os
from datetime import datetime, timezone

import pytest

from app import consensus
from app.risk import working_buys, working_buy_symbols


class _Broker:
    def __init__(self, open_text=""):
        self.open_text = open_text

    async def open_orders_text(self):
        return self.open_text

    async def order_history_text(self):
        return ""


def _working(coid):
    return (f"Client Order ID: {coid}\n  Client Order ID: {coid}\n  Order ID: O{coid}\n"
            "  Symbol: XLY\n  Side: BUY\n  Status: WORKING\n  Total Quantity: 2\n"
            "  Filled Quantity: 0\n  Limit Price: 100.0\n")


@pytest.mark.asyncio
async def test_timeout_then_reconcile_working_blocks_second_buy(test_engine, test_db):
    row = {"symbol": "XLY", "side": "BUY", "quantity": 2, "limit_price": 100.0}
    pid = test_db.add_proposal(None, row, "unknown", client_order_id="coidx1")
    assert [w["proposal_id"] for w in working_buys(test_db, "XLY")] == [pid]
    test_engine.broker = _Broker(_working("coidx1"))
    assert await test_engine.reconcile_orders() == 1
    assert test_db.get_proposal(pid)["status"] == "placed"
    assert test_db.get_trade(pid) is not None          # ledger entry created by reconcile
    assert "XLY" in working_buy_symbols(test_db)


def test_placed_buy_without_ledger_row_still_counts(test_db):
    row = {"symbol": "XLY", "side": "BUY", "quantity": 2, "limit_price": 100.0}
    pid = test_db.add_proposal(None, row, "placed", client_order_id="c2")
    assert [w["proposal_id"] for w in working_buys(test_db, "XLY")] == [pid]
    assert working_buys(test_db, "XLY", exclude_pid=pid) == []


def test_ml_override_needs_more_than_70_percent():
    from app.config import load_settings
    import dataclasses
    s = dataclasses.replace(load_settings(), ml_enabled=True, ml_first=True, ml_min_p=0.52)
    from tests.test_ml_first import BUY, _sig
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.60, trend="down"), s)["ok"] is False
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.70, trend="down"), s)["ok"] is False
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.71, trend="down"), s)["ok"] is True


def test_dotenv_skipped_in_tests():
    assert os.environ.get("TRADER_SKIP_DOTENV") == "1"


def test_bar_merge_dedupes_by_trading_day(test_settings):
    import asyncio
    from app.signals import SignalService
    from app.market import now_utc

    svc = SignalService(test_settings, None, None, None, None)
    bars = [{"time": datetime(2025, 1 + i // 28, 1 + i % 28, tzinfo=timezone.utc), "close": 1} for i in range(70)]
    last = bars[-1]["time"]
    # today's partial bar returns with an intraday stamp, not midnight
    class B2:
        async def daily_bars(self, sym, n):
            return [{"time": last.replace(hour=14, minute=30), "close": 2}]
    svc.broker = B2()
    from datetime import timedelta
    svc._bars_cache["X"] = (now_utc() - timedelta(seconds=svc.BARS_TTL_SECONDS + 5), bars)
    svc._bars_full_at["X"] = now_utc()
    out = asyncio.run(svc._load_bars("X"))
    days = [svc._day_key(b) for b in out]
    assert len(days) == len(set(days)) == 70


@pytest.mark.asyncio
async def test_mock_accepts_core_gtc_stop_out_of_hours(monkeypatch):
    from app.mock import server
    import app.market as market
    monkeypatch.setattr(market, "order_session", lambda *a, **k: None)
    monkeypatch.setattr(server, "check_account", lambda a: None, raising=False)
    r = await server.place_stock_order("AAPL", "SELL", "STOP_LOSS", "GTC", 1, stop_price=100.0,
                                       trading_session="CORE")
    assert "CORE_SESSION_CLOSED" not in r
    r = await server.place_stock_order("AAPL", "BUY", "LIMIT", "DAY", 1, limit_price=100.0,
                                       trading_session="CORE")
    assert "CORE_SESSION_CLOSED" in r


@pytest.mark.asyncio
async def test_stop_replace_backs_off(test_engine, test_db, monkeypatch):
    from app.broker import BrokerError
    e = test_engine
    import dataclasses
    e.s = dataclasses.replace(e.s, bracket_orders=True, core_enabled=False)
    calls = []

    class Bk:
        async def positions(self): return {"ZZZ": 5}
        async def open_orders_text(self): return ""
        async def place_stop(self, *a, **k):
            calls.append(a)
            raise BrokerError("rejected")
    e.broker = Bk()
    test_db.add_trade_entry(1, {"symbol": "ZZZ", "side": "BUY", "quantity": 5,
                                "limit_price": 10.0, "sl": 9.0, "tp": 12.0})
    await e._verify_protective_stops()
    await e._verify_protective_stops()
    assert len(calls) == 1                       # second pass is inside the backoff window
    e._stop_backoff["ZZZ"]["next"] = 0
    await e._verify_protective_stops()
    assert len(calls) == 2 and e._stop_backoff["ZZZ"]["fails"] == 2
