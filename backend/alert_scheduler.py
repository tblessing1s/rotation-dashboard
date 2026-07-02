"""In-process alert scheduler.

Why in-process (vs a Fly scheduled machine or external cron): the persistent
volume attaches to exactly ONE machine and state.json is a single-writer store,
so a second scheduled machine could never share /data — a background thread in
the one app process is the only shape that preserves the single-writer
invariant without new infrastructure. It costs one daemon thread and requires
the machine to stay up (fly.toml pins min_machines_running = 1). As a belt-and-
braces path, POST /api/alerts/run triggers the same evaluator over HTTP — an
external cron can hit it (auto_start wakes a stopped machine) and dedup makes
overlapping or repeated runs harmless.

The schedule is a set of ET times on market days (Mon-Fri; exchange holidays
are not modelled — a holiday run just evaluates an unchanged state and fires
nothing new). A slot fires once per day: the tick loop wakes every ~30s and
runs any slot whose time has passed and hasn't run today, so a machine restart
mid-day catches up on the next tick instead of skipping the day (dedup keeps
the catch-up from re-notifying).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("cfm.alerts")

ET = ZoneInfo("America/New_York")
_TICK_SECONDS = 30

_started = False
_start_lock = threading.Lock()
_stop = threading.Event()
# slot "HH:MM" -> last date it ran; in-memory only (a restart may re-run a slot,
# which dedup makes a no-op).
_last_run: dict[str, date] = {}


def enabled() -> bool:
    """Scheduler on by default; CFM_ALERTS_SCHEDULER=0 turns it off (tests, CLI
    tools importing app, one-off scripts)."""
    return os.environ.get("CFM_ALERTS_SCHEDULER", "1").strip() not in ("0", "false", "no")


def due_slots(now: datetime, last_run: dict[str, date] | None = None) -> list[str]:
    """Schedule slots that should run at `now` (ET): time reached, market day,
    not yet run today. Pure so it's unit-testable without threads."""
    last_run = _last_run if last_run is None else last_run
    if now.weekday() >= 5:
        return []
    today = now.date()
    hhmm_now = now.strftime("%H:%M")
    return [slot for slot in config.ALERT_SCHEDULE_ET
            if slot <= hhmm_now and last_run.get(slot) != today]


def _tick() -> None:
    import alerts  # local import: keep module import side-effect free
    now = datetime.now(ET)
    due = due_slots(now)
    if not due:
        return
    # A restart mid-day makes several slots due at once; one evaluator pass
    # covers them all (the conditions are the same state either way).
    for slot in due:
        _last_run[slot] = now.date()
    try:
        result = alerts.run()
        logger.info("scheduled alert run (%s ET): %d fired, %d resolved, %d active",
                    "+".join(due), len(result["fired"]), len(result["resolved"]),
                    result["active_count"])
    except Exception as e:  # noqa: BLE001 — a failed run must not kill the thread
        logger.error("scheduled alert run (%s ET) failed: %s", "+".join(due), e)


def _loop() -> None:
    while not _stop.wait(_TICK_SECONDS):
        _tick()


def start_once() -> bool:
    """Start the scheduler thread exactly once per process. Returns True if it
    started now (False when disabled or already running)."""
    global _started
    with _start_lock:
        if _started or not enabled():
            return False
        threading.Thread(target=_loop, name="alert-scheduler", daemon=True).start()
        _started = True
        logger.info("alert scheduler started (ET slots: %s)", ", ".join(config.ALERT_SCHEDULE_ET))
        return True
