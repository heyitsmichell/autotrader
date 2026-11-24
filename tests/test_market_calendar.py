"""Tests for market calendar."""

import pytest
from datetime import date, datetime


class TestMarketCalendar:
    """Test market calendar functions."""

    def test_nyse_holidays(self):
        """Should have correct NYSE holidays."""
        from app import market
        cases = {
            date(2026, 4, 3): "Good Friday",
            date(2026, 7, 3): "Independence Day",
            date(2026, 11, 26): "Thanksgiving Day",
            date(2026, 6, 19): "Juneteenth",
            date(2027, 6, 18): "Juneteenth",
            date(2027, 12, 24): "Christmas Day",
            date(2028, 1, 17): "Martin Luther King Jr. Day",
            date(2026, 5, 25): "Memorial Day",
            date(2025, 4, 18): "Good Friday",
            date(2026, 1, 1): "New Year's Day",
        }
        for d, name in cases.items():
            assert market.holiday_name(d) == name, f"{d}: expected {name}, got {market.holiday_name(d)}"

    def test_no_makeup_day_when_jan1_saturday(self):
        """No make-up day when Jan 1 is a Saturday."""
        from app import market
        assert market.is_trading_day(date(2027, 12, 31))

    def test_day_after_thanksgiving_closes_early(self):
        """Day after Thanksgiving should close at 1 pm."""
        from app import market
        assert market.is_trading_day(date(2026, 11, 27))
        assert market.session_close(date(2026, 11, 27)).hour == 13

    def test_christmas_eve_and_july3_close_early(self):
        """Christmas Eve and July 3 should close early."""
        from app import market
        assert market.session_close(date(2025, 12, 24)).hour == 13
        assert market.session_close(date(2025, 7, 3)).hour == 13

    def test_no_early_close_when_july3_is_holiday(self):
        """No early close when July 3 is the holiday."""
        from app import market
        assert market.session_close(date(2026, 7, 2)).hour == 16

    def test_early_close_respected(self):
        """Early close should be respected."""
        from app import market
        from datetime import timezone
        et = market.ET
        assert market.market_is_open(datetime(2026, 11, 27, 12, 59, tzinfo=et))
        assert not market.market_is_open(datetime(2026, 11, 27, 13, 0, tzinfo=et))

    def test_closed_on_good_friday(self):
        """Should be closed on Good Friday."""
        from app import market
        from datetime import timezone
        et = market.ET
        assert not market.market_is_open(datetime(2026, 4, 3, 11, 0, tzinfo=et))

    def test_next_open_skips_holiday_weekend(self):
        """Next open should skip the holiday weekend."""
        from app import market
        from datetime import timezone
        et = market.ET
        next_open = market.next_open(datetime(2026, 4, 2, 17, 0, tzinfo=et))
        assert next_open == datetime(2026, 4, 6, 9, 30, tzinfo=et)