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
# Intraday reconcile + transaction-ingestion cadence (spec §4/§5) — last datetime
# it ran. Rate-limited to RECONCILE_INTERVAL_MINUTES during market hours so the
# minutes-based staleness clock has a cadence to be measured against.
_last_interval_reconcile: datetime | None = None


def enabled() -> bool:
    """Scheduler on by default; CFM_ALERTS_SCHEDULER=0 turns it off (tests, CLI
    tools importing app, one-off scripts)."""
    return os.environ.get("CFM_ALERTS_SCHEDULER", "1").strip() not in ("0", "false", "no")


def warm_scan_enabled() -> bool:
    """Pre-open scan warm-up on by default; CFM_WARM_SCAN=0 turns it off."""
    return os.environ.get("CFM_WARM_SCAN", "1").strip() not in ("0", "false", "no")


def recommendations_enabled() -> bool:
    """Scheduled recommendation passes on by default; CFM_RECOMMENDATIONS=0
    turns them off (tests, one-off scripts). Manual runs via the API still work."""
    return os.environ.get("CFM_RECOMMENDATIONS", "1").strip() not in ("0", "false", "no")


def _warm_scan() -> None:
    """Prime the full-universe scan cache so the first Scan of the day loads warm.
    Best-effort: logged, never fatal to the tick or the process."""
    if not warm_scan_enabled():
        return
    try:
        import screening
        result = screening.warm_scan_cache()
        if result.get("ok"):
            logger.info("scan cache warmed")
        else:
            logger.warning("scan cache warm-up incomplete: %s", result.get("error"))
    except Exception as e:  # noqa: BLE001 — a warm-up must never break its caller
        logger.warning("scan cache warm-up failed: %s", e)


def _market_hours(now: datetime) -> bool:
    """True during regular US equity trading hours (Mon-Fri, 09:30-16:00 ET).
    Holidays aren't modelled — a holiday just force-refreshes an unchanged
    hot set, which the freshness cache makes near-free."""
    if now.weekday() >= 5:
        return False
    return "09:30" <= now.strftime("%H:%M") <= "16:00"


def _maybe_hot_refresh(now: datetime) -> None:
    """During market hours, keep the live-risk names (open positions, entry
    candidates, earnings-imminent) current by force-refreshing the small "hot"
    set on the HOT_REFRESH_MINUTES cadence — while the long tail rides the daily
    pre-open warm-up. Best-effort: logged, never fatal to the tick."""
    import refresh_policy
    if not refresh_policy.enabled() or not _market_hours(now):
        return
    try:
        result = refresh_policy.maybe_refresh_hot(now)
        if result and result["count"]:
            logger.info("hot refresh: %d tickers (%s)", result["count"],
                        ", ".join(result["tickers"][:8]))
    except Exception as e:  # noqa: BLE001 — a refresh must never break the tick
        logger.warning("hot refresh failed: %s", e)


def tier_poll_enabled() -> bool:
    """Tiered quote polling on by default; CFM_TIER_POLL=0 turns it off (tests,
    CLI tools, or to fall back to the legacy flat hot-refresh alone)."""
    return os.environ.get("CFM_TIER_POLL", "1").strip() not in ("0", "false", "no")


def _maybe_tier_poll(now: datetime) -> None:
    """Run one tiered polling cycle: batched Tier 0/1 quotes, defense/market
    escalation, and the intraday kill-switch RS3M refresh. Its own cadence gates
    live inside ``tier_poll.run_cycle`` (fetch_due per symbol), so this runs every
    tick during market hours. Best-effort: logged, never fatal to the tick."""
    if not tier_poll_enabled() or not _market_hours(now):
        return
    try:
        import tier_poll
        result = tier_poll.run_cycle(now)
        if result and result.get("due"):
            logger.info("tier poll: %d quotes (%s)%s", len(result["due"]),
                        ", ".join(result["due"][:8]),
                        f", {len(result['degraded'])} degraded" if result.get("degraded") else "")
        if result and result.get("escalations"):
            for detail in result["escalations"]:
                logger.warning("defense escalation: %s", detail)
        if result and result.get("market_escalation"):
            logger.warning("%s", result["market_escalation"])
    except Exception as e:  # noqa: BLE001 — a poll must never break the tick
        logger.warning("tier poll failed: %s", e)


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

    # Keep the live-risk names fresh intraday. Runs every tick (its own cadence
    # gate rate-limits the actual refresh), so it must sit BEFORE the slot-based
    # early return below. The hot refresh keeps daily BARS current (EOD/warm/post-
    # close); the tiered poll adds batched intraday QUOTES + escalation on top.
    _maybe_hot_refresh(now)
    _maybe_tier_poll(now)
    _maybe_interval_reconcile(now)

    due = due_slots(now)
    if not due:
        return
    # Pre-market reconciliation runs on the FIRST morning slot, before the alert
    # pass, so reconcile_dirty / short_stock_detected fire off a fresh report.
    _maybe_morning_reconcile(now, due)
    # Post-close slot: force the hot set current so the OFFICIAL close is in the
    # cache before the confirmed-close kill switch / end-of-day circuit breaker
    # evaluate. _maybe_hot_refresh above skips it (past 16:00), so refresh here.
    _maybe_post_close_refresh(now, due)
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
    # Recommendation pass — the SAME slots as the alert pass (incl. 16:15 for
    # the confirmed-close kill switch), after it: the alert engine pages on raw
    # conditions first; the engine then commits to the explicit recommendation
    # records the trust scoreboard measures. A failed pass pages like a failed
    # alert run — an engine that silently stops emitting voids the coverage
    # evidence, which is exactly the failure the scoreboard exists to catch.
    if recommendations_enabled():
        try:
            import recommendation_runner
            summary = recommendation_runner.run()
            logger.info("scheduled recommendation pass (%s ET): %d emitted",
                        "+".join(due), summary.get("emitted", 0))
        except Exception as e:  # noqa: BLE001
            import heartbeat
            heartbeat.ping("/fail", force=True)
            logger.error("scheduled recommendation pass (%s ET) failed: %s",
                         "+".join(due), e)
    # Warm the full-universe scan cache after the alert pass (which is what pages
    # the operator, so it runs first). At the pre-open 08:30 slot this primes the
    # morning's first Scan; later slots keep the daily-bar cache from ageing out.
    _warm_scan()


