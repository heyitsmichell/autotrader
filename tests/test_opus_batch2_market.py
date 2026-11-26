"""S9/C7: early-close after-hours ends 17:00; no overnight session around holidays/early closes."""
from datetime import datetime

from app import market as m
from app.market import ET


def _at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_thanksgiving_evening_has_no_night():
    assert m.order_session(_at(2026, 11, 26, 20, 30)) is None


def test_wednesday_before_thanksgiving_has_no_night():
    assert m.order_session(_at(2026, 11, 25, 20, 30)) is None
    assert m.order_session(_at(2026, 11, 26, 2, 0)) is None


def test_early_close_day_windows():
    assert m.order_session(_at(2026, 11, 27, 12, 30)) == "CORE"
    assert m.order_session(_at(2026, 11, 27, 16, 30)) == "ALL"
    assert m.order_session(_at(2026, 11, 27, 17, 30)) is None
    assert m.order_session(_at(2026, 11, 27, 20, 30)) is None
    assert not m.market_is_open_extended(_at(2026, 11, 27, 18, 0))
    assert m.market_is_open_extended(_at(2026, 11, 27, 16, 59))


def test_normal_tuesday_unchanged():
    assert m.order_session(_at(2026, 12, 1, 3, 0)) == "NIGHT"
    assert m.order_session(_at(2026, 12, 1, 8, 0)) == "ALL"
    assert m.order_session(_at(2026, 12, 1, 12, 0)) == "CORE"
    assert m.order_session(_at(2026, 12, 1, 19, 0)) == "ALL"
    assert m.order_session(_at(2026, 12, 1, 20, 30)) == "NIGHT"
    assert m.order_session(_at(2026, 12, 2, 1, 0)) == "NIGHT"


def test_sunday_evening_night_and_friday_evening_closed():
    assert m.order_session(_at(2026, 11, 29, 21, 0)) == "NIGHT"
    assert m.order_session(_at(2026, 11, 30, 2, 0)) == "NIGHT"
    assert m.order_session(_at(2026, 12, 4, 21, 0)) is None


def test_overnight_closed_dates_override(monkeypatch):
    monkeypatch.setenv("OVERNIGHT_CLOSED_DATES", "2026-12-01")
    assert m.order_session(_at(2026, 12, 1, 20, 30)) is None
    assert m.order_session(_at(2026, 12, 2, 1, 0)) is None
    assert m.order_session(_at(2026, 12, 2, 20, 30)) == "NIGHT"
