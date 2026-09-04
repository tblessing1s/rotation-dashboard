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
# The scan day this process has already ensured a full-universe sweep for. The
# sweep itself runs ONCE per trading day, outside trading hours: the scan day rolls
# just after the close (scan_cache.SCAN_ROLL_ET), so the first tick past the roll
# finds an empty cache and runs it on that session's final bars.
_last_warm_scan_day: str | None = None


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


# ---------------------------------------------------------------------------
# Multi-account fan-out
#
# The daemon evaluates BOOKS, and there is now one book per account. Anything
# that reads or writes state (alerts, recommendations, reconciliation, ingestion,
# expiry checks, nightly maintenance, backups, the hot/tier refreshes whose sets
# come from open positions) therefore runs once per non-archived account, each
# inside ``accounts.use`` so every state read underneath resolves to that book.
# Market-wide work (the full-universe scan sweep) stays a single pass — it is the
# same universe whichever account is looking at it.
#
# Cadence gates stay GLOBAL: "every N minutes, reconcile the books" is one clock
# for all of them, not N drifting clocks. Registries keyed per position (the
# mandatory expiry checks) are per account, since two books can hold the same
# ticker and strike.
# ---------------------------------------------------------------------------
def account_ids() -> list[str | None]:
    """Accounts this tick should evaluate. ``[None]`` means "just the active
    book" — the degraded path when the registry can't be read, which must never
    stop the daemon from monitoring the book it can see."""
    try:
        import accounts
        return list(accounts.scheduled_ids()) or [None]
    except Exception as e:  # noqa: BLE001 — a registry problem must not kill the tick
        logger.error("could not enumerate accounts (%s); evaluating the active book only", e)
        return [None]


def for_each_account(what: str, fn) -> None:
    """Run ``fn(account_id)`` against every scheduled account in turn.

    ``fn`` owns its own error handling (several of these page the dead-man's
    switch on failure); the guard here only stops one account's unexpected
    explosion from skipping the accounts after it.
    """
    import accounts
    for account_id in account_ids():
        try:
            with accounts.use(account_id):
                fn(account_id)
        except Exception as e:  # noqa: BLE001 — one book must not sink the others
            logger.error("%s failed for account %s: %s", what, account_id or "active", e)


def _account_label(account_id: str | None) -> str:
    return account_id or "active"


def _warm_scan() -> None:
    """Run the day's full-universe sweep so the operator's Scan loads warm.

    Never forces: when this scan day's sweep is already on disk the call
    short-circuits to a cache read. Best-effort: logged, never fatal to the tick
    or the process."""
    if not warm_scan_enabled():
        return
    try:
        import scan_cache
        import screening
        import sector_data
        from metrics import scorecard as scorecard_metrics
        # Cheap pre-check purely so the log says which happened; the sweep itself
        # is idempotent per epoch either way.
        if scan_cache.status(sector_data.all_tickers(),
                             scorecard_metrics._current_regime_color())["warm"]:
            logger.debug("this scan day's sweep is already cached; skipping")
            return
        result = screening.warm_scan_cache()
        if result.get("ok"):
            logger.info("scan cache warmed")
        else:
            logger.warning("scan cache warm-up incomplete: %s", result.get("error"))
    except Exception as e:  # noqa: BLE001 — a warm-up must never break its caller
        logger.warning("scan cache warm-up failed: %s", e)


def warm_scan_due(now: datetime, last_day: str | None, current_day: str) -> bool:
    """PURE: does this scan day still need its full-universe sweep? True when this
    process hasn't already handled ``current_day``. The scan day rolls just after
    the close, so the answer flips once a day, outside trading hours — never on an
    interval. Unit-testable without threads/clock."""
    return last_day != current_day


