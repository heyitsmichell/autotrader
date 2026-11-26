"""Webull US positions text: option head line + Leg line must parse as the option, and the
stock rows that follow must never be read as options (2026-11-26: the exit manager armed
'6 x TLT' and '1278 x HPE' stock rows as options)."""

from app.engine.option_exits import parse_held_options
from app.portfolio import _option_rows

TEXT = """=== Positions ===
  Symbol: TLT  Position ID: VEH8X  Qty: 3  Available Qty: 3  Type: OPTION  Strategy: SINGLE  Cost: 1.30  Last: 1.15  Market Value: 345
            Leg: TLT  Side: LONG  Qty: 3  Type: OPTION  Option: CALL  Strike: 82  Exp: 2026-12-31
  Symbol: TLT  Position ID: P2  Qty: 6  Type: EQUITY  Cost: 81.69  Last: 82.10
  Symbol: QQQ  Position ID: P3  Qty: 8  Type: OPTION  Strategy: SINGLE  Cost: 12.54  Last: 7.87
            Leg: QQQ  Side: SHORT  Qty: 8  Type: OPTION  Option: PUT  Strike: 580  Exp: 2026-12-18
  Symbol: HPE  Position ID: P4  Qty: 1278  Type: EQUITY  Cost: 38.89  Last: 39.00
"""


def test_webull_layout_reads_options_not_stocks():
    rows = {r.symbol: r for r in parse_held_options(TEXT)}
    assert set(rows) == {"TLT261231C00082000", "QQQ261218P00580000"}
    tlt = rows["TLT261231C00082000"]
    assert (tlt.qty, tlt.cost, tlt.mark) == (3, 1.30, 1.15)
    assert rows["QQQ261218P00580000"].qty == -8          # SHORT leg


def test_occ_one_line_layout_still_works():
    rows = parse_held_options("Symbol: AAPL261218C00150000  Qty: 2  Cost: 3.10  Last: 3.40")
    assert [(r.symbol, r.qty) for r in rows] == [("AAPL261218C00150000", 2)]


def test_dashboard_option_rows_from_webull_layout():
    rows = {r["symbol"]: r for r in _option_rows(TEXT)}
    assert rows["TLT261231C00082000"]["strike"] == 82
    assert rows["TLT261231C00082000"]["option_type"] in ("CALL", "C")


def test_multi_leg_position_is_skipped():
    text = ("  Symbol: SPY  Qty: 1  Type: OPTION  Strategy: VERTICAL  Cost: 1.0\n"
            "      Leg: SPY  Side: LONG  Qty: 1  Type: OPTION  Option: CALL  Strike: 600  Exp: 2026-12-18\n"
            "      Leg: SPY  Side: SHORT  Qty: 1  Type: OPTION  Option: CALL  Strike: 610  Exp: 2026-12-18\n"
            "  Symbol: SPY  Qty: 10  Type: EQUITY  Cost: 590\n")
    assert parse_held_options(text) == []


LIVE = """=== Positions ===
  Symbol: QQQ  Position ID: M3IES5RIFPEGET0VNIIK8DRNI8  Qty: -8  Type: OPTION  Strategy: SINGLE  Cost: 12.54  Last: 7.87  Market Value: -6296.00  Unrealized P&L: 3736.00  Currency: USD
            Leg: QQQ  Leg ID: M3IES5RIFPEGET0VNIIK8DRNI8  Qty: N/A  Type: OPTION  Option: PUT  Strike: 580  Exp: 2026-12-18  Multiplier: 100  Deliverable: 100  ExpType: PM
  Symbol: HPE  Position ID: X1  Qty: 1278  Type: EQUITY  Cost: 38.89  Last: 39.00  Currency: USD
  Symbol: TLT  Position ID: VEH8CSIIC16V4C9EBNLLNL58H8  Qty: 3  Type: OPTION  Strategy: SINGLE  Cost: 1.30  Last: 1.11  Market Value: 333.00  Unrealized P&L: -57.00  Currency: USD
            Leg: TLT  Leg ID: VEH8CSIIC16V4C9EBNLLNL58H8  Qty: N/A  Type: OPTION  Option: CALL  Strike: 82  Exp: 2026-12-31  Multiplier: 100  Deliverable: 100  ExpType: PM
  Symbol: IEF  Position ID: X2  Qty: 5382  Type: EQUITY  Cost: 96.0  Last: 96.2  Currency: USD
"""


def test_live_webull_text_2026_11_26():
    rows = {r.symbol: (r.qty, r.cost, r.mark) for r in parse_held_options(LIVE)}
    assert rows == {"QQQ261218P00580000": (-8, 12.54, 7.87), "TLT261231C00082000": (3, 1.30, 1.11)}
