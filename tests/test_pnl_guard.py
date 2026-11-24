"""An all-zero broker balance must not become a $0 account, a $0 day P&L or an equity point."""
from app.portfolio import balance_is_empty, parse_balance

GOOD = ("Total Cash Balance: 1000002.21\nTotal Market Value: 0.00\nTotal Unrealized P&L: 0.00\n"
        "Net Liquidation: 1000002.21\nDay P&L: 2.21\nBuying Power: 999659.65\n")
ZERO = ("Total Cash Balance: 0.00\nTotal Market Value: 0.00\nTotal Unrealized P&L: 0.00\n"
        "Net Liquidation: 0.00\nDay P&L: 0.00\nBuying Power: 0.00\n")


def test_zero_balance_is_empty():
    assert balance_is_empty(parse_balance(ZERO))
    assert balance_is_empty({}) and balance_is_empty(None)
    assert balance_is_empty({"net_liq": None, "cash": None})


def test_real_balances_are_not_empty():
    assert not balance_is_empty(parse_balance(GOOD))
    # all positions sold, nothing left in cash margin, but equity and buying power exist
    assert not balance_is_empty({"net_liq": 5.0, "cash": 0.0, "market_value": 5.0, "buying_power": 0.0})
    # a legitimately flat day: P&L is zero but the account is not
    assert not balance_is_empty({"net_liq": 1000000.0, "cash": 1000000.0, "day_pnl": 0.0})