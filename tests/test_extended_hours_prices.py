"""Regression: premarket ADI limit 378.75 (previous regular close) filled at 343.00.

The Webull snapshot header ``<SYM> Price:`` is the REGULAR-session price; premarket/after-hours
live in an ``ExtHr Price:`` line and overnight in ``OVN Price:``. Format source:
webull_openapi_mcp.formatters.format_stock_snapshot.
"""
import dataclasses
from datetime import datetime

import pytest

from app.broker import Broker, parse_snapshot
from app.broker.parsers import live_price_for_session
from app.market import ET, set_test_time
from app.risk import RiskManager

PRE = datetime(2026, 11, 24, 7, 38, tzinfo=ET)     # Tuesday premarket
CORE = datetime(2026, 11, 24, 10, 0, tzinfo=ET)
NIGHT = datetime(2026, 11, 24, 22, 0, tzinfo=ET)

SNAP_EXT = """=== Stock Snapshot ===
       ADI  Price:     378.75  PreClose:     378.75  Change:     0.00  Change%:     0.00%
            Open:     377.00  High:     380.00  Low:     376.00  Close:     378.75  Vol:      1200000
            Bid:     0 x      0  Ask:     0 x      0
            ExtHr Price:     328.06  High:     330.00  Low:     326.00  Change:   -50.69 (-13.4%)  Vol:        45000
            OVN Price:     330.10  High:     331.00  Low:     329.00  Change:   -48.65 (-12.8%)  Vol:         900
            OVN Bid:     330.00 x    100  OVN Ask:     330.20 x    100
       TXN  Price:     291.19  PreClose:     291.19  Change:     0.00  Change%:     0.00%
            Open:     290.00  High:     292.00  Low:     289.00  Close:     291.19  Vol:      800000
"""


@pytest.fixture(autouse=True)
def _clock():
    yield
    from conftest import TEST_TIME_ET
    set_test_time(TEST_TIME_ET.astimezone(ET))


class SnapBroker(Broker):
    def __init__(self, settings, text):
        super().__init__(dataclasses.replace(settings, broker_mode="webull"))
        self.text = text

    async def call(self, tool, **kw):
        return self.text


def test_parser_extracts_ext_and_ovn():
    snaps = parse_snapshot(SNAP_EXT)
    adi = snaps["ADI"]
    assert adi["price"] == adi["regular_price"] == 378.75
    assert adi["ext_price"] == 328.06 and adi["ovn_price"] == 330.10
    assert adi["volume"] == 1_200_000 and adi["prev_close"] == 378.75
    assert snaps["TXN"]["ext_price"] is None and snaps["TXN"]["ovn_price"] is None
    assert snaps["TXN"]["volume"] == 800_000


def test_live_price_for_session():
    s = parse_snapshot(SNAP_EXT)["ADI"]
    assert live_price_for_session(s, "CORE") == (378.75, "regular", False)
    assert live_price_for_session(s, "ALL") == (328.06, "extended", False)
    assert live_price_for_session(s, "NIGHT") == (330.10, "overnight", False)
    t = parse_snapshot(SNAP_EXT)["TXN"]
    assert live_price_for_session(t, "ALL") == (291.19, "regular", True)
    assert live_price_for_session(t, "CORE")[2] is False


@pytest.mark.asyncio
async def test_last_price_per_session(test_settings):
    b = SnapBroker(test_settings, SNAP_EXT)
    for when, expect in ((CORE, 378.75), (PRE, 328.06), (NIGHT, 330.10)):
        set_test_time(when)
        b._snapshot_cache.clear()
        assert await b.last_price("ADI") == expect
        q = await b.quote("ADI")
        assert q.last == expect and q.stale is False
    set_test_time(PRE)
    b._snapshot_cache.clear()
    assert await b.last_price("TXN") == 291.19       # best available, for exits
    assert (await b.quote("TXN")).stale is True
    assert await b.is_price_stale("TXN") is True and await b.is_price_stale("ADI") is False


class FakeRiskBroker:
    def __init__(self, snap_broker, held=0.0):
        self.b, self.held = snap_broker, held

    async def last_price(self, s): return await self.b.last_price(s)
    async def get_snapshot(self, s): return await self.b.get_snapshot(s)
    async def quote(self, s): return await self.b.quote(s)
    async def positions(self): return {"TXN": self.held, "ADI": self.held}
    async def day_pnl(self): return 0.0


def _p(sym, side, limit, sl=None, tp=None, qty=1):
    return {"symbol": sym, "side": side, "quantity": qty, "limit_price": limit, "sl": sl, "tp": tp,
            "created_at": None}


async def _verdict(test_settings, test_db, p, text=SNAP_EXT, held=0.0):
    set_test_time(PRE)
    sb = SnapBroker(test_settings, text)
    s = dataclasses.replace(test_settings, bracket_orders=False)
    return await RiskManager(s).check(p, FakeRiskBroker(sb, held), test_db, kill_switch=False, at_execution=False,
                                      watchlist=["ADI", "TXN"])


@pytest.mark.asyncio
async def test_adi_case_band_uses_ext_price(test_settings, test_db):
    v = await _verdict(test_settings, test_db, _p("ADI", "BUY", 378.75))
    assert not v.ok
    assert any("Price band" in c["name"] and not c["ok"] for c in v.checks) or \
        any("328.06" in r for r in v.reasons)


@pytest.mark.asyncio
async def test_stale_only_premarket_buy_rejected_sell_allowed(test_settings, test_db):
    v = await _verdict(test_settings, test_db, _p("TXN", "BUY", 291.19))
    assert not v.ok
    assert any("No extended-hours price for TXN" in r and "stale" in r for r in v.reasons)
    v = await _verdict(test_settings, test_db, _p("TXN", "SELL", 291.19), held=5)
    assert not any("extended-hours" in r for r in v.reasons)
    assert v.last_price == 291.19


@pytest.mark.asyncio
async def test_buy_priced_off_ext_price_passes_stale_check(test_settings, test_db):
    v = await _verdict(test_settings, test_db, _p("ADI", "BUY", 328.06))
    assert not any("extended-hours" in r or "Price band" in r for r in v.reasons)


@pytest.mark.asyncio
async def test_stop_above_live_rejected(test_settings, test_db):
    # The ADI case: stop 348.45 is above the live 328.06.
    v = await _verdict(test_settings, test_db, _p("ADI", "BUY", 328.06, sl=348.45, tp=401.48))
    assert any("already below the stop" in r for r in v.reasons)
    v = await _verdict(test_settings, test_db, _p("ADI", "BUY", 328.06, sl=300.0, tp=360.0))
    assert not any("below the stop" in r for r in v.reasons)


@pytest.mark.asyncio
async def test_proposal_levels_anchor_to_live_price(test_settings):
    from app import ta
    set_test_time(PRE)
    b = SnapBroker(test_settings, SNAP_EXT)
    px = await b.last_price("ADI")
    sl, tp = ta.levels(round(px, 2), 6.0, test_settings)
    assert sl < px < tp and abs(px - sl) < 0.2 * px     # anchored to 328.06, not 378.75
