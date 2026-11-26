"""Golden Webull-format fixtures (output of the real webull_openapi_mcp formatters)
run through the app parsers, plus a mock-vs-real format contract test."""
import asyncio
import re
from pathlib import Path

import pytest

from app.broker.parsers import parse_option_positions, parse_positions, parse_snapshot
from app.engine.option_exits import parse_held_options
from app.portfolio import parse_balance, parse_orders, parse_position_rows

FX = Path(__file__).parent / "fixtures" / "webull"


def _fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def test_parse_positions_skips_options_keeps_equity():
    assert parse_positions(_fx("positions_us.txt")) == {"AAPL": 10.0, "SPY": 2.5}


def test_parse_position_rows_equity_fields():
    rows = {r["symbol"]: r for r in parse_position_rows(_fx("positions_us.txt"))}
    aapl = rows["AAPL"]
    assert aapl["qty"] == 10 and aapl["cost"] == 150.25 and aapl["last"] == 172.4
    assert aapl["market_value"] == 1724.0 and aapl["unrealized"] == 221.5
    assert rows["SPY"]["qty"] == 2.5
    assert set(rows) == {"AAPL", "SPY"}      # option rows are not stock rows


def test_parse_held_options_webull_legs():
    held = {h.symbol: h for h in parse_held_options(_fx("positions_us.txt"))}
    assert set(held) == {"QQQ261218P00580000", "TLT261231C00082000"}
    assert held["QQQ261218P00580000"].qty == -8 and held["QQQ261218P00580000"].cost == 12.54
    assert held["TLT261231C00082000"].qty == 3 and held["TLT261231C00082000"].mark == 1.11


def test_parse_option_positions_does_not_crash_on_webull_layout():
    # the block parser expects OCC Symbols (mock layout); on the real layout it must not raise
    rows = parse_option_positions(_fx("positions_us.txt"))
    assert isinstance(rows, list)


def test_parse_balance():
    b = parse_balance(_fx("balance_us.txt"))
    assert b["currency"] == "USD" and b["cash"] == 25000.50
    assert b["market_value"] == 3004.25 and b["net_liq"] == 28004.75
    assert b["day_pnl"] == -120.30 and b["buying_power"] == 48000.0


def test_parse_orders():
    orders = parse_orders(_fx("open_orders_us.txt"))
    assert [o["client_order_id"] for o in orders] == ["C1", "C2"]
    assert orders[0]["symbol"] == "AAPL" and orders[0]["side"] == "BUY"
    assert orders[0]["quantity"] == 10 and orders[0]["limit_price"] == 170.0
    assert orders[1]["stop_price"] == 495.0 and orders[1]["combo_type"] == "MASTER"
    assert orders[1]["limit_price"] is None


def test_parse_snapshot():
    snap = parse_snapshot(_fx("snapshot_us.txt"))["AAPL"]
    assert snap["price"] == 172.4 and snap["prev_close"] == 171.0
    assert snap["bid"] == 172.39 and snap["ask"] == 172.41


def _mock_text(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_STATE_FILE", str(tmp_path / "mock_state.json"))
    from app.mock import server, state
    st = state.load()
    st["positions"] = {"AAPL": {"qty": 10.0, "cost": 150.25}}
    state.save(st)
    pos = asyncio.run(server.get_account_positions(state.ACCOUNT_ID))
    bal = asyncio.run(server.get_account_balance(state.ACCOUNT_ID))
    return pos, bal


def test_mock_positions_and_balance_match_real_format(tmp_path, monkeypatch):
    pos, bal = _mock_text(tmp_path, monkeypatch)
    real_row = next(r for r in parse_position_rows(_fx("positions_us.txt")) if r["symbol"] == "AAPL")
    mock_row = next(r for r in parse_position_rows(pos) if r["symbol"] == "AAPL")
    assert set(mock_row) == set(real_row)
    assert parse_positions(pos) == {"AAPL": 10.0}
    assert set(parse_balance(bal)) == set(parse_balance(_fx("balance_us.txt")))
    # head-line labels the real EQUITY row carries but the mock omits (known gap, parsers derive them)
    labels = lambda t: set(re.findall(r"([A-Z][A-Za-z &]+?):", next(
        l for l in t.splitlines() if "Type: EQUITY" in l)))
    missing = labels(_fx("positions_us.txt")) - labels(pos)
    assert not missing, missing
