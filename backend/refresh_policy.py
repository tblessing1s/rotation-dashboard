"""Smart intraday refresh tiering.

The whole ticker universe (~500 names) is fetched once pre-open and then frozen
in the parquet cache for the trading day (see data_handler / screening.warm_scan_
cache). That's the right cadence for the long tail, but a handful of names carry
live risk and should stay current *intraday*:

  * open positions          — you're managing LEAPs/shorts on these right now;
  * entry candidates (GO)    — about to be traded, so their price/RS must be live;
  * earnings-imminent names  — a report within EARNINGS_WARN_DAYS moves the stock.

This module selects that small "hot" set from signals the app already computes —
open positions from state.json and the GO verdict / earnings date off the last
memoized scorecard sweep — so picking it costs no provider calls. The scheduler
force-refreshes the hot set on the HOT_REFRESH_MINUTES cadence during market
hours (alert_scheduler._maybe_hot_refresh); everything else rides the daily
pre-open warm-up. The set is bounded by HOT_TICKERS_MAX, and open positions are
never dropped from it (live risk outranks a long candidate tail).
"""
from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
import data_handler
import logging_handler as log
import maintenance
import screening

ET = ZoneInfo("America/New_York")

# Timestamp (a tz-aware ET datetime) of the last hot refresh. Kept here so
# status() can report it without reaching into the scheduler, and so the
# scheduler tick and the on-demand endpoint share one consistent clock.
_last_refresh = None


def _now_et() -> datetime:
    return datetime.now(ET)


def enabled() -> bool:
    """Intraday hot refresh on by default; CFM_HOT_REFRESH=0 turns it off."""
    return os.environ.get("CFM_HOT_REFRESH", "1").strip() not in ("0", "false", "no")


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _today() -> date:
    return _parse_date(log.utcnow()[:10]) or date.min


def _candidate_rows() -> list[dict]:
    """GO/earnings candidate pool from the last memoized scorecard — a cheap peek
    that never triggers a fresh sweep, and ignores an overnight-stale memo."""
    sc = screening.peek_cached("scorecard:full", max_age=config.HOT_CANDIDATE_MAX_AGE)
    return (sc or {}).get("results", []) if isinstance(sc, dict) else []


def _earnings_imminent(rows: list[dict], today: date) -> list[str]:
    """Candidate tickers whose next earnings report is within the warn window —
    read straight off the scorecard rows (they already carry earnings_date), so
    no earnings-cache reads on the hot path."""
    out = []
    for r in rows:
        d = _parse_date(r.get("earnings_date"))
        if d and 0 <= (d - today).days <= config.EARNINGS_WARN_DAYS:
            t = (r.get("ticker") or "").strip().upper()
            if t:
                out.append(t)
    return out


def hot_tickers(state: dict | None = None) -> list[str]:
    """The intraday-refresh set, priority-ordered so truncation drops the least
    important first: open positions (never dropped) → earnings-imminent
    candidates → GO candidates. Deduped and capped at HOT_TICKERS_MAX (but the
    cap always leaves room for every open position)."""
    state = state if state is not None else log.load_state()
    seen: set[str] = set()
    ordered: list[str] = []

    def add(ticker: str | None) -> None:
        t = (ticker or "").strip().upper()
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)

    # Tier 1 — open positions. Live risk; always included, never truncated.
    for t in maintenance.open_tickers(state):
        add(t)
    n_positions = len(ordered)

    rows = _candidate_rows()
    today = _today()
    # Tier 2 — earnings-imminent candidates (a report within the warn window).
    for t in _earnings_imminent(rows, today):
        add(t)
    # Tier 3 — entry candidates the scorecard verdicts GO.
    for r in rows:
        if r.get("verdict") == "GO":
            add(r.get("ticker"))

    cap = max(config.HOT_TICKERS_MAX, n_positions)
    return ordered[:cap]


def refresh_hot(state: dict | None = None) -> dict:
    """Force-refresh the hot set's daily bars (one parallel batch, bypassing the
    freshness window). Returns the set that was refreshed. In demo mode this is a
    cheap no-op — get_daily is cache-only there and ignores force."""
    hot = hot_tickers(state)
    if hot:
        data_handler.prefetch(hot, force=True)
    return {"tickers": hot, "count": len(hot)}


def refresh_tickers(tickers: list[str]) -> dict:
    """Force-refresh a SPECIFIC set of tickers' daily bars now (bypassing the
    freshness window), then return their freshly-computed scorecard rows.

    The on-demand "this quote is stale, pull it live" path for names OUTSIDE the
    hot set — which otherwise ride the daily pre-open warm-up and so read stale
    intraday. Re-fetching the daily bars is the same mechanism the hot-set
    refresh uses: Schwab's daily history carries the current session's forming
    candle during market hours, so the last close reads ~live and every derived
    metric in the row (RS, MA distances, verdict) updates together — not just an
    overlaid price. SPY and each ticker's sector ETF are force-refreshed
    alongside, so relative strength is measured against equally-fresh benchmarks.
    In demo mode get_daily is cache-only and force is a no-op, so this simply
    recomputes from the synthetic cache."""
    import sector_data
    names = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
    if not names:
        return {"tickers": [], "rows": [], "count": 0, "as_of": None}
    etfs = sorted({e for e in (sector_data.sector_for(t) for t in names) if e})
    force_set = list(dict.fromkeys(names + [config.BENCHMARK] + etfs))
    data_handler.prefetch(force_set, force=True)
    from metrics import scorecard as scorecard_metrics
    sc = scorecard_metrics.scorecard(names)  # explicit list => computes fresh, no memo
    return {"tickers": names, "rows": sc.get("results", []),
            "count": len(names), "as_of": sc.get("as_of")}


def maybe_refresh_hot(now: datetime | None = None, force: bool = False) -> dict | None:
    """Refresh the hot set if the cadence has elapsed since the last one. ``now``
    defaults to the current ET time; the scheduler passes its own ET clock so the
    two paths stay tz-consistent. Returns the refresh result, or None when it was
    skipped as too soon. ``force`` bypasses the gate (on-demand endpoint / tests)."""
    global _last_refresh
    if now is None:
        now = _now_et()
    if not force and _last_refresh is not None:
        if (now - _last_refresh).total_seconds() < config.HOT_REFRESH_MINUTES * 60:
            return None
    _last_refresh = now
    return refresh_hot()


def status() -> dict:
    """Hot-refresh summary for the data-health panel: whether it's enabled, the
    cadence, when it last ran, and the current hot set."""
    hot = hot_tickers()
    return {
        "enabled": enabled(),
        "cadence_minutes": config.HOT_REFRESH_MINUTES,
        "max_tickers": config.HOT_TICKERS_MAX,
        "last_refresh": _last_refresh.strftime("%Y-%m-%dT%H:%M:%S") if _last_refresh else None,
        "count": len(hot),
        "tickers": hot,
    }
