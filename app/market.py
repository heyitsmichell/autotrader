"""US market clock: regular session, NYSE holidays and early closes.

Holidays follow NYSE rules: a holiday on Saturday is observed on the Friday
before, one on Sunday on the Monday after, except New Year's Day on a Saturday,
which is not made up. Early closes (1:00 pm ET) are the day before Independence
Day, the day after Thanksgiving and Christmas Eve, when those fall on a normal
trading day.

Unscheduled closures (national days of mourning, weather) can't be predicted:
add them to MARKET_CLOSED_DATES in .env (comma-separated YYYY-MM-DD), and extra
early closes to MARKET_EARLY_CLOSE_DATES. The broker rejects orders on any day
this calendar gets wrong, so a mistake here costs a skipped run, not a bad trade.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PREMARKET_OPEN = time(4, 0)      # 4:00 AM ET
AFTERHOURS_CLOSE = time(20, 0)   # 8:00 PM ET: end of after-hours, start of Webull overnight
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)

# Test time override - set to a datetime to freeze time for testing
_test_time: datetime | None = None


def set_test_time(dt: datetime | None) -> None:
    """Set a fixed time for testing. Pass None to use real time."""
    global _test_time
    _test_time = dt


def _now_et() -> datetime:
    """Internal: get current ET time, or test time if set.

    ``TRADER_TEST_TIME`` is also honored so a mock broker in a child process
    (which cannot see set_test_time) follows the same frozen clock as pytest.
    """
    if _test_time:
        return _test_time.astimezone(ET)
    raw = os.environ.get("TRADER_TEST_TIME")
    if raw:
        try:
            return datetime.fromisoformat(raw).astimezone(ET)
        except ValueError:
            pass
    return datetime.now(ET)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_et() -> datetime:
    return _now_et()


# -- calendar --------------------------------------------------------------------
def _easter(year: int) -> date:
    """Western (Gregorian) Easter Sunday, anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = (h + el - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _env_dates(name: str) -> set[date]:
    out = set()
    for part in os.environ.get(name, "").split(","):
        try:
            out.add(date.fromisoformat(part.strip()))
        except ValueError:
            pass
    return out


@lru_cache(maxsize=16)
def holidays(year: int) -> dict[date, str]:
    """Full-day NYSE closures for one year, plus any MARKET_CLOSED_DATES."""
    h: dict[date, str] = {}
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        h[date(year, 1, 2)] = "New Year's Day (observed)"
    elif new_year.weekday() < 5:
        h[new_year] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[_easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    h[_observed(date(year, 12, 25))] = "Christmas Day"
    for d in _env_dates("MARKET_CLOSED_DATES"):
        if d.year == year:
            h[d] = "Closed (MARKET_CLOSED_DATES)"
    return h


@lru_cache(maxsize=16)
def early_closes(year: int) -> set[date]:
    out = set()
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 4:                      # Mon-Thu; on a Friday it is the holiday
        out.add(jul3)
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 4:
        out.add(dec24)
    out |= {d for d in _env_dates("MARKET_EARLY_CLOSE_DATES") if d.year == year}
    return out - set(holidays(year))


def holiday_name(d: date) -> str | None:
    return holidays(d.year).get(d)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def session_close(d: date) -> time | None:
    """Close time of the regular session on date d (ET), or None if closed."""
    if not is_trading_day(d):
        return None
    return EARLY_CLOSE if d in early_closes(d.year) else CLOSE


def market_is_open(at: datetime | None = None) -> bool:
    """Regular session (9:30 to 16:00 ET, or to 13:00 on early-close days)."""
    t = (at or _now_et()).astimezone(ET)
    close = session_close(t.date())
    return close is not None and OPEN <= t.time() < close


def market_is_open_extended(at: datetime | None = None) -> bool:
    """Extended-hours session: premarket + regular + after-hours (4:00-20:00 ET)."""
    t = (at or _now_et()).astimezone(ET)
    close = session_close(t.date())
    if close is None:
        return False
    return PREMARKET_OPEN <= t.time() < AFTERHOURS_CLOSE


def is_premarket(at: datetime | None = None) -> bool:
    """Check if we're in premarket hours (4:00-9:30 ET)."""
    t = (at or _now_et()).astimezone(ET)
    close = session_close(t.date())
    if close is None:
        return False
    return PREMARKET_OPEN <= t.time() < OPEN


def is_afterhours(at: datetime | None = None) -> bool:
    """After-hours (regular close to 20:00 ET) on a trading day."""
    t = (at or _now_et()).astimezone(ET)
    close = session_close(t.date())
    return close is not None and close <= t.time() < AFTERHOURS_CLOSE


def is_overnight(at: datetime | None = None) -> bool:
    """Webull overnight session: 20:00 to 04:00 ET, Sunday evening through Friday
    morning (the evening of a trading day, or the early morning after one)."""
    t = (at or _now_et()).astimezone(ET)
    d = t.date()
    if t.time() >= AFTERHOURS_CLOSE:
        # evening: the session belongs to the next trading day
        return is_trading_day(d + timedelta(days=1)) or (
            d.weekday() == 6 and is_trading_day(d + timedelta(days=1)))
    if t.time() < PREMARKET_OPEN:
        return is_trading_day(d)
    return False


def order_session(at: datetime | None = None) -> str | None:
    """The Webull ``trading_session`` an order may be sent with right now.

    CORE  - regular session
    ALL   - premarket (4:00-9:30) and after-hours (close-20:00), ET
    NIGHT - overnight (20:00-04:00 ET) only
    None  - nothing is open (weekend / holiday): do not send an order
    """
    t = (at or _now_et()).astimezone(ET)
    if market_is_open(t):
        return "CORE"
    if is_premarket(t) or is_afterhours(t):
        return "ALL"
    if is_overnight(t):
        return "NIGHT"
    return None


def session_phase(at: datetime | None = None) -> str:
    """Human-readable phase: premarket | regular | afterhours | overnight | closed."""
    t = (at or _now_et()).astimezone(ET)
    if market_is_open(t):
        return "regular"
    if is_premarket(t):
        return "premarket"
    if is_afterhours(t):
        return "afterhours"
    if is_overnight(t):
        return "overnight"
    return "closed"


def next_open(at: datetime | None = None) -> datetime:
    """Start of the next regular session at or after `at` (ET). Returns `at`
    itself when the market is open."""
    t = (at or _now_et()).astimezone(ET)
    d = t.date()
    close = session_close(d)
    if close is not None and t.time() < close:
        return t if t.time() >= OPEN else datetime.combine(d, OPEN, ET)
    for _ in range(15):
        d += timedelta(days=1)
        if is_trading_day(d):
            break
    return datetime.combine(d, OPEN, ET)


def session_info(at: datetime | None = None) -> dict:
    """Today's session, for the dashboard."""
    t = (at or _now_et()).astimezone(ET)
    d = t.date()
    close = session_close(d)
    return {
        "date": d.isoformat(),
        "trading_day": close is not None,
        "holiday": holiday_name(d),
        "early_close": close == EARLY_CLOSE,
        "close": close.strftime("%H:%M") if close else None,
        "open_now": market_is_open(t),
        "premarket_now": is_premarket(t),
        "extended_open_now": market_is_open_extended(t),
        "afterhours_now": is_afterhours(t),
        "overnight_now": is_overnight(t),
        "phase": session_phase(t),
        "order_session": order_session(t),
        "next_open": next_open(t).isoformat(timespec="minutes"),
    }


def start_of_today_et_utc() -> datetime:
    t = _now_et()
    return t.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
