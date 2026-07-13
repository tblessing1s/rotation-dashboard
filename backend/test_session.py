"""Tests for the intraday session model (fully offline, injected clock)."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import session

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


# ---- early-close (half-day) table --------------------------------------------

def test_early_close_days_2025():
    days = session.early_close_days(2025)
    assert date(2025, 7, 3) in days       # July 4 is a Friday -> July 3 half-day
    assert date(2025, 11, 28) in days     # day after Thanksgiving
    assert date(2025, 12, 24) in days     # Christmas Eve (Wed)


def test_early_close_days_2026_july3_is_full_holiday_not_half():
    days = session.early_close_days(2026)
    # July 4 2026 is a Saturday -> July 3 is the *observed* full closure, so it is
    # NOT an early close (and must not contradict market_calendar.holidays).
    assert date(2026, 7, 3) not in days
    assert date(2026, 11, 27) in days     # day after Thanksgiving
    assert date(2026, 12, 24) in days     # Christmas Eve (Thu)


def test_is_early_close_boolean():
    assert session.is_early_close(date(2026, 11, 27)) is True
    assert session.is_early_close(date(2026, 7, 13)) is False  # ordinary Monday


# ---- session bounds & openness -----------------------------------------------

def test_regular_session_bounds_and_openness():
    s = session.session_state(_et(2026, 7, 13, 9, 45))   # ordinary Monday
    assert s.is_trading_day and s.is_open and not s.is_early_close
    assert s.open_at == _et(2026, 7, 13, 9, 30)
    assert s.close_at == _et(2026, 7, 13, 16, 0)
    assert s.minutes_since_open == 15.0
    assert s.minutes_until_close == 375.0


def test_pre_open_is_not_open_but_is_trading_day():
    s = session.session_state(_et(2026, 7, 13, 8, 0))
    assert s.is_trading_day and not s.is_open
    assert s.minutes_since_open == -90.0   # before the open


def test_after_close_is_not_open():
    s = session.session_state(_et(2026, 7, 13, 16, 30))
    assert s.is_trading_day and not s.is_open
    assert s.minutes_until_close == -30.0


def test_weekend_and_holiday_are_not_trading_days():
    sat = session.session_state(_et(2026, 7, 11, 12, 0))
    assert not sat.is_trading_day and not sat.is_open
    assert sat.open_at is None and sat.close_at is None
    # New Year's Day 2026 (Thursday) is a full holiday.
    hol = session.session_state(_et(2026, 1, 1, 11, 0))
    assert not hol.is_trading_day and not hol.is_open


def test_early_close_session_closes_at_1pm():
    # 12:50 ET on the day after Thanksgiving -> 10 minutes to the 13:00 close.
    s = session.session_state(_et(2026, 11, 27, 12, 50))
    assert s.is_open and s.is_early_close
    assert s.close_at == _et(2026, 11, 27, 13, 0)
    assert s.minutes_until_close == 10.0
    # After 13:00 the half-day session is closed even though a regular day wouldn't be.
    after = session.session_state(_et(2026, 11, 27, 13, 30))
    assert not after.is_open


# ---- clock normalization -----------------------------------------------------

def test_utc_input_equivalent_to_et():
    # 13:45 UTC == 09:45 EDT on 2026-07-13.
    utc = session.session_state(datetime(2026, 7, 13, 13, 45, tzinfo=timezone.utc))
    et = session.session_state(_et(2026, 7, 13, 9, 45))
    assert utc.minutes_since_open == et.minutes_since_open == 15.0


def test_naive_input_treated_as_utc():
    naive = session.session_state(datetime(2026, 7, 13, 13, 45))
    assert naive.minutes_since_open == 15.0


# ---- next session open -------------------------------------------------------

def test_next_session_open_from_mid_session_is_tomorrow():
    # During Monday's session, the *next* open is Tuesday.
    nxt = session.next_session_open(_et(2026, 7, 13, 10, 0))
    assert nxt == _et(2026, 7, 14, 9, 30)


def test_next_session_open_pre_open_is_today():
    nxt = session.next_session_open(_et(2026, 7, 13, 8, 0))
    assert nxt == _et(2026, 7, 13, 9, 30)


def test_next_session_open_skips_weekend_and_holiday():
    # After Friday's close, next open is Monday.
    nxt = session.next_session_open(_et(2026, 7, 10, 16, 30))
    assert nxt == _et(2026, 7, 13, 9, 30)
    # Day before Independence Day observed (Thu 2026-07-02 after close) -> the
    # observed holiday Fri 07-03 is skipped, next open is Monday 07-06.
    nxt2 = session.next_session_open(_et(2026, 7, 2, 17, 0))
    assert nxt2 == _et(2026, 7, 6, 9, 30)


# ---- DST correctness ---------------------------------------------------------

def test_dst_open_offset_is_edt_in_summer_est_in_winter():
    summer = session.session_state(_et(2026, 7, 13, 9, 30))
    winter = session.session_state(_et(2026, 1, 5, 9, 30))
    assert summer.open_at.utcoffset().total_seconds() == -4 * 3600   # EDT
    assert winter.open_at.utcoffset().total_seconds() == -5 * 3600   # EST


def test_dst_spring_forward_week_open_is_correct():
    # 2026 spring-forward is Sun 2026-03-08. The Monday after opens 09:30 EDT,
    # and 09:30 ET == 13:30 UTC that week.
    s = session.session_state(_et(2026, 3, 9, 9, 30))
    assert s.is_open
    assert s.open_at.astimezone(timezone.utc) == datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)
    # The Friday before (still EST) opens 09:30 == 14:30 UTC.
    pre = session.session_state(_et(2026, 3, 6, 9, 30))
    assert pre.open_at.astimezone(timezone.utc) == datetime(2026, 3, 6, 14, 30, tzinfo=timezone.utc)