def _maybe_post_close_refresh(now: datetime, due: list[str]) -> None:
    """At the post-close slot, force-refresh the hot set so today's official
    close is cached before the alert pass evaluates confirmed-close conditions.
    Best-effort — logged, never fatal to the tick. Skipped in demo mode / when
    the refresh tier is off (a demo/offline evaluation reads the pinned store)."""
    if config.POST_CLOSE_SLOT_ET not in due:
        return
    import refresh_policy
    if not refresh_policy.enabled():
        return
    try:
        result = refresh_policy.maybe_refresh_hot(now, force=True)
        if result and result.get("count"):
            logger.info("post-close refresh: %d tickers before EOD alert pass",
                        result["count"])
    except Exception as e:  # noqa: BLE001 — a refresh must never break the tick
        logger.warning("post-close refresh failed: %s", e)


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


def reconcile_interval_enabled() -> bool:
    """Intraday reconcile+ingest cadence on by default; CFM_RECONCILE_INTERVAL=0
    turns it off (tests, CLI tools, or to fall back to the pre-market run alone)."""
    return os.environ.get("CFM_RECONCILE_INTERVAL", "1").strip() not in ("0", "false", "no")


def _reconcile_window(now: datetime) -> bool:
    """Market hours plus a short post-close tail (through 16:30 ET) so the "once
    after close" reconcile+ingest run (spec §4) happens on the same cadence gate."""
    if now.weekday() >= 5:
        return False
    return "09:30" <= now.strftime("%H:%M") <= "16:30"


def _maybe_interval_reconcile(now: datetime) -> None:
    """During market hours (+ a post-close tail), run position reconciliation AND
    transaction ingestion on the RECONCILE_INTERVAL_MINUTES cadence. Reconcile
    surfaces divergence/freeze; ingestion pulls broker executions as ground truth
    (confirming app fills, surfacing out-of-band trades for one-click adoption).
    Best-effort — each is isolated and logged, never fatal to the tick. Requires
    Schwab connected (read-only is enough) or demo mode."""
    global _last_interval_reconcile
    if not reconcile_interval_enabled() or not _reconcile_window(now):
        return
    import schwab_api
    if not (schwab_api.configured() or config.demo_enabled()):
        return
    last = _last_interval_reconcile
    if last is not None and (now - last).total_seconds() / 60.0 < float(
            config.RECONCILE_INTERVAL_MINUTES):
        return
    _last_interval_reconcile = now
    try:
        import reconcile
        report = reconcile.run_reconciliation()
        logger.info("interval reconciliation: status=%s diffs=%d",
                    report.get("status"), len(report.get("diffs", [])))
    except Exception as e:  # noqa: BLE001 — a failed reconcile must not kill the thread
        logger.error("interval reconciliation failed: %s", e)
    try:
        import transaction_ingest
        ing = transaction_ingest.run_ingestion()
        if ing.get("proposals") or ing.get("matched"):
            logger.info("interval ingestion: %d matched, %d out-of-band proposal(s)",
                        len(ing.get("matched") or []), len(ing.get("proposals") or []))
    except Exception as e:  # noqa: BLE001 — a failed ingestion must not kill the thread
        logger.error("interval transaction ingestion failed: %s", e)


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
        # Warm the scan cache once on startup, off-thread so it never delays boot,
        # so a deploy/restart during the day doesn't leave the first Scan cold.
        if warm_scan_enabled():
            threading.Thread(target=_warm_scan, name="scan-warmup", daemon=True).start()
        return True
