"""Staleness must be measured in trading days: weekend gaps and NYSE holidays
are not staleness, and a confident 'fresh' must never be shown on old data."""
from datetime import date, datetime, timezone

import market_calendar as mcal


def utc(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


# 2026-06-12 is a Friday; 2026-06-15 the following Monday.
def test_friday_data_is_fresh_all_weekend():
    assert mcal.staleness("2026-06-12", now=utc(2026, 6, 13)) == "fresh"   # Sat
    assert mcal.staleness("2026-06-12", now=utc(2026, 6, 14)) == "fresh"   # Sun


def test_friday_data_still_fresh_monday_before_close():
    assert mcal.staleness("2026-06-12", now=utc(2026, 6, 15, 14)) == "fresh"


def test_friday_data_goes_yellow_after_monday_close():
    assert mcal.staleness("2026-06-12", now=utc(2026, 6, 15, 22)) == "yellow"


def test_two_sessions_behind_is_red():
    assert mcal.staleness("2026-06-11", now=utc(2026, 6, 15, 22)) == "red"
    assert mcal.staleness("2026-06-01", now=utc(2026, 6, 15)) == "red"


def test_holiday_is_not_counted_as_staleness():
    # 2026-07-03 (Friday) is the July 4th closure. Thursday 7/2 data must be
    # fresh through the long weekend and Monday 7/6 before the close.
    assert mcal.staleness("2026-07-02", now=utc(2026, 7, 4)) == "fresh"
    assert mcal.staleness("2026-07-02", now=utc(2026, 7, 6, 14)) == "fresh"
    assert mcal.staleness("2026-07-02", now=utc(2026, 7, 6, 22)) == "yellow"


def test_last_completed_trading_day_intraday_is_previous_session():
    # Mid-session Tuesday: Monday is the last completed session.
    assert mcal.last_completed_trading_day(utc(2026, 6, 16, 15)) == date(2026, 6, 15)
    # After the close it is the same day.
    assert mcal.last_completed_trading_day(utc(2026, 6, 16, 22)) == date(2026, 6, 16)


def test_unknown_for_missing_or_garbage_dates():
    assert mcal.staleness(None) == "unknown"
    assert mcal.staleness("not-a-date") == "unknown"
