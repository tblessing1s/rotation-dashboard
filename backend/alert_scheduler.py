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
# Nightly maintenance (earnings/dividends cache refresh) — last date it ran.
_last_maintenance: date | None = None
# Pre-market position reconciliation (state.json vs Schwab) — last date it ran.
# The morning run is the important one: assignments materialize overnight and
# pre-market is when the operator can act calmly.
_last_reconcile: date | None = None


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


def maintenance_due(now: datetime, last: date | None) -> bool:
    """Nightly maintenance runs once per calendar day after MAINTENANCE_ET
    (weekends included — providers publish calendar updates any day)."""
    return now.strftime("%H:%M") >= config.MAINTENANCE_ET and last != now.date()


def _tick() -> None:
    import alerts  # local import: keep module import side-effect free
    import heartbeat
    global _last_maintenance
    now = datetime.now(ET)

    # Dead-man's switch: prove the scheduler thread is alive on EVERY tick,
    # including weekends/holidays when no alert slot fires — a missed run of
    # pings (thread wedged or machine stopped) is what pages the operator.
    heartbeat.ping()

    if maintenance_due(now, _last_maintenance):
        _last_maintenance = now.date()
        try:
            import maintenance
            maintenance.nightly_refresh()
        except Exception as e:  # noqa: BLE001 — a failed refresh must not kill the thread
            logger.error("nightly maintenance failed: %s", e)

    due = due_slots(now)
    if not due:
        return
    # Pre-market reconciliation runs on the FIRST morning slot, before the alert
    # pass, so reconcile_dirty / short_stock_detected fire off a fresh report.
    _maybe_morning_reconcile(now, due)
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
        # The thread is alive but the evaluation itself broke — page immediately
        # (a persistently failing run is as dangerous as a dead thread).
        import heartbeat
        heartbeat.ping("/fail", force=True)
        logger.error("scheduled alert run (%s ET) failed: %s", "+".join(due), e)


def _maybe_morning_reconcile(now: datetime, due: list[str]) -> None:
    """Run position reconciliation once per day on the first morning slot, but
    only when Schwab is connected (read-only connected mode is enough —
    CFM_LIVE_TRADING is not required) or in demo mode (report-only). A failure is
    logged and recorded (feeding reconcile_stale), never fatal to the tick."""
    global _last_reconcile
    slots = config.ALERT_SCHEDULE_ET
    first_slot = slots[0] if slots else None
    if not first_slot or first_slot not in due or _last_reconcile == now.date():
        return
    import schwab_api
    if not (schwab_api.configured() or config.demo_enabled()):
        return
    _last_reconcile = now.date()
    try:
        import reconcile
        report = reconcile.run_reconciliation()
        logger.info("pre-market reconciliation: status=%s diffs=%d",
                    report.get("status"), len(report.get("diffs", [])))
    except Exception as e:  # noqa: BLE001 — a failed reconcile must not kill the thread
        logger.error("pre-market reconciliation failed: %s", e)


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
