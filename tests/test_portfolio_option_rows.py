"""Portfolio snapshot exposes option holdings (parse_position_rows keeps equities only)."""
from app.portfolio import _option_rows, parse_position_rows

TEXT = (
    "Symbol: XLF Type: EQUITY Qty: 10 Cost: 50.00 Last: 51.00\n"
    "Symbol: XLF260102C00052000 Type: OPTION Qty: 1 Cost: 3.10 Mark: 3.40 Unrealized: 30.00\n"
)


def test_option_rows_parsed_and_equity_kept_separate():
    eq = parse_position_rows(TEXT)
    assert [r["symbol"] for r in eq] == ["XLF"]
    opts = _option_rows(TEXT)
    assert len(opts) == 1 and opts[0]["symbol"] == "XLF260102C00052000"
    assert opts[0]["underlying"] == "XLF" and opts[0]["option_type"] == "CALL"
    assert opts[0]["qty"] == 1 and opts[0]["strike"] == 52.0
    assert opts[0]["cost_basis"] == 3.10 and opts[0]["mark_price"] == 3.40
    assert opts[0]["unrealized_pnl"] == 30.0


def test_option_rows_never_raise():
    assert _option_rows("") == [] and _option_rows(None) == []
