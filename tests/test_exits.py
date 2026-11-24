"""Tests for signal-based exits (app/exits.py)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import exits

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)


def cfg(**kw):
    base = dict(exit_max_hold_days=20, exit_confirm_readings=3, exit_confirm_minutes=10,
                exit_ta_floor=-0.10, exit_ml_margin=0.05, exit_ml_drop_margin=0.05,
                ml_enabled=True, ml_min_p=0.52,
                consensus_min_news=3, consensus_sent_min=-0.25, fractional_enabled=False,
                fractional_whitelist=["AAPL"], fractional_decimals=4)
    base.update(kw)
    return SimpleNamespace(**base)


def snap(trend="up", ta=0.3, minutes_ago=0, **kw):
    return {"trend": trend, "ta_score": ta,
            "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), **kw}


# -- hysteresis ---------------------------------------------------------------------------
def test_small_ta_dip_in_range_does_not_exit():
    sig = snap("range", 0.05)          # below the +0.15 entry bar but above the -0.10 exit bar
    assert exits.decide_exit(sig, [sig], cfg()) is None


def test_ta_below_exit_floor_in_range_exits():
    sig = snap("range", -0.15)
    d = exits.decide_exit(sig, [sig], cfg())
    assert d and d.kind == "weak_signal"


def test_weak_ta_but_trend_up_holds():
    sig = snap("up", -0.5)
    assert exits.decide_exit(sig, [sig], cfg()) is None


def test_bad_sentiment_with_enough_headlines_exits():
    sig = snap("range", 0.1, sent_score=-0.4, sent_n=5)
    assert exits.decide_exit(sig, [sig], cfg()).kind == "weak_signal"


def test_bad_sentiment_with_few_headlines_ignored():
    sig = snap("range", 0.1, sent_score=-0.9, sent_n=1)
    assert exits.decide_exit(sig, [sig], cfg()) is None


# -- trend confirmation -------------------------------------------------------------------
def test_single_down_reading_is_not_enough():
    hist = [snap("down", 0.5, 0), snap("up", 0.5, 5), snap("up", 0.5, 10)]
    assert not exits.trend_down_confirmed(hist, 3, 10)
    assert exits.decide_exit(hist[0], hist, cfg()) is None or \
        exits.decide_exit(hist[0], hist, cfg()).kind != "trend_down"


def test_three_down_readings_spanning_enough_time_exits():
    hist = [snap("down", 0.5, 0), snap("down", 0.5, 5), snap("down", 0.5, 10)]
    d = exits.decide_exit(hist[0], hist, cfg())
    assert d and d.kind == "trend_down"


def test_three_down_readings_too_close_together_do_not_exit():
    hist = [snap("down", 0.5, 0), snap("down", 0.5, 1), snap("down", 0.5, 2)]
    assert not exits.trend_down_confirmed(hist, 3, 10)


def test_not_enough_history_does_not_confirm():
    assert not exits.trend_down_confirmed([snap("down")], 3, 10)


def test_single_reading_mode_matches_old_behaviour():
    assert exits.trend_down_confirmed([snap("down")], 1, 0)


# -- ML -----------------------------------------------------------------------------------
def test_ml_just_below_threshold_holds_but_well_below_exits():
    assert exits.decide_exit(snap("up", 0.5, ml_p=0.50), [snap("up")], cfg()) is None
    d = exits.decide_exit(snap("up", 0.5, ml_p=0.40), [snap("up")], cfg())
    assert d and d.kind == "ml_drop"


def test_ml_ignored_when_disabled():
    assert exits.decide_exit(snap("up", 0.5, ml_p=0.1), [snap("up")], cfg(ml_enabled=False)) is None


# -- time stop ----------------------------------------------------------------------------
def test_time_stop_fires_at_limit_and_not_before():
    sig = snap("up", 0.5)
    assert exits.decide_exit(sig, [sig], cfg(), held_for_days=19.9) is None
    assert exits.decide_exit(sig, [sig], cfg(), held_for_days=20).kind == "time_stop"


def test_time_stop_off_and_unknown_entry():
    sig = snap("up", 0.5)
    assert exits.decide_exit(sig, [sig], cfg(exit_max_hold_days=0), held_for_days=999) is None
    assert exits.decide_exit(sig, [sig], cfg(), held_for_days=None) is None


def test_held_days_parses_naive_and_aware_timestamps():
    assert abs(exits.held_days("2026-11-13T15:00:00", NOW) - 10) < 1e-6
    assert abs(exits.held_days("2026-11-13T15:00:00+00:00", NOW) - 10) < 1e-6
    assert exits.held_days(None, NOW) is None
    assert exits.held_days("garbage", NOW) is None


# -- sell quantity ------------------------------------------------------------------------
def test_whole_quantity_unchanged():
    assert exits.sell_quantity(5.0, cfg(), "AAPL") == 5


def test_fractional_remainder_not_left_behind_when_enabled():
    s = cfg(fractional_enabled=True)
    assert exits.sell_quantity(2.6, s, "AAPL") == 2.6


def test_fractional_rounds_down_never_over_sells():
    s = cfg(fractional_enabled=True, fractional_decimals=2)
    assert exits.sell_quantity(2.6789, s, "AAPL") == 2.67


def test_fractional_disabled_or_not_whitelisted_sells_whole_shares():
    assert exits.sell_quantity(2.6, cfg(), "AAPL") == 2
    assert exits.sell_quantity(2.6, cfg(fractional_enabled=True), "XLF") == 2


def test_less_than_one_share_without_fractional_is_zero():
    assert exits.sell_quantity(0.6, cfg(), "AAPL") == 0
    assert exits.sell_quantity(0, cfg(), "AAPL") == 0
