"""In-process day-trade scheduler.

Same rationale and shape as ``alert_scheduler.py`` (see its module docstring):
the persistent volume attaches to one machine, so a background thread in the
one app process — not a second scheduled machine — is the only shape that
needs no new infrastructure. A second daemon thread (not a hook into
``alert_scheduler``'s own loop) so a bug in one sleeve's tick can never stall
the other's, and so this sleeve can be disabled independently
(``CFM_DAYTRADE_SCHEDULER=0``) without touching CFM's alerts.

Two jobs, both best-effort (logged, never fatal to the tick):

  screen        once per trading day, after the close (``DAYTRADE_SCREEN_ET``)
                — ``daytrade.universe.screen()``
  bar ingest    every ``DAYTRADE_BAR_INTERVAL_MINUTES`` during the Rule 2
                signal window (``DAYTRADE_WINDOW_START_ET``-``_END_ET``) —
                ``daytrade.bars.ingest()``

The window is expressed in ET, not CT: alert_scheduler's clock is already ET,
and 8:30-10:00 AM CT == 9:30-11:00 AM ET year-round (both zones observe the
same US DST transitions on the same date), so no second timezone is needed in
the process.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
import market_calendar

logger = logging.getLogger("cfm.daytrade")

ET = ZoneInfo("America/New_York")
_TICK_SECONDS = 30

_started = False
_start_lock = threading.Lock()
_stop = threading.Event()

# Trading day the nightly screener last ran for — in-memory only, a restart
# just re-runs it on the next tick (screen() overwrites the same day's file).
_last_screen_day: date | None = None
# Last time the bar-ingest job ran, any day — cadence-gated within the window.
_last_bar_fetch: datetime | None = None
# Trading day the signal engine's post-window finalize last ran for — see
# _maybe_finalize_signals. The engine also runs after every bar ingest
# (in-window), so this only covers the once-per-day cutoff sweep.
_last_signals_finalize_day: date | None = None


def enabled() -> bool:
    """Scheduler on by default; CFM_DAYTRADE_SCHEDULER=0 turns it off (tests,
    CLI tools importing app, one-off scripts)."""
    return os.environ.get("CFM_DAYTRADE_SCHEDULER", "1").strip() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Pure predicates — unit-testable without threads/clock.
# ---------------------------------------------------------------------------
def screen_due(now: datetime, last_day: date | None) -> bool:
    """Nightly screener: fires once per trading day, at/after DAYTRADE_SCREEN_ET."""
    return now.strftime("%H:%M") >= config.DAYTRADE_SCREEN_ET and last_day != now.date()


def in_window(now: datetime) -> bool:
    """True during the Rule 2 signal window (Mon-Fri only)."""
    if now.weekday() >= 5:
        return False
    return config.DAYTRADE_WINDOW_START_ET <= now.strftime("%H:%M") <= config.DAYTRADE_WINDOW_END_ET


def bar_fetch_due(now: datetime, last_fetch: datetime | None) -> bool:
    """Bar ingest: fires on DAYTRADE_BAR_INTERVAL_MINUTES cadence, only inside
    the signal window."""
    if not in_window(now):
        return False
    if last_fetch is None:
        return True
    return (now - last_fetch).total_seconds() >= config.DAYTRADE_BAR_INTERVAL_MINUTES * 60


def signals_finalize_due(now: datetime, last_day: date | None) -> bool:
    """Signal-engine cutoff sweep: fires once per trading day, at/after the
    signal window ends — forces any still-open trade to its rule-6 time-
    cutoff exit. Same shape as screen_due; the engine also runs (without
    finalizing) after every in-window bar ingest, see _maybe_bar_ingest."""
    return now.strftime("%H:%M") >= config.DAYTRADE_WINDOW_END_ET and last_day != now.date()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _run_screen() -> None:
    try:
        from daytrade import universe
        result = universe.screen()
        logger.info("daytrade screener: %d pick(s) from %d screened",
                     len(result["picks"]), len(result["screened"]))
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal to the tick
        logger.warning("daytrade screener failed: %s", e)


def _maybe_screen(now: datetime) -> None:
    global _last_screen_day
    if not market_calendar.is_trading_day(now.date()):
        return
    if not screen_due(now, _last_screen_day):
        return
    _last_screen_day = now.date()
    _run_screen()


def _run_bar_ingest(now: datetime) -> None:
    try:
        from daytrade import bars
        result = bars.ingest(now)
        logger.info("daytrade bar ingest: %d bar(s) written across %d symbol(s)",
                     result["written"], len(result["symbols"]))
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal to the tick
        logger.warning("daytrade bar ingest failed: %s", e)


def _run_signals(now: datetime) -> None:
    try:
        from daytrade import signals
        day = now.strftime("%Y-%m-%d")
        result = signals.run_day(day, now=now)
        logger.info("daytrade signals: %d total event(s) journaled for %s",
                     len(result["events"]), day)
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal to the tick
        logger.warning("daytrade signal engine failed: %s", e)


def _maybe_bar_ingest(now: datetime) -> None:
    global _last_bar_fetch
    if not market_calendar.is_trading_day(now.date()):
        return
    if not bar_fetch_due(now, _last_bar_fetch):
        return
    _last_bar_fetch = now
    _run_bar_ingest(now)
    # Evaluate the signal engine against the bars just ingested — rules 3-6
    # react to each new candle as it lands, not just once at day's end.
    _run_signals(now)


def _maybe_finalize_signals(now: datetime) -> None:
    """Once per trading day, at/after the window ends: force any still-open
    trade to its rule-6 time-cutoff exit (see signals.run_day/_finalize_open_
    trades). Separate from _maybe_bar_ingest's in-window run because bar
    ingestion — and with it the in-window signals run — stops once the
    window closes, but the cutoff itself still needs one more pass."""
    global _last_signals_finalize_day
    if not market_calendar.is_trading_day(now.date()):
        return
    if not signals_finalize_due(now, _last_signals_finalize_day):
        return
    _last_signals_finalize_day = now.date()
    _run_signals(now)


def _tick() -> None:
    now = datetime.now(ET)
    _maybe_screen(now)
    _maybe_bar_ingest(now)
    _maybe_finalize_signals(now)


def _loop() -> None:
    while not _stop.wait(_TICK_SECONDS):
        try:
            _tick()
        except Exception as e:  # noqa: BLE001 — one bad tick must not kill the daemon
            logger.error("daytrade scheduler tick failed: %s", e)


def start_once() -> bool:
    """Idempotent: a no-op if already started or disabled. Call from app.py
    at import time, same as alert_scheduler.start_once()."""
    global _started
    with _start_lock:
        if _started or not enabled():
            return False
        threading.Thread(target=_loop, name="daytrade-scheduler", daemon=True).start()
        _started = True
        logger.info("daytrade scheduler started")
        return True
