"""Day-trade nightly screener — strategy rule 1.

Screens the day-trade sleeve's OWN ticker roster (``daytrade/tickers.py``,
seeded once from CFM's universe but independent from then on — see its
module docstring) for $20-150 stocks, avg daily volume > 1M, and ATR% between
2% and 5% (``config.DAYTRADE_*``), and records each candidate's most recent
completed session's high/low as the next day's prior-day levels for the
setup/entry rules (a later phase). Reuses the CFM daily-bar plumbing
(``data_handler.get_daily``) rather than a new data path — the day-trade
sleeve is a new STRATEGY, not a new DATA SOURCE.

``screen()`` logs every candidate looked at, pass or fail (``screened``), not
just the picks — adherence/coverage is auditable from day one, the same spirit
as the brief's "log every signal, taken or not" rule applied to the universe
step.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone

import config
import data_handler
import indicators

from daytrade import store, tickers as daytrade_tickers

logger = logging.getLogger("cfm.daytrade")


def _evaluate(symbol: str) -> dict:
    """One ticker's screener readout. Never raises — a fetch failure is
    recorded as a failed candidate, not a crash of the whole nightly sweep.

    force=True: get_daily's 12h cache is fine for a read-heavy path like a
    scan, but wrong here — the screener's whole job is to capture the most
    recently COMPLETED session's high/low/close, and a same-day cache entry
    (e.g. from an earlier "Rescan now" before that session even closed) can
    be under 12h old while still predating today's close. Without this, the
    after-close scheduled run can silently re-serve that stale pre-close
    frame instead of the real closing data, and every screen after it keeps
    inheriting the same frozen prior-day levels."""
    try:
        df = data_handler.get_daily(symbol, force=True)
    except Exception as e:  # noqa: BLE001 — one bad symbol must not sink the sweep
        return {"symbol": symbol, "qualified": False, "reason": f"data unavailable: {e}"}
    if df is None or df.empty:
        return {"symbol": symbol, "qualified": False, "reason": "no data"}

    last = df.iloc[-1]
    price = float(last["Close"])
    avg_volume = float(df["Volume"].tail(config.DAYTRADE_AVG_VOLUME_LOOKBACK_DAYS).mean())
    atr14 = indicators.atr(df, window=config.DAYTRADE_ATR_WINDOW)
    atr_pct = indicators.atr_pct(df, window=config.DAYTRADE_ATR_WINDOW)

    row = {
        "symbol": symbol,
        "price": round(price, 2),
        "avg_volume": round(avg_volume),
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "atr_pct": atr_pct,
        "prior_day_high": round(float(last["High"]), 2),
        "prior_day_low": round(float(last["Low"]), 2),
    }
    reasons = []
    if not (config.DAYTRADE_MIN_PRICE <= price <= config.DAYTRADE_MAX_PRICE):
        reasons.append("price out of range")
    if avg_volume <= config.DAYTRADE_MIN_AVG_VOLUME:
        reasons.append("avg volume too low")
    if atr_pct is None or not (config.DAYTRADE_ATR_PCT_MIN <= atr_pct <= config.DAYTRADE_ATR_PCT_MAX):
        reasons.append("ATR% out of range")
    row["qualified"] = not reasons
    row["reason"] = None if row["qualified"] else "; ".join(reasons)
    return row


def rank(qualified: list[dict]) -> list[dict]:
    """PROPOSED_DEFAULT tie-break when more than DAYTRADE_UNIVERSE_MAX names
    qualify: most-liquid first (avg_volume desc). The brief specifies the
    qualifying bounds but not a selection order among names that clear them;
    liquidity is the safer default for a 1%-risk intraday strategy. Revisit
    once the signal engine (a later phase) has real fills to calibrate
    against."""
    return sorted(qualified, key=lambda r: r["avg_volume"], reverse=True)


def screen(tickers: list[str] | None = None, now: datetime | None = None,
           date_override: date | None = None, trigger: str = "manual") -> dict:
    """Run the nightly screener once and persist the result. Returns the same
    dict that gets written to disk.

    ``date_override`` files the result under a date other than ``now``'s —
    the scheduler's own after-close run uses this to file under the NEXT
    trading day (see its module docstring: today's close feeds tomorrow's
    prior-day levels), since bar ingest and the signal engine both look up
    "today's screen" by exact date match. An on-demand "Rescan now" leaves
    this unset and files under today, same-day, as before.

    ``trigger`` ("scheduled" | "manual") records this run's success
    separately per trigger (store.save_screen_health) — an operator's actual
    question is "did LAST NIGHT'S automated job run," which a manual
    "Rescan now" success must never quietly answer for it."""
    now = now or datetime.now(timezone.utc)
    target_date = date_override or now.date()
    tickers = tickers if tickers is not None else daytrade_tickers.all_tickers()

    # Warm the cache for every candidate in parallel (data_handler's 8-worker
    # pool) BEFORE scoring — same pattern CFM's own full-universe scan uses
    # (metrics/scorecard.py). Without this, _evaluate's per-ticker
    # get_daily() calls run one at a time, and on a cold cache (e.g. right
    # after importing a large CSV of names never fetched before) that means
    # serial, rate-limited network calls — minutes instead of seconds for a
    # roster in the hundreds/thousands. force=True to match _evaluate's own
    # force=True below — otherwise this warms the cache respecting the 12h
    # TTL, _evaluate force-fetches anyway, and the parallel warm-up was
    # wasted work instead of where the real fetching happens.
    data_handler.prefetch(tickers, force=True)

    screened = [_evaluate(t) for t in tickers]
    qualified = [r for r in screened if r.get("qualified")]
    picks = rank(qualified)[:config.DAYTRADE_UNIVERSE_MAX]

    result = {
        "schema_version": store.SCHEMA_VERSION,
        "date": target_date.strftime("%Y-%m-%d"),
        "computed_at": now.astimezone(timezone.utc).isoformat(),
        "picks": picks,
        "screened": screened,
    }
    store.save_screen(result)
    store.save_screen_health({
        "trigger": trigger,
        "succeeded_at": datetime.now(timezone.utc).isoformat(),
        "date": result["date"],
        "picks": len(picks),
        "screened": len(screened),
    })
    if len(picks) < config.DAYTRADE_UNIVERSE_MIN:
        logger.warning("daytrade screener: only %d name(s) qualified (rule 1 wants %d-%d)",
                        len(picks), config.DAYTRADE_UNIVERSE_MIN, config.DAYTRADE_UNIVERSE_MAX)
    return result


# ---------------------------------------------------------------------------
# On-demand rescan — the operator's "Rescan now" button.
#
# The nightly run is scheduler-owned (daytrade/scheduler.py's _maybe_screen)
# and gated on "hasn't run today yet" / "some account is enabled" / "not
# every trial is already complete" — none of which should block an explicit
# manual request, so this calls screen() directly rather than routing
# through the scheduler. Same detached-thread shape as
# screening.start_background_scan for CFM's own universe: the roster can
# run into the hundreds (bigger after a CSV import of names never fetched
# before, i.e. cold-cache), so this returns immediately and the client polls
# screen_status() rather than holding the request open.
# ---------------------------------------------------------------------------
_scan_thread: threading.Thread | None = None
_scan_guard = threading.Lock()
_scan_state: dict = {"status": "idle", "started_at": None, "finished_at": None, "error": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _run_background_screen() -> None:
    try:
        screen()
        with _scan_guard:
            _scan_state.update(status="done", finished_at=_now_iso(), error=None)
    except Exception as e:  # noqa: BLE001 — surfaced via status, never crashes the thread silently
        with _scan_guard:
            _scan_state.update(status="error", finished_at=_now_iso(), error=str(e))


def start_background_screen() -> dict:
    """Kick an on-demand rescan in a detached daemon thread and return the
    status immediately. Deduped: a concurrent call while one is already
    running just returns the current (running) status."""
    global _scan_thread
    with _scan_guard:
        if _scan_thread is not None and _scan_thread.is_alive():
            return dict(_scan_state, running=True)
        _scan_state.update(status="running", started_at=_now_iso(), finished_at=None, error=None)
        _scan_thread = threading.Thread(target=_run_background_screen,
                                        name="daytrade-screen-runner", daemon=True)
        _scan_thread.start()
        return dict(_scan_state, running=True)


def screen_status() -> dict:
    """Current on-demand-rescan state for the client to poll."""
    with _scan_guard:
        running = _scan_thread is not None and _scan_thread.is_alive()
        st = dict(_scan_state)
    st["running"] = running
    return st
