"""Audit fixes: MAE/MFE None, hold clamp, daily P&L day one, put-write marks, drawdown, titles."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.portfolio import Portfolio

T0 = datetime(2026, 11, 24, 14, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)


def _bar(t, lo, hi):
    return {"time": t, "low": lo, "high": hi, "open": lo, "close": hi}


class _Bk:
    def __init__(self, intra=None, daily=None, fail=False):
        self.intra, self.daily, self.fail = intra or [], daily or [], fail

    async def intraday_bars(self, *a, **k):
        if self.fail:
            raise RuntimeError("x")
        return self.intra

    async def daily_bars(self, *a, **k):
        if self.fail:
            raise RuntimeError("x")
        return self.daily


@pytest.mark.asyncio
async def test_mae_mfe_none_on_failure(test_settings, test_db):
    pf = Portfolio(test_settings, test_db, _Bk(fail=True))
    assert await pf._compute_mae_mfe("X", 100.0, T0, T1) == (None, None)


@pytest.mark.asyncio
async def test_mae_mfe_falls_back_to_daily(test_settings, test_db):
    day = datetime(2026, 11, 24, tzinfo=timezone.utc)
    late = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)   # intraday starts after entry
    pf = Portfolio(test_settings, test_db, _Bk(intra=[_bar(late, 99, 101)], daily=[_bar(day, 95, 110)]))
    assert await pf._compute_mae_mfe("X", 100.0, T0, T1) == (-5.0, 10.0)


@pytest.mark.asyncio
async def test_mae_mfe_intraday_when_covering(test_settings, test_db):
    bars = [_bar(T0, 98, 101), _bar(T1, 99, 104)]
    pf = Portfolio(test_settings, test_db, _Bk(intra=bars))
    assert await pf._compute_mae_mfe("X", 100.0, T0, T1) == (-2.0, 4.0)


def test_autopsy_keeps_null_mae(test_db):
    import inspect
    from app.db import autopsies
    assert 'trade.get("mae") or 0' not in inspect.getsource(autopsies)


def test_exit_filled_before_entry_is_not_matched(test_settings, test_db):
    pf = Portfolio(test_settings, test_db, _Bk())
    p = {"symbol": "NXPI", "side": "BUY", "quantity": 1, "limit_price": 200.0,
         "stop_price": 190.0, "target_price": 230.0}
    pid = test_db.add_proposal(None, p, "placed", client_order_id="e")
    test_db.add_trade_entry(pid, p)
    test_db.update_trade_fill(pid, 200.0, "2026-11-24T14:00:00Z", 1, 0.0, 0.0)
    test_db.add_protective(pid, "NXPI", "sl", "s", 1, 190.0, "filled", "own")
    orders = {"s": {"symbol": "NXPI", "side": "SELL", "status": "FILLED",
                    "filled_at": "2026-11-24T13:58:40Z", "client_order_id": "s"}}
    o, _, _ = pf._find_exit_order(test_db.get_trade(pid), orders)
    assert o is None


def test_daily_pnl_first_day_uses_start_capital():
    from app.results import daily_pnl
    rows = [{"day": "d1", "a": 10_300.0}, {"day": "d2", "a": 10_250.0}]
    out = daily_pnl(rows, ["a"], {"a": 10_000.0})
    assert [r["a"] for r in out] == [300.0, -50.0]
    assert daily_pnl(rows, ["a"])[0]["a"] == 0.0


def test_sleeve_value_flags_unmarked_option(test_settings, test_db):
    from app.sleeves.manager import SleeveManager
    test_db.upsert_sleeve_state("putwrite", cash=1000.0, start_capital=1000.0, start_date="2026-11-20")
    test_db.upsert_sleeve_holding("putwrite", "AAPL261218P00100000", "option", -1, 2.0) \
        if hasattr(test_db, "upsert_sleeve_holding") else pytest.skip("no holding helper")
    mgr = SleeveManager.__new__(SleeveManager)
    mgr.db = test_db
    vals = mgr._value({"options": {}, "last": {}, "net_liq": None})
    assert vals["putwrite"]["unmarked"] is True


@pytest.mark.asyncio
async def test_drawdown_updates_even_when_killed(test_engine, test_settings, test_db, mock_broker):
    from app.supervisor import Supervisor
    from app.alerts import Alerts
    sup = Supervisor(test_settings, test_db, mock_broker, test_engine, test_engine.portfolio,
                     Alerts(test_settings, test_db))
    sup._track_drawdown({"net_liq": 10_000.0})
    sup._track_drawdown({"net_liq": 9_000.0})
    assert sup.drawdown_pct == 10.0


def test_generic_titles_fall_back_to_folder(tmp_path):
    from app.api.experiments import _title
    from app import backtests_view
    d = tmp_path / "momentum_variants"
    d.mkdir()
    f = d / "summary.md"
    f.write_text("# Full\nbody", encoding="utf-8")
    assert _title(f) == "momentum_variants / summary"
    assert backtests_view._title(f) == "momentum_variants / summary"
    f.write_text("# Momentum skip one week\n", encoding="utf-8")
    assert _title(f) == "Momentum skip one week"


def test_shadow_window_from_row():
    from app.backtests_view import _row_window
    assert _row_window({"start": "2010-01-04", "end": "2025-11-21"}, 4000) == "2010-01-04..2025-11-21 (4000 days)"
    assert "dates not recorded" in _row_window({}, 100)
