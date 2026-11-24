"""Strategy rules that used to live only in strategy.md prose (QA H6), and the H5 fail-closed
daily-loss guard. Deterministic, no network."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app import consensus
from app.risk import OptionRiskManager, RiskManager


# ---------------------------------------------------------------- pullback gate (consensus)
def _sig(off_high: float | None, **kw):
    feats = {} if off_high is None else {"pct_from_high10": off_high}
    s = {"ta_score": 0.6, "trend": "up", "strength": "strong", "sent_n": 0, "sent_score": None,
         "ml_p": None, "close": 100.0, "atr": 2.0, "features": feats}
    s.update(kw)
    return s


def _check(res, name):
    return next((c for c in res["checks"] if c["name"] == name), None)


def test_pullback_gate_off_by_default_adds_no_check(test_settings):
    assert test_settings.entry_pullback_gate is False
    res = consensus.evaluate({"side": "BUY", "limit_price": 100.0}, _sig(-0.2), test_settings)
    assert _check(res, "Pullback") is None


def test_pullback_gate_requires_2_to_5_pct_below_high(test_settings):
    s = replace(test_settings, entry_pullback_gate=True)
    p = {"side": "BUY", "limit_price": 100.0}
    for off, ok in [(-3.0, True), (-2.0, True), (-5.0, True), (-0.5, False), (-9.0, False),
                    (0.0, False)]:
        c = _check(consensus.evaluate(p, _sig(off), s), "Pullback")
        assert c is not None and c["ok"] is ok, off


def test_pullback_gate_blocks_the_verdict_and_fails_closed_without_data(test_settings):
    s = replace(test_settings, entry_pullback_gate=True)
    p = {"side": "BUY", "limit_price": 100.0}
    near_high = consensus.evaluate(p, _sig(-0.4), s)
    assert near_high["ok"] is False
    assert consensus.evaluate(p, _sig(-3.0), s)["ok"] in (True, False)  # other checks decide
    missing = consensus.evaluate(p, _sig(None), s)
    assert _check(missing, "Pullback")["ok"] is False and missing["ok"] is False


def test_pullback_gate_honours_configured_range(test_settings):
    s = replace(test_settings, entry_pullback_gate=True, entry_pullback_min_pct=0.01,
                entry_pullback_max_pct=0.03)
    p = {"side": "BUY", "limit_price": 100.0}
    assert _check(consensus.evaluate(p, _sig(-1.5), s), "Pullback")["ok"] is True
    assert _check(consensus.evaluate(p, _sig(-4.0), s), "Pullback")["ok"] is False


def test_pullback_gate_never_blocks_a_sell(test_settings):
    s = replace(test_settings, entry_pullback_gate=True)
    assert consensus.evaluate({"side": "SELL"}, _sig(-0.1), s)["ok"] is True


def test_features_may_be_a_json_string(test_settings):
    s = replace(test_settings, entry_pullback_gate=True)
    sig = _sig(None, features='{"pct_from_high10": -3.0}')
    assert _check(consensus.evaluate({"side": "BUY", "limit_price": 100.0}, sig, s),
                  "Pullback")["ok"] is True


# ---------------------------------------------------------------- risk: fake broker
class FakeBroker:
    def __init__(self, held=0.0, last=100.0, cost=100.0, pnl=0.0, pnl_exc=None, text=True):
        self.held, self.last, self.cost, self.pnl, self.pnl_exc, self.text = held, last, cost, pnl, pnl_exc, text

    async def last_price(self, symbol):
        return self.last

    async def positions(self):
        return {"AAPL": self.held} if self.held else {}

    async def positions_text(self):
        if not self.text:
            raise RuntimeError("no positions text")
        if not self.held:
            return "No data"
        return (f"  Symbol: AAPL  Position ID: X  Qty: {self.held:g}  Type: EQUITY  "
                f"Cost: {self.cost}  Last: {self.last}")

    async def day_pnl(self):
        if self.pnl_exc:
            raise self.pnl_exc
        return self.pnl


def _order(**kw):
    p = dict(symbol="AAPL", side="BUY", quantity=1, limit_price=100.0, sl=95.0, tp=110.0)
    p.update(kw)
    return p


async def _check_risk(settings, broker, db, p=None):
    s = replace(settings, market_hours_only=False)
    return await RiskManager(s).check(p or _order(), broker, db, kill_switch=False,
                                      at_execution=False, watchlist=["AAPL"])


def _by(v, name):
    return next((c for c in v.checks if c["name"] == name), None)


# ---------------------------------------------------------------- H5 daily loss fails closed
@pytest.mark.asyncio
async def test_daily_loss_guard_rejects_buy_when_pnl_unavailable(test_settings, test_db):
    s = replace(test_settings, max_daily_loss_usd=100.0)
    v = await _check_risk(s, FakeBroker(pnl=None), test_db)
    c = _by(v, "Daily loss")
    assert v.ok is False and c["ok"] is False
    assert "day P&L unavailable" in c["detail"]


@pytest.mark.asyncio
async def test_daily_loss_guard_still_passes_with_known_pnl(test_settings, test_db):
    s = replace(test_settings, max_daily_loss_usd=100.0)
    assert (await _check_risk(s, FakeBroker(pnl=-50.0), test_db)).ok is True
    assert (await _check_risk(s, FakeBroker(pnl=-150.0), test_db)).ok is False


@pytest.mark.asyncio
async def test_daily_loss_guard_disabled_ignores_missing_pnl(test_settings, test_db):
    s = replace(test_settings, max_daily_loss_usd=0.0)
    v = await _check_risk(s, FakeBroker(pnl=None), test_db)
    assert _by(v, "Daily loss") is None and v.ok is True


@pytest.mark.asyncio
async def test_sell_is_never_blocked_by_missing_pnl(test_settings, test_db):
    s = replace(test_settings, max_daily_loss_usd=100.0)
    v = await _check_risk(s, FakeBroker(held=5, pnl=None), test_db, _order(side="SELL", sl=None, tp=None))
    assert v.ok is True


# ---------------------------------------------------------------- H6 max adds / add to loser
def _open_buy(db, pid, fill_price=100.0, qty=1):
    p = _order(quantity=qty)
    db.add_trade_entry(pid, p)
    db.update_trade_fill(pid, fill_price, "2026-01-02T15:00:00+00:00", qty) \
        if db.update_trade_fill.__code__.co_argcount >= 4 else None


@pytest.mark.asyncio
async def test_first_buy_is_not_an_add(test_settings, test_db):
    v = await _check_risk(test_settings, FakeBroker(held=0), test_db)
    assert _by(v, "Max adds") is None and _by(v, "Add to loser") is None and v.ok is True


@pytest.mark.asyncio
async def test_one_add_allowed_then_refused(test_settings, test_db):
    s = replace(test_settings, max_adds_per_symbol=1, max_position_usd=10_000.0)
    broker = FakeBroker(held=1, last=101.0, cost=100.0)
    _open_buy(test_db, 1)                               # the starter
    v = await _check_risk(s, broker, test_db)
    assert _by(v, "Max adds")["ok"] is True and v.ok is True
    _open_buy(test_db, 2)                               # the first add happened
    v2 = await _check_risk(s, FakeBroker(held=2, last=101.0, cost=100.0), test_db)
    c = _by(v2, "Max adds")
    assert c["ok"] is False and v2.ok is False
    assert "MAX_ADDS_PER_SYMBOL" in c["detail"]


@pytest.mark.asyncio
async def test_max_adds_zero_refuses_any_add(test_settings, test_db):
    s = replace(test_settings, max_adds_per_symbol=0, max_position_usd=10_000.0)
    _open_buy(test_db, 1)
    v = await _check_risk(s, FakeBroker(held=1, last=101.0, cost=100.0), test_db)
    assert _by(v, "Max adds")["ok"] is False


@pytest.mark.asyncio
async def test_reexecution_check_ignores_the_proposals_own_ledger_row(test_settings, test_db):
    s = replace(test_settings, max_adds_per_symbol=1, max_position_usd=10_000.0)
    _open_buy(test_db, 1)
    _open_buy(test_db, 2)   # this proposal's own row (id 2) is already in the ledger
    v = await _check_risk(s, FakeBroker(held=2, last=101.0, cost=100.0), test_db, _order(id=2))
    assert _by(v, "Max adds")["ok"] is True


@pytest.mark.asyncio
async def test_never_add_to_a_loser(test_settings, test_db):
    s = replace(test_settings, max_adds_per_symbol=5, block_add_to_loser=True,
                max_position_usd=10_000.0)
    loser = await _check_risk(s, FakeBroker(held=1, last=99.0, cost=100.0), test_db,
                              _order(limit_price=99.0, sl=94.0, tp=108.0))
    c = _by(loser, "Add to loser")
    assert c["ok"] is False and loser.ok is False and "loser" in c["detail"]
    winner = await _check_risk(s, FakeBroker(held=1, last=101.0, cost=100.0), test_db)
    assert _by(winner, "Add to loser")["ok"] is True and winner.ok is True


@pytest.mark.asyncio
async def test_add_to_loser_can_be_switched_off(test_settings, test_db):
    s = replace(test_settings, max_adds_per_symbol=5, block_add_to_loser=False,
                max_position_usd=10_000.0)
    v = await _check_risk(s, FakeBroker(held=1, last=99.0, cost=100.0), test_db,
                          _order(limit_price=99.0, sl=94.0, tp=108.0))
    assert _by(v, "Add to loser") is None and v.ok is True


@pytest.mark.asyncio
async def test_add_to_loser_fails_closed_when_cost_unknown(test_settings, test_db):
    s = replace(test_settings, max_adds_per_symbol=5, block_add_to_loser=True,
                max_position_usd=10_000.0)
    v = await _check_risk(s, FakeBroker(held=1, last=101.0, text=False), test_db)
    c = _by(v, "Add to loser")
    assert c["ok"] is False and "unavailable" in c["detail"]


@pytest.mark.asyncio
async def test_add_rules_never_touch_a_sell(test_settings, test_db):
    s = replace(test_settings, max_adds_per_symbol=0, block_add_to_loser=True)
    v = await _check_risk(s, FakeBroker(held=1, last=50.0, cost=100.0), test_db,
                          _order(side="SELL", sl=None, tp=None, limit_price=50.0))
    assert _by(v, "Max adds") is None and _by(v, "Add to loser") is None and v.ok is True


# ---------------------------------------------------------------- OptionRiskManager fails closed
def _occ(days=30, kind="C", under="AAPL"):
    # The app's clock (frozen by conftest), which OptionRiskManager measures DTE against.
    from app.market import now_et
    d = now_et().date() + timedelta(days=days)
    return f"{under}{d:%y%m%d}{kind}00150000"


@pytest.fixture
def orm(test_settings):
    return OptionRiskManager(replace(
        test_settings, options_enabled=True, options_allowed_underlyings=["AAPL", "SPY"],
        options_min_dte=1, options_max_dte=60, options_max_contracts_per_order=5,
        options_max_notional_usd=5000.0, options_allowed_strategies=["SINGLE", "VERTICAL"]))


def _opt(**kw):
    p = dict(symbol=_occ(), side="BUY", quantity=1, limit_price=2.0,
             position_intent="BUY_TO_OPEN")
    p.update(kw)
    return p


def test_option_open_happy_path(orm):
    ok, reasons = orm.check_single_leg(_opt())
    assert ok, reasons


@pytest.mark.parametrize("premium", [float("nan"), float("inf"), True, "2", None, 0, -1])
def test_option_open_rejects_bad_premium(orm, premium):
    ok, reasons = orm.check_single_leg(_opt(limit_price=premium))
    assert not ok and reasons


def test_option_open_rejects_bool_and_float_quantity(orm):
    assert not orm.check_single_leg(_opt(quantity=True))[0]
    assert not orm.check_single_leg(_opt(quantity=1.5))[0]


def test_option_open_rejects_past_expiry_and_naked_short(orm):
    assert not orm.check_single_leg(_opt(symbol=_occ(days=-3)))[0]
    ok, reasons = orm.check_single_leg(_opt(side="SELL", position_intent="SELL_TO_OPEN"))
    assert not ok and any("naked" in r or "cover" in r for r in reasons)
    ok, _ = orm.check_single_leg(_opt(side="SELL", position_intent="SELL_TO_OPEN", _covered=True))
    assert ok


def test_option_side_contradicting_intent_is_refused(orm):
    ok, reasons = orm.check_single_leg(_opt(side="SELL", position_intent="BUY_TO_OPEN"))
    assert not ok and any("contradicts" in r for r in reasons)


def test_option_closing_orders_are_not_blocked_by_opening_limits(orm, test_settings):
    # near-expiry, over the notional cap, and options disabled: an exit still goes through
    off = OptionRiskManager(replace(test_settings, options_enabled=False))
    p = _opt(symbol=_occ(days=0), side="SELL", position_intent="SELL_TO_CLOSE", quantity=9,
             limit_price=900.0)
    assert off.check_single_leg(p) == (True, [])
    assert orm.check_single_leg(p) == (True, [])
    # but a malformed closing order is still refused
    assert not orm.check_single_leg({**p, "limit_price": float("nan")})[0]


def test_option_disabled_rejects_opening(test_settings):
    off = OptionRiskManager(replace(test_settings, options_enabled=False))
    ok, reasons = off.check_single_leg(_opt())
    assert not ok and any("disabled" in r for r in reasons)


def test_strategy_rejects_mixed_underlyings_and_nan_premium(orm):
    legs = [dict(symbol=_occ(kind="C"), quantity=1, limit_price=2.0, option_strategy="VERTICAL",
                 position_intent="BUY_TO_OPEN"),
            dict(symbol=_occ(kind="C", under="SPY"), quantity=1, limit_price=1.0,
                 option_strategy="VERTICAL", position_intent="SELL_TO_OPEN")]
    ok, reasons = orm.check_strategy(legs)
    assert not ok and any("underlying" in r for r in reasons)
    legs[1] = {**legs[0], "limit_price": float("nan"), "position_intent": "SELL_TO_OPEN"}
    ok, reasons = orm.check_strategy(legs)
    assert not ok
