"""US equity market (NYSE/Nasdaq) trading-calendar helpers.

CFM sells a *weekly* short call and wants that short to land on the week's
standard expiration — a Friday — rather than on one of the daily expirations
that names like IWM/SPY now list every trading day. When a Friday is an
exchange holiday the week's option series expires the day before (Thursday),
so `option_chain` needs to know which Fridays the market is closed.

No third-party calendar library is a dependency, so the standard NYSE holiday
set is computed here (dates are exact and observance rules are the NYSE ones).
Only *full-day* closures matter for expiration; early-close half-days (e.g. the
day after Thanksgiving) still settle options normally and are not modelled.
"""
from __future__ import annotations

import calendar
import threading
from datetime import date, timedelta

_cache: dict[int, frozenset[date]] = {}
_guard = threading.Lock()


def _easter(year: int) -> date:
    """Easter Sunday (Gregorian, Anonymous computus) — Good Friday is two days
    before and is the one recurring market holiday that always falls on a Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th (1-based) `weekday` of a month, e.g. 3rd Monday (MLK/Presidents)."""
    days = [w[weekday] for w in calendar.monthcalendar(year, month) if w[weekday]]
    return date(year, month, days[n - 1])


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last `weekday` of a month, e.g. last Monday of May (Memorial Day)."""
    days = [w[weekday] for w in calendar.monthcalendar(year, month) if w[weekday]]
    return date(year, month, days[-1])


def _observed(d: date) -> date:
    """NYSE weekend-observance for a fixed-date holiday: Saturday -> the Friday
    before, Sunday -> the Monday after. (New Year's Day is handled separately —
    it is NOT pulled back to Dec 31 when Jan 1 lands on a Saturday.)"""
    if d.weekday() == calendar.SATURDAY:
        return d - timedelta(days=1)
    if d.weekday() == calendar.SUNDAY:
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> frozenset[date]:
    """The set of full-day NYSE holidays observed in `year`."""
    cached = _cache.get(year)
    if cached is not None:
        return cached
    with _guard:
        cached = _cache.get(year)
        if cached is not None:
            return cached
        hols: set[date] = set()

        # New Year's Day — observed on Monday when Jan 1 is a Sunday, but NOT
        # pulled back to the prior Dec 31 when Jan 1 falls on a Saturday.
        ny = date(year, 1, 1)
        if ny.weekday() == calendar.SUNDAY:
            hols.add(date(year, 1, 2))
        elif ny.weekday() != calendar.SATURDAY:
            hols.add(ny)

        hols.add(_nth_weekday(year, 1, calendar.MONDAY, 3))      # MLK Jr. Day
        hols.add(_nth_weekday(year, 2, calendar.MONDAY, 3))      # Presidents' Day
        hols.add(_easter(year) - timedelta(days=2))              # Good Friday
        hols.add(_last_weekday(year, 5, calendar.MONDAY))        # Memorial Day
        if year >= 2022:
            hols.add(_observed(date(year, 6, 19)))               # Juneteenth
        hols.add(_observed(date(year, 7, 4)))                    # Independence Day
        hols.add(_nth_weekday(year, 9, calendar.MONDAY, 1))      # Labor Day
        hols.add(_nth_weekday(year, 11, calendar.THURSDAY, 4))   # Thanksgiving
        hols.add(_observed(date(year, 12, 25)))                  # Christmas

        frozen = frozenset(hols)
        _cache[year] = frozen
        return frozen


def is_market_holiday(d: date) -> bool:
    """True if the market is fully closed on `d` (an exchange holiday)."""
    return d in holidays(d.year)


def is_trading_day(d: date) -> bool:
    """True if `d` is a weekday the market is open (not a weekend or holiday)."""
    return d.weekday() < calendar.SATURDAY and not is_market_holiday(d)
