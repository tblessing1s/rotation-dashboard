"""In-process day-trade scheduler.

Same rationale and shape as ``alert_scheduler.py`` (see its module docstring):
the persistent volume attaches to one machine, so a background thread in the
one app process — not a second scheduled machine — is the only shape that
needs no new infrastructure. A second daemon thread (not a hook into
``alert_scheduler``'s own loop) so a bug in one sleeve's tick can never stall
the other's, and so this sleeve can be disabled independently
(``CFM_DAYTRADE_SCHEDULER=0``) without touching CFM's alerts.

PER ACCOUNT: the screener/bar ingest are SHARED (one nightly universe scan,
one set of ingested bars — which stocks qualify doesn't depend on account
data). Everything downstream of that — the signal engine, its budget, its
trial, its digest line — runs independently per account with day-trading
turned on (``daytrade.settings.enabled_account_ids()``). ``_for_each_
enabled_account`` is the per-tick iterator every per-account job uses,
mirroring ``alert_scheduler.for_each_account``'s isolation: one account's
exception must never skip another's.

Jobs, all best-effort (logged, never fatal to the tick):

  screen        once per trading day, after the close (``DAYTRADE_SCREEN_ET``)
                — ``daytrade.universe.screen()``, SHARED, filed under the
                NEXT trading day (today's close is tomorrow's prior-day
                levels; bar ingest/signals look up "today's screen" by exact
                date, so filing under today would leave every day's own
                window with nothing to read). Skipped when no
                account is enabled, or every enabled account's paper trial
                is already complete: no screener picks that day means bar
                ingest and the signal engine have nothing to do for anyone,
                so gating the screener alone quietly stops the whole
                pipeline for future days.
  bar ingest    every ``DAYTRADE_BAR_INTERVAL_MINUTES`` during the Rule 2
                signal window (``DAYTRADE_WINDOW_START_ET``-``_END_ET``) —
                ``daytrade.bars.ingest()``, SHARED.
  signals       after every bar ingest, and once more at the window's end,
                ONE RUN PER ENABLED ACCOUNT — ``daytrade.signals.run_day()``,
                each sized off that account's own live budget
                (``daytrade.budget``) and gated on that account's own
                trial's completion (``entries_enabled``): a trade already
                open the day an account's trial completes still resolves
                normally, and every other enabled account keeps running.
  daily digest  once per trading day (``DAYTRADE_DIGEST_ET``) —
                ``daytrade.digest.send_daily_digest()`` pushes every
                enabled account's running P&L/R in one combined message, so
                it reaches the operator without opening the UI.

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
# Trading day the daily performance digest last went out.
_last_digest_day: date | None = None


def enabled() -> bool:
    """Scheduler on by default; CFM_DAYTRADE_SCHEDULER=0 turns it off (tests,
    CLI tools importing app, one-off scripts). Independent of any per-
    account day-trading toggle (daytrade.settings) — this is the process-
    level kill switch for the whole thread."""
    return os.environ.get("CFM_DAYTRADE_SCHEDULER", "1").strip() not in ("0", "false", "no")


def _for_each_enabled_account(what: str, fn) -> None:
    """Run ``fn(account_id)`` for every account with day-trading turned on.
    ``fn`` owns its own error handling for anything it wants to survive;
    the guard here only stops one account's unexpected explosion from
    skipping the accounts after it — same isolation as alert_scheduler.
    for_each_account."""
    import accounts
    from daytrade import settings
    for account_id in settings.enabled_account_ids():
        try:
            with accounts.use(account_id):
                fn(account_id)
        except Exception as e:  # noqa: BLE001 — one account must not sink the others
            logger.error("%s failed for account %s: %s", what, account_id, e)


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


def digest_due(now: datetime, last_day: date | None) -> bool:
    """Daily digest: fires once per day, at/after DAYTRADE_DIGEST_ET. Every
    calendar day, not just trading days — a Saturday digest just repeats
    Friday's still-accurate trial status, same trade-off market_calendar-
    unaware alert_scheduler slots make elsewhere."""
    return now.strftime("%H:%M") >= config.DAYTRADE_DIGEST_ET and last_day != now.date()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _run_screen(now: datetime) -> None:
    try:
        from daytrade import universe
        # Files under the NEXT trading day, not today: this runs after
        # today's close, using today's just-completed session as the prior-
        # day levels for tomorrow's setups (see universe.py's module
        # docstring) — but bar ingest and the signal engine both look up
        # "today's screen" by exact date match, so filing under today would
        # leave every trading day's own window with nothing to read, forever.
        target = market_calendar.next_trading_day(now.date())
        result = universe.screen(now=now, date_override=target, trigger="scheduled")
        logger.info("daytrade screener: %d pick(s) from %d screened, filed for %s",
                     len(result["picks"]), len(result["screened"]), result["date"])
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal to the tick
        logger.warning("daytrade screener failed: %s", e)


def _maybe_screen(now: datetime) -> None:
    global _last_screen_day
    if not market_calendar.is_trading_day(now.date()):
        return
    if not screen_due(now, _last_screen_day):
        return
    _last_screen_day = now.date()
    try:
        from daytrade import settings, trial
        enabled_ids = settings.enabled_account_ids()
        if not enabled_ids:
            logger.info("daytrade screener skipped — no account has day-trading enabled")
            return
        if all(trial.trial_status(aid)["status"] == "complete" for aid in enabled_ids):
            logger.info("daytrade screener skipped — every enabled account's paper "
                        "trial is complete, no new entries")
            return
    except Exception as e:  # noqa: BLE001 — a settings/trial read failure must not block screening
        logger.warning("daytrade trial status check failed (%s); screening anyway", e)
    _run_screen(now)


def _run_bar_ingest(now: datetime) -> None:
    try:
        from daytrade import bars
        result = bars.ingest(now)
        logger.info("daytrade bar ingest: %d bar(s) written across %d symbol(s)",
                     result["written"], len(result["symbols"]))
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal to the tick
        logger.warning("daytrade bar ingest failed: %s", e)


def _run_signals(now: datetime, account_id: str) -> None:
    try:
        from daytrade import budget, signals, trial
        day = now.strftime("%Y-%m-%d")
        # Live budget every run — see budget.daytrade_budget: THIS account's
        # own dry powder, falling back to the static config placeholder on
        # any read failure. run_day() itself still defaults to the
        # placeholder when account_equity is omitted (the signal-engine
        # tests rely on that default staying pure/offline), so the live
        # figure only reaches production runs through this explicit pass.
        equity = budget.daytrade_budget(account_id)
        # Same for the trial gate: run_day() defaults entries_enabled=True,
        # so THIS account's paper-trading trial completion only reaches
        # production runs through this explicit pass too.
        status = trial.trial_status(account_id)
        entries_enabled = status["status"] != "complete"
        result = signals.run_day(day, account_id, now=now, account_equity=equity["amount"],
                                 entries_enabled=entries_enabled)
        logger.info("daytrade signals: %d total event(s) journaled for %s (account %s, "
                    "budget $%.2f from %s, trial %d/%d%s)",
                    len(result["events"]), day, account_id, equity["amount"], equity["detail"],
                    status["completed_trades"], status["target_trades"],
                    "" if entries_enabled else " — COMPLETE, no new entries")
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal to the tick
        logger.warning("daytrade signal engine failed for account %s: %s", account_id, e)


def _maybe_bar_ingest(now: datetime) -> None:
    global _last_bar_fetch
    if not market_calendar.is_trading_day(now.date()):
        return
    if not bar_fetch_due(now, _last_bar_fetch):
        return
    _last_bar_fetch = now
    _run_bar_ingest(now)
    # Evaluate every enabled account's signal engine against the bars just
    # ingested — rules 3-6 react to each new candle as it lands, not just
    # once at day's end.
    _for_each_enabled_account("signals", lambda aid: _run_signals(now, aid))


def _maybe_finalize_signals(now: datetime) -> None:
    """Once per trading day, at/after the window ends: force any still-open
    trade to its rule-6 time-cutoff exit (see signals.run_day/_finalize_open_
    trades), for every enabled account. Separate from _maybe_bar_ingest's
    in-window run because bar ingestion — and with it the in-window signals
    run — stops once the window closes, but the cutoff itself still needs
    one more pass."""
    global _last_signals_finalize_day
    if not market_calendar.is_trading_day(now.date()):
        return
    if not signals_finalize_due(now, _last_signals_finalize_day):
        return
    _last_signals_finalize_day = now.date()
    _for_each_enabled_account("signals finalize", lambda aid: _run_signals(now, aid))


def _run_daily_digest() -> None:
    try:
        from daytrade import digest
        report = digest.send_daily_digest()
        logger.info("daytrade daily digest: %s", report or "no channel configured "
                    "(or no account enabled)")
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal to the tick
        logger.warning("daytrade daily digest failed: %s", e)


def _maybe_daily_digest(now: datetime) -> None:
    global _last_digest_day
    if not digest_due(now, _last_digest_day):
        return
    _last_digest_day = now.date()
    _run_daily_digest()


def _tick() -> None:
    now = datetime.now(ET)
    _maybe_screen(now)
    _maybe_bar_ingest(now)
    _maybe_finalize_signals(now)
    _maybe_daily_digest(now)


def _loop() -> None:
    while not _stop.wait(_TICK_SECONDS):
        try:
            _tick()
        except BaseException as e:  # noqa: BLE001 — one bad tick must not kill the daemon.
            # BaseException, not Exception: a broken native dependency (seen
            # in practice — a broken `cryptography`/pyo3 build makes
            # webpush.configured() raise pyo3_runtime.PanicException, which
            # does NOT subclass Exception) would otherwise escape this
            # thread's target function entirely, silently ending the
            # scheduler for the rest of the process with no crash and no
            # further log line to explain why it stopped.
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
