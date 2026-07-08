"""Tiered polling runtime — the impure orchestrator that runs one polling cycle.

This is the glue the scheduler daemon calls each market-hours tick. It wires the
pure decision layer (``market_scheduler``) to the transport, budget, staleness and
escalation machinery, keeping all provider I/O out of the pure code:

  1. build ``PortfolioState`` / ``QueueState`` (cheap, provider-free) and assign tiers;
  2. select the Tier 0/1 symbols whose quote cadence is due (``fetch_due``), applying
     the budget shed ladder (Tier 3/2 dropped, Tier 1 cadence stretched, Tier 0 never);
  3. fetch them — plus SPY and held sector ETFs — in ONE batched Schwab quote call;
  4. run defense escalation per Tier 0 position (levels derived from cached bars) and
     the global market escalation (SPY / held-sector intraday move);
  5. periodically recompute the kill-switch RS3M inputs intraday
     (``REFRESH_KILLSWITCH_PER_DAY`` times) by reusing the tested on-demand refresh.

Everything is best-effort and logged; a failure never breaks the scheduler tick.
Steady state during market hours is one batched quote call per interval, chains on
demand only, one EOD bar batch — the target in the spec.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import data_budget
import data_cache
import data_transport as transport
import market_scheduler as ms
import queue_state
from market_scheduler import QUOTE, EscalationTracker, ListAlertSink, Tier

logger = logging.getLogger("cfm.tierpoll")
ET = ZoneInfo("America/New_York")

# In-process cadence + escalation state (mirrors refresh_policy._last_refresh:
# process-local, resets on restart — the first post-restart cycle simply refreshes).
_last_quote_at: dict[str, datetime] = {}
_tracker = EscalationTracker(sink=ListAlertSink())
_killswitch_runs: dict = {"day": None, "count": 0, "last": None}


def reset() -> None:
    """Clear in-process cycle state (tests / demo-mode switch)."""
    global _tracker
    _last_quote_at.clear()
    _tracker = EscalationTracker(sink=ListAlertSink())
    _killswitch_runs.update(day=None, count=0, last=None)


def _now() -> datetime:
    return datetime.now(ET)


def _market_symbols(state: dict) -> set[str]:
    """SPY plus the parent sector ETF of every open position — the names whose
    intraday move can trigger a global market escalation."""
    import sector_data
    syms = {config.BENCHMARK.upper()}
    for pos in state.get("positions", []):
        if pos.get("status") == "closed":
            continue
        etf = pos.get("sector") or sector_data.sector_for(pos.get("ticker", ""))
        if etf:
            syms.add(etf.upper())
    return syms


def _escalation_flags(now: datetime):
    """Snapshot for fetch_due: True (all-escalated) under a global market
    escalation, else the set of symbols with an active defense escalation."""
    if _tracker.market_active(now):
        return True
    return _tracker.escalated_symbols(now)


def _due_quotes(tiers: dict[str, Tier], market_open: bool, now: datetime) -> dict[str, Tier]:
    """The Tier 0/1 symbols due for a quote this cycle, after the shed ladder."""
    flags = _escalation_flags(now)
    t1_mult = data_budget.t1_cadence_multiplier("schwab")
    due: dict[str, Tier] = {}
    for sym, tier in tiers.items():
        if tier not in (Tier.T0, Tier.T1):
            continue
        last = _last_quote_at.get(sym)
        if not ms.fetch_due(sym, tier, QUOTE, market_open, last, flags, now):
            continue
        # Budget shed: Tier 1 cadence is stretched under deep pressure; Tier 0 is
        # never slowed. An escalated symbol ignores the stretch (freshness wins).
        if tier == Tier.T1 and t1_mult > 1.0 and last is not None \
                and not ms._is_escalated(flags, sym):
            if (now - last).total_seconds() < config.POLL_T1_SECONDS * t1_mult:
                continue
        due[sym] = tier
    return due


def _run_defense_escalations(state: dict, tiers: dict, quotes: dict, now: datetime) -> list[str]:
    import data_handler
    fired: list[str] = []
    for pos in state.get("positions", []):
        if pos.get("status") == "closed":
            continue
        sym = (pos.get("ticker") or "").upper()
        if tiers.get(sym) != Tier.T0:
            continue
        q = quotes.get(sym)
        if not q or q.get("price") is None:
            continue
        try:
            bars = data_handler.get_daily(sym)
            levels = transport.defense_levels(pos, bars)
            for alert in _tracker.observe_defense(sym, levels, float(q["price"]), now):
                fired.append(alert.detail)
        except Exception as e:  # noqa: BLE001 — one position must not break the cycle
            logger.warning("defense escalation check failed for %s: %s", sym, e)
    return fired


def _run_market_escalation(state: dict, quotes: dict, now: datetime):
    import data_handler
    moves: dict[str, float] = {}
    for sym in _market_symbols(state):
        q = quotes.get(sym)
        if not q or q.get("price") is None:
            continue
        try:
            bars = data_handler.get_daily(sym)
            moves[sym] = transport.intraday_move_pct(float(q["price"]), bars)
        except Exception as e:  # noqa: BLE001
            logger.warning("market-move check failed for %s: %s", sym, e)
    alert = _tracker.observe_market(moves, now) if moves else None
    return alert


def _maybe_killswitch_refresh(state: dict, now: datetime) -> bool:
    """Recompute the kill-switch RS3M inputs (RS3M vs SPY / vs Sector) intraday, up
    to REFRESH_KILLSWITCH_PER_DAY times spaced across the session, by reusing the
    tested on-demand refresh (which overlays live quotes and rescoring). Kill-switch
    inputs must not be stale end-of-day-only values."""
    import maintenance
    import refresh_policy
    day = now.date()
    if _killswitch_runs["day"] != day:
        _killswitch_runs.update(day=day, count=0, last=None)
    n = max(1, config.REFRESH_KILLSWITCH_PER_DAY)
    if _killswitch_runs["count"] >= n:
        return False
    # Evenly space the N runs across a ~6.5h session.
    interval = (6.5 * 3600.0) / n
    last = _killswitch_runs["last"]
    if last is not None and (now - last).total_seconds() < interval:
        return False
    open_tickers = maintenance.open_tickers(state)
    if not open_tickers:
        return False
    try:
        refresh_policy.refresh_tickers(open_tickers)
        _killswitch_runs["count"] += 1
        _killswitch_runs["last"] = now
        logger.info("kill-switch RS3M refresh %d/%d (%d names)",
                    _killswitch_runs["count"], n, len(open_tickers))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("kill-switch RS3M refresh failed: %s", e)
        return False


def run_cycle(now: datetime | None = None, sleep=time.sleep) -> dict | None:
    """Run one polling cycle. Returns a summary (or None in demo mode / off-hours).
    Best-effort: exceptions are logged, never raised to the caller."""
    now = now or _now()
    if config.demo_enabled():
        return None
    market_open = ms.is_market_open(now)

    try:
        state = _load_state()
        ps, qs = queue_state.build(state)
        tiers = ms.assign_tiers(ps, qs, now)
    except Exception as e:  # noqa: BLE001
        logger.warning("tier assignment failed: %s", e)
        return None

    # Off-hours: no quote polling (quotes drop to zero); the EOD batch + kill-switch
    # cadence are handled by the scheduler's existing EOD/warm paths.
    if not market_open:
        return {"market_open": False, "due": [], "quotes": {}}

    # Poll set = due Tier 0/1 symbols + the market-escalation names (SPY, held
    # sectors), all in ONE batch. Market names ride Tier 1 cadence for budgeting.
    poll_tiers = dict(tiers)
    for m in _market_symbols(state):
        poll_tiers.setdefault(m, Tier.T1)
    due = _due_quotes(poll_tiers, market_open, now)

    summary = {"market_open": True, "due": sorted(due), "quotes": {},
               "escalations": [], "market_escalation": None, "degraded": [],
               "killswitch_refreshed": False}
    if due:
        try:
            fetched = transport.fetch_quotes_batched(due, sleep=sleep)
            summary["quotes"] = fetched["quotes"]
            summary["degraded"] = fetched["degraded"]
            for sym in due:
                _last_quote_at[sym] = now
            summary["escalations"] = _run_defense_escalations(state, tiers, fetched["quotes"], now)
            alert = _run_market_escalation(state, fetched["quotes"], now)
            summary["market_escalation"] = alert.detail if alert else None
        except Exception as e:  # noqa: BLE001
            logger.warning("quote cycle failed: %s", e)

    summary["killswitch_refreshed"] = _maybe_killswitch_refresh(state, now)
    return summary


def _load_state():
    import logging_handler as log
    return log.load_state()


def status(now: datetime | None = None) -> dict:
    """Tier-poll summary for the data-health panel: escalations active, last quote
    times, kill-switch refresh progress."""
    now = now or _now()
    return {
        "escalated_symbols": sorted(_tracker.escalated_symbols(now)),
        "market_escalation_active": _tracker.market_active(now),
        "polled_symbols": len(_last_quote_at),
        "killswitch_runs_today": _killswitch_runs["count"] if _killswitch_runs["day"] == now.date() else 0,
        "killswitch_target": config.REFRESH_KILLSWITCH_PER_DAY,
    }


def recent_alerts(limit: int = 20) -> list[dict]:
    """Drain-safe view of the most recent escalation alerts for the UI."""
    sink = _tracker.sink
    alerts = getattr(sink, "alerts", [])
    return [{"kind": a.kind, "symbol": a.symbol, "level": a.level,
             "detail": a.detail, "at": a.at} for a in alerts[-limit:]]