def _maybe_warm_scan(now: datetime) -> None:
    """Ensure the CURRENT scan day has a full-universe sweep — at most one per day.

    Normally this fires on the first tick after the post-close roll and sweeps that
    session's final bars, outside trading hours. It is also the recovery path: if
    the machine was stopped at the roll (Fly auto-stops), the next tick notices the
    day has no sweep and runs it, which is far better than handing the operator a
    cold ~25s sweep on the request path. When the day's sweep is already on disk —
    including one written by a previous process — this records the day and does
    nothing. Best-effort: logged, never fatal to the tick."""
    global _last_warm_scan_day
    if not warm_scan_enabled():
        return
    try:
        import scan_cache
        day = scan_cache.scan_day(now)
    except Exception as e:  # noqa: BLE001 — never break the tick on a clock/calendar read
        logger.warning("could not resolve the scan day: %s", e)
        return
    if not warm_scan_due(now, _last_warm_scan_day, day):
        return
    _last_warm_scan_day = day
    _warm_scan()


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

    def run(account_id):
        try:
            result = refresh_policy.maybe_refresh_hot(now)
            if result and result["count"]:
                logger.info("hot refresh (%s): %d tickers (%s)", _account_label(account_id),
                            result["count"], ", ".join(result["tickers"][:8]))
        except Exception as e:  # noqa: BLE001 — a refresh must never break the tick
            logger.warning("hot refresh failed for account %s: %s", _account_label(account_id), e)

    # Per account: the hot set is "this book's open positions + candidates". A
    # second book's names are a different set, and its live risk is no less live.
    for_each_account("hot refresh", run)


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

    def run(account_id):
        try:
            import tier_poll
            result = tier_poll.run_cycle(now)
            if result and result.get("due"):
                logger.info("tier poll (%s): %d quotes (%s)%s", _account_label(account_id),
                            len(result["due"]), ", ".join(result["due"][:8]),
                            f", {len(result['degraded'])} degraded" if result.get("degraded") else "")
            if result and result.get("escalations"):
                for detail in result["escalations"]:
                    logger.warning("defense escalation (%s): %s", _account_label(account_id), detail)
            if result and result.get("market_escalation"):
                logger.warning("%s", result["market_escalation"])
        except Exception as e:  # noqa: BLE001 — a poll must never break the tick
            logger.warning("tier poll failed for account %s: %s", _account_label(account_id), e)

    # Per account: tiering, defense escalation and the kill-switch refresh all key
    # off the book's own positions. The per-symbol quote cadence inside tier_poll
    # is shared, so two books holding the same name still cost one quote.
    for_each_account("tier poll", run)


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


# ---------------------------------------------------------------------------
# Mandatory DATE-SPECIFIC checks (schema v22, CSP Stage 2 §2.2)
#
# Every other check in this daemon is RECURRING: a slot fires once per day, every
# day, and the conditions decide whether anything is wrong. That model cannot
# express "this must run on THIS date, before the close, and its absence is
# itself a failure" — which is exactly what a put's expiry day is. A put left
# unattended through expiry is assigned, and an assignment nobody evaluated is
# the silent-drift failure this whole feature exists to prevent.
#
# So the daemon is EXTENDED rather than duplicated: one date-keyed registry
# alongside `_last_run`, evaluated in the SAME tick loop, covered by the SAME
# dead-man's-switch ping. There is no second thread and no second schedule.
# ---------------------------------------------------------------------------
# "YYYY-MM-DD:check" -> the date it ran. In-memory like _last_run; a restart may
# re-run a check, which alert dedup makes a no-op.
_mandatory_run: dict[str, date] = {}
# Per-account view of the registry above. Two books can hold the same ticker and
# strike expiring the same day, so the run keys have to be scoped by account or
# one book's completed check would mark the other's as done. The primary account
# keeps the shared registry object so the single-account behaviour (and the
# module-level default in mandatory_expiry_checks) is exactly unchanged.
_mandatory_run_by_account: dict[str, dict[str, date]] = {}


def mandatory_registry(account_id: str | None) -> dict:
    """The expiry-check run registry for one account."""
    try:
        import accounts
        default = accounts.DEFAULT_ID
    except Exception:  # noqa: BLE001
        default = "primary"
    if account_id in (None, default):
        return _mandatory_run
    return _mandatory_run_by_account.setdefault(account_id, {})

