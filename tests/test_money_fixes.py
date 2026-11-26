"""Money-path fixes: bracket validation, rebalance deferral, fees, option outcomes."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app import options_ml
from app.broker import BrokerError
from app.broker.orders import OrdersMixin
from app.sleeves import manager as sm
from app.sleeves.strategies import rebalance


def _bracket(limit, tp, sl):
    coids = {"entry": "e", "tp": "t", "sl": "s"}
    return asyncio.run(OrdersMixin.place_bracket(SimpleNamespace(), "AAPL", 1, limit, tp, sl, coids, "c"))


@pytest.mark.parametrize("limit,tp,sl", [(100, 110, 0), (0, 110, 90), (100, 99, 90), (100, 110, 101),
                                         (100, 110, 100), (100, -1, 90)])
def test_bracket_rejects_bad_prices(limit, tp, sl):
    with pytest.raises(BrokerError, match="nothing sent"):
        _bracket(limit, tp, sl)


def test_rebalance_missing_prices_and_fractional():
    assert rebalance.missing_prices({"A": 1.5}, {"B": 0.5}, {"A": 10}) == ["B"]
    assert rebalance.missing_prices({"A": 0}, {}, {}) == []
    out = rebalance.diff({"A": 1.5}, {}, 1000, {"A": 10}, min_trade_usd=0)
    assert out == [("A", "SELL", 1.5)]


def test_fee_applied_on_fill():
    cash = []
    fake = SimpleNamespace(_add_cash=lambda sid, x: cash.append(x),
                           _adjust_holding=lambda *a, **k: None)
    fake._apply_fill_trade = lambda r, q, px: None
    opt = {"sleeve": "putwrite", "symbol": "X", "kind": "option", "side": "SELL"}
    sm.SleeveManager._apply_fill(fake, opt, 2, 1.0)
    assert cash == [-2 * sm.OPTION_FEE_PER_CONTRACT]
    cash.clear()
    sm.SleeveManager._apply_fill(fake, {"sleeve": "m", "symbol": "A", "kind": "stock", "side": "SELL"}, 100, 50.0)
    assert cash == [-round(5000 * sm.STOCK_SELL_FEE_RATE, 4)]
    cash.clear()
    sm.SleeveManager._apply_fill(fake, {"sleeve": "m", "symbol": "A", "kind": "stock", "side": "BUY"}, 100, 50.0)
    assert cash == []


def test_expiry_intrinsic():
    sym = "AAPL240119C00150000"
    assert options_ml.expiry_intrinsic(sym, "2024-01-19", lambda u, d: 155.0) == 5.0
    assert options_ml.expiry_intrinsic(sym, "2024-01-19", lambda u, d: 140.0) == 0.0
    assert options_ml.expiry_intrinsic(sym, "2024-01-19", lambda u, d: None) is None
    assert options_ml.expiry_intrinsic("AAPL240119P00150000", "2024-01-19", lambda u, d: 140.0) == 10.0


class _DB:
    def __init__(self):
        self.added = []

    def proposals(self, n):
        return [{"id": 1, "symbol": "AAPL240119C00150000", "side": "BUY", "position_intent": "BUY_TO_OPEN",
                 "status": "placed", "limit_price": 2.0, "created_at": "x", "expiration_date": "2024-01-19",
                 "instrument_json": {"selection": {"a": 1}, "underlying": "AAPL"}}]

    def has_option_outcome(self, i):
        return False

    def get_trade(self, i):
        return {"entry_fill_price": 2.0}

    def add_option_outcome(self, row):
        self.added.append(row)


def test_outcomes_expired_uses_intrinsic_and_skips():
    today = date(2024, 2, 1)
    db = _DB()
    assert options_ml.record_closed_outcomes(db, set(), today, positions_ok=False) == 0
    assert options_ml.record_closed_outcomes(db, set(), today, close_lookup=lambda u, d: None) == 0
    assert options_ml.record_closed_outcomes(db, set(), today, close_lookup=lambda u, d: 155.0) == 1
    assert db.added[0]["exit_premium"] == 5.0 and db.added[0]["exit_reason"] == "expired"
