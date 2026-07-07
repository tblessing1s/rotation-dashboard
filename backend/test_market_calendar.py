"""Tests for the NYSE trading-calendar helpers (fully offline)."""
from datetime import date

import market_calendar as mc


def test_easter_and_good_friday_2026():
    # Easter 2026 is April 5; Good Friday is two days earlier.
    assert mc._easter(2026) == date(2026, 4, 5)
    assert (date(2026, 4, 3)) in mc.holidays(2026)   # Good Friday
    assert mc.is_market_holiday(date(2026, 4, 3)) is True


def test_fixed_and_floating_holidays_2026():
    h = mc.holidays(2026)
    assert date(2026, 1, 1) in h      # New Year's Day (Thu)
    assert date(2026, 1, 19) in h     # MLK — 3rd Monday
    assert date(2026, 2, 16) in h     # Presidents' — 3rd Monday
    assert date(2026, 5, 25) in h     # Memorial — last Monday
    assert date(2026, 9, 7) in h      # Labor Day — 1st Monday
    assert date(2026, 11, 26) in h    # Thanksgiving — 4th Thursday


def test_weekend_observance_pulls_saturday_holiday_to_friday():
    # July 4 2026 is a Saturday -> observed on Friday July 3.
    assert date(2026, 7, 3) in mc.holidays(2026)
    assert date(2026, 7, 4) not in mc.holidays(2026)
    # Christmas 2026 (Dec 25) is itself a Friday -> stays Friday.
    assert date(2026, 12, 25) in mc.holidays(2026)


def test_juneteenth_only_from_2022():
    assert date(2026, 6, 19) in mc.holidays(2026)   # Friday -> holiday
    assert date(2021, 6, 18) not in mc.holidays(2021)
    assert not any(h.month == 6 and h.day in (18, 19) for h in mc.holidays(2021))


def test_new_year_saturday_is_not_pulled_back_to_dec_31():
    # Jan 1 2022 was a Saturday; NYSE did NOT close Fri Dec 31 2021 for it.
    assert date(2021, 12, 31) not in mc.holidays(2021)
    # Jan 1 2023 was a Sunday -> observed Monday Jan 2.
    assert date(2023, 1, 2) in mc.holidays(2023)


def test_is_trading_day():
    assert mc.is_trading_day(date(2026, 7, 7)) is True    # ordinary Tuesday
    assert mc.is_trading_day(date(2026, 7, 4)) is False    # Saturday
    assert mc.is_trading_day(date(2026, 4, 3)) is False    # Good Friday