# PROPOSED_DEFAULT — how long before the close the expiry-day check must have run.
# 15:30 ET is the existing pre-close alert slot, so the mandatory check rides a
# time the daemon already wakes for rather than introducing a new one.
EXPIRY_CHECK_ET = "15:30"


def mandatory_expiry_checks(state: dict, now: datetime,
                            last: dict | None = None) -> list[str]:
    """Keys for every put expiring TODAY whose mandatory pre-close check is due.

    PURE given ``now`` and the run registry, so the whole scheduling rule is
    testable with a mocked clock and no daemon. A put expiring today is due once
    the clock passes ``EXPIRY_CHECK_ET`` and the check has not already run today.
    """
    last = _mandatory_run if last is None else last
    today = now.date()
    if now.strftime("%H:%M") < EXPIRY_CHECK_ET:
        return []
    due = []
    for p in (state or {}).get("positions", []):
        if p.get("status") == "closed":
            continue
        for leg in p.get("short_puts") or []:
            if str(leg.get("expiration") or "")[:10] != today.isoformat():
                continue
            key = f"{today.isoformat()}:expiry:{p.get('ticker')}:{leg.get('strike')}"
            if last.get(key) != today:
                due.append(key)
    return due


def _maybe_expiry_check(now: datetime) -> None:
    """Run the mandatory expiry-day re-gate for any put expiring today.

    A FAILURE HERE PAGES. Every other evaluator in this daemon swallows its own
    exception so a broken condition cannot kill the thread — correct, because a
    missed recurring check runs again in an hour. This one does not get another
    chance: the put expires today, and a missed evaluation is a silent assignment
    with real money attached. So a failure pings the dead-man's switch's /fail
    endpoint immediately, exactly as a failed alert run does.
    """
    import alerts
    import heartbeat
    import logging_handler as log

    def run(account_id):
        registry = mandatory_registry(account_id)
        try:
            state = log.load_state()
        except Exception as e:  # noqa: BLE001
            logger.error("expiry-day check could not load state for account %s: %s",
                         _account_label(account_id), e)
            heartbeat.ping("/fail", force=True)
            return
        due = mandatory_expiry_checks(state, now, registry)
        if not due:
            return
        try:
            alerts.run()
            for key in due:
                registry[key] = now.date()
            logger.info("mandatory expiry-day put check ran for %d leg(s) on account %s",
                        len(due), _account_label(account_id))
        except Exception as e:  # noqa: BLE001 — the thread survives, but this PAGES
            heartbeat.ping("/fail", force=True)
            logger.error("MANDATORY expiry-day put check FAILED on account %s (%s): %s",
                         _account_label(account_id), ", ".join(due), e)

    # Every book gets the check: a put expiring today in the second account is
    # exactly as unattended as one in the first.
    for_each_account("expiry-day put check", run)


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

        def _nightly(account_id):
            try:
                import maintenance
                maintenance.nightly_refresh()
            except Exception as e:  # noqa: BLE001 — a failed refresh must not kill the thread
                logger.error("nightly maintenance failed for account %s: %s",
                             _account_label(account_id), e)

        # Per account: the refresh syncs each book's held names and takes that
        # book's nightly backup (backups.py keeps them in per-account directories,
        # so one book's rotation can't age out another's).
        for_each_account("nightly maintenance", _nightly)

    # Keep the live-risk names fresh intraday. Runs every tick (its own cadence
    # gate rate-limits the actual refresh), so it must sit BEFORE the slot-based
    # early return below. The hot refresh keeps daily BARS current (EOD/warm/post-
    # close); the tiered poll adds batched intraday QUOTES + escalation on top.
    _maybe_hot_refresh(now)
    _maybe_tier_poll(now)
    _maybe_interval_reconcile(now)
    _maybe_warm_scan(now)  # keep the full-universe scan cache warm between slots
    # Mandatory date-specific put expiry check. Runs every tick (its own date gate
    # decides), so it must sit BEFORE the slot-based early return below — a put
    # expiring today must be evaluated even on a day no recurring slot is due.
    _maybe_expiry_check(now)

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
    def _alert_pass(account_id):
        try:
            result = alerts.run()
            logger.info("scheduled alert run (%s ET, account %s): %d fired, %d resolved, %d active",
                        "+".join(due), _account_label(account_id), len(result["fired"]),
                        len(result["resolved"]), result["active_count"])
        except Exception as e:  # noqa: BLE001 — a failed run must not kill the thread
            # The thread is alive but the evaluation itself broke — page immediately
            # (a persistently failing run is as dangerous as a dead thread).
            import heartbeat
            heartbeat.ping("/fail", force=True)
            logger.error("scheduled alert run (%s ET) failed for account %s: %s",
                         "+".join(due), _account_label(account_id), e)

    for_each_account("scheduled alert run", _alert_pass)
    # Recommendation pass — the SAME slots as the alert pass (incl. 16:15 for
    # the confirmed-close kill switch), after it: the alert engine pages on raw
    # conditions first; the engine then commits to the explicit recommendation
    # records the trust scoreboard measures. A failed pass pages like a failed
    # alert run — an engine that silently stops emitting voids the coverage
    # evidence, which is exactly the failure the scoreboard exists to catch.
    if recommendations_enabled():
        def _recommendation_pass(account_id):
            try:
                import recommendation_runner
                summary = recommendation_runner.run(trigger="scheduled")
                logger.info("scheduled recommendation pass (%s ET, account %s): %d emitted",
                            "+".join(due), _account_label(account_id), summary.get("emitted", 0))
            except Exception as e:  # noqa: BLE001
                import heartbeat
                heartbeat.ping("/fail", force=True)
                logger.error("scheduled recommendation pass (%s ET) failed for account %s: %s",
                             "+".join(due), _account_label(account_id), e)

        for_each_account("scheduled recommendation pass", _recommendation_pass)
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
    def run(account_id):
        try:
            result = refresh_policy.maybe_refresh_hot(now, force=True)
            if result and result.get("count"):
                logger.info("post-close refresh (%s): %d tickers before EOD alert pass",
                            _account_label(account_id), result["count"])
        except Exception as e:  # noqa: BLE001 — a refresh must never break the tick
            logger.warning("post-close refresh failed for account %s: %s",
                           _account_label(account_id), e)

    for_each_account("post-close refresh", run)


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

    def run(account_id):
        try:
            import reconcile
            report = reconcile.run_reconciliation()
            logger.info("pre-market reconciliation (%s): status=%s diffs=%d",
                        _account_label(account_id), report.get("status"),
                        len(report.get("diffs", [])))
        except Exception as e:  # noqa: BLE001 — a failed reconcile must not kill the thread
            logger.error("pre-market reconciliation failed for account %s: %s",
                         _account_label(account_id), e)

    # One clock (above), every book reconciled against ITS OWN brokerage account.
    for_each_account("pre-market reconciliation", run)


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

    def run(account_id):
        label = _account_label(account_id)
        try:
            import reconcile
            report = reconcile.run_reconciliation()
            logger.info("interval reconciliation (%s): status=%s diffs=%d",
                        label, report.get("status"), len(report.get("diffs", [])))
        except Exception as e:  # noqa: BLE001 — a failed reconcile must not kill the thread
            logger.error("interval reconciliation failed for account %s: %s", label, e)
        try:
            import transaction_ingest
            ing = transaction_ingest.run_ingestion()
            if ing.get("proposals") or ing.get("matched"):
                logger.info("interval ingestion (%s): %d matched, %d out-of-band proposal(s)",
                            label, len(ing.get("matched") or []),
                            len(ing.get("proposals") or []))
        except Exception as e:  # noqa: BLE001 — a failed ingestion must not kill the thread
            logger.error("interval transaction ingestion failed for account %s: %s", label, e)

    for_each_account("interval reconciliation", run)


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
