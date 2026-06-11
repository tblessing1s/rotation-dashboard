"""
US equity market calendar for staleness decisions.

Boring on purpose: weekday logic plus a hardcoded NYSE full-closure holiday
list (2024-2028). Half-days still produce a daily bar, so they count as
trading days. If the list runs out, weekends still work and the worst case is
a holiday being counted as a missed trading day (data shows one day staler
than it is — fails safe, never fresher).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# NYSE full closures. Source: nyse.com holiday calendar.
NYSE_HOLIDAYS = {
    # 2024
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    # 2025
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    # 2028
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19",
    "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25",
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def previous_trading_day(d: date) -> date:
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_completed_trading_day(now: datetime | None = None) -> date:
    """Most recent trading day whose session has closed (4pm ET ~= 21:00 UTC).

    Uses a fixed 21:00 UTC close so we don't need a tz database; during winter
    (EST) the real close is 21:00 UTC, during summer (EDT) it's 20:00 UTC, so
    this errs on the late side — data is never reported staler than it is by
    more than the DST hour, and never reported fresher than reality.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if is_trading_day(today) and now.hour >= 21:
        return today
    d = today - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_behind(as_of: date, now: datetime | None = None) -> int:
    """How many completed trading sessions a value dated `as_of` is missing.

    0 = current (covers the last completed session or newer — Saturday data is
    not stale on Sunday), 1 = one session behind, etc.
    """
    last = last_completed_trading_day(now)
    if as_of >= last:
        return 0
    behind = 0
    d = last
    while d > as_of:
        behind += 1
        d = previous_trading_day(d)
        if behind > 30:
            break
    return behind


def staleness(as_of: str | date | None, now: datetime | None = None) -> str:
    """Map an as_of date to 'fresh' | 'yellow' | 'red' | 'unknown'."""
    if not as_of:
        return "unknown"
    if isinstance(as_of, str):
        try:
            as_of = date.fromisoformat(as_of[:10])
        except ValueError:
            return "unknown"
    behind = trading_days_behind(as_of, now)
    if behind <= 0:
        return "fresh"
    if behind == 1:
        return "yellow"
    return "red"
