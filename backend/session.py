"""Intraday US-equity **session model** — the time authority for the execution gate.

``market_calendar`` answers "is this *date* a trading day" (full-day holidays only,
and it deliberately does not model half-days — see ``market_calendar.py:11``). The
time-of-day execution gate needs finer questions about a specific *timestamp*:

  * is the market open right now;
  * minutes since the open / minutes until the close;
  * is today an **early-close** (half-day) session — because the close blackout must
    key off the *actual* close (13:00 ET on a half-day, not 16:00);
  * when does the *next* session open (so a blocked signal can carry an
    ``executable_at`` into the next session).

Everything here is **pure and deterministic given an injected ``now``** — no wall
clock is read — so the gate and its tests share one mockable clock. All wall-time
reasoning is done in Eastern time and is DST-correct via ``zoneinfo`` (constructing
``datetime(..., tzinfo=ZoneInfo("America/New_York"))`` resolves the correct offset
for each date; the open/close wall times never fall in the spring-forward gap, so
there is no ambiguity to resolve).

This module is the execution-gate's session authority. ``market_scheduler``'s own
``is_market_open`` stays as-is for the data-polling scheduler (it operates on an
ET-local clock and a coarser open/close notion); a future unification is possible
but deliberately out of scope here to keep the blast radius small.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import market_calendar

ET = ZoneInfo("America/New_York")

# Regular-session wall times (ET). Early-close sessions swap the 16:00 close for
# 13:00; the open is unchanged. These are the NYSE/Nasdaq regular hours.
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# How far ahead next_session_open() will scan before giving up (a safety bound;
# the market is never closed for anywhere near this many consecutive days).
_MAX_SCAN_DAYS = 10


def _to_et(now: datetime) -> datetime:
    """Normalize an injected ``now`` to an Eastern-time aware datetime. A naive
    timestamp is treated as UTC (the codebase's ``log.utcnow`` convention), never as
    machine-local time, so behaviour is identical regardless of where it runs."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ET)


def early_close_days(year: int) -> frozenset[date]:
    """The NYSE early-close (1:00 pm ET) half-days observed in ``year``.

    Three recurring half-days: the day after Thanksgiving, July 3, and December 24.
    A candidate is dropped when it is a weekend or is itself a *full* holiday (e.g.
    when July 4 falls on a Saturday, July 3 is the observed full closure, not an
    early close) — so this never contradicts ``market_calendar.holidays``."""
    hols = market_calendar.holidays(year)
    candidates: set[date] = set()

    # Day after Thanksgiving (Friday) — always a half-day when the market is open.
    thanksgiving = market_calendar._nth_weekday(year, 11, calendar.THURSDAY, 4)
    candidates.add(thanksgiving + timedelta(days=1))

    # July 3 and December 24 — half-days only when they are trading weekdays.
    candidates.add(date(year, 7, 3))
    candidates.add(date(year, 12, 24))

    return frozenset(
        d for d in candidates
        if d.weekday() < calendar.SATURDAY and d not in hols
    )


def is_early_close(d: date) -> bool:
    """True if ``d`` is a half-day (early-close) trading session."""
    return d in early_close_days(d.year)


def session_bounds(d: date) -> tuple[datetime, datetime] | None:
    """(open, close) as ET-aware datetimes for trading day ``d``, honouring an
    early close; ``None`` if ``d`` is not a trading day (weekend/holiday)."""
    if not market_calendar.is_trading_day(d):
        return None
    close = EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE
    open_at = datetime.combine(d, REGULAR_OPEN, tzinfo=ET)
    close_at = datetime.combine(d, close, tzinfo=ET)
    return open_at, close_at


def next_session_open(now: datetime) -> datetime:
    """The open of the next session strictly *after* ``now`` (ET-aware).

    If ``now`` is before today's open on a trading day, that is today's open; if
    ``now`` is at/after today's open (mid-session, or after the close), it is the
    next trading day's open. Used to stamp ``executable_at`` on signals blocked
    because the market is closed or is in the close blackout."""
    now_et = _to_et(now)
    d = now_et.date()
    for _ in range(_MAX_SCAN_DAYS + 1):
        bounds = session_bounds(d)
        if bounds is not None and bounds[0] > now_et:
            return bounds[0]
        d += timedelta(days=1)
    # Unreachable in practice; keep the contract total rather than raise.
    return datetime.combine(d, REGULAR_OPEN, tzinfo=ET)


@dataclass(frozen=True)
class SessionState:
    """A snapshot of the market session at one injected instant. Pure data — the
    gate consumes this and never re-reads a clock.

    ``minutes_since_open`` / ``minutes_until_close`` are populated on any trading
    day (they may be negative before the open or past the regular length after the
    close); consumers guard on ``is_open`` first. ``open_at`` / ``close_at`` are the
    current date's session bounds (``None`` on a non-trading day). ``next_open_at``
    is always the next future open, so a blocked signal can defer into it."""
    now_et: datetime
    is_trading_day: bool
    is_open: bool
    is_early_close: bool
    open_at: datetime | None
    close_at: datetime | None
    next_open_at: datetime
    minutes_since_open: float | None
    minutes_until_close: float | None


def session_state(now: datetime) -> SessionState:
    """Build the :class:`SessionState` for injected ``now`` (UTC-aware or ET-aware;
    naive is treated as UTC). Deterministic — no wall-clock read."""
    now_et = _to_et(now)
    d = now_et.date()
    bounds = session_bounds(d)
    if bounds is not None:
        open_at, close_at = bounds
        is_open = open_at <= now_et < close_at
        mins_since = (now_et - open_at).total_seconds() / 60.0
        mins_until = (close_at - now_et).total_seconds() / 60.0
        early = is_early_close(d)
    else:
        open_at = close_at = None
        is_open = False
        mins_since = mins_until = None
        early = False
    return SessionState(
        now_et=now_et,
        is_trading_day=bounds is not None,
        is_open=is_open,
        is_early_close=early,
        open_at=open_at,
        close_at=close_at,
        next_open_at=next_session_open(now_et),
        minutes_since_open=mins_since,
        minutes_until_close=mins_until,
    )
