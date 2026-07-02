"""Next-earnings tracking for CFM positions.

Earnings approach matters for this strategy: into a report we either roll the
short deep-ITM for protection or close the position entirely, so the next report
date is surfaced on every open position (tracker + kill switch + checklist).

Dates come from Alpha Vantage's EARNINGS_CALENDAR — Schwab exposes no reliable
next-earnings field. Results are cached per ticker for a day in
DATA_DIR/earnings_cache.json so the position/kill-switch routes stay cheap and we
stay inside Alpha Vantage's request budget. A manual override in state.json
metadata (``earnings_overrides: {TICKER: "YYYY-MM-DD"}``) always wins, so a date
can be set by hand when the provider has none.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime

import alpha_vantage
import config
import logging_handler as log

_CACHE_FILE = os.path.join(config.DATA_DIR, "earnings_cache.json")
_TTL_SECONDS = 24 * 3600
_lock = threading.Lock()


def _today() -> date:
    return date.today()


def _parse_date(value) -> date | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _read_cache() -> dict:
    try:
        with open(_CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_cache(data: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _CACHE_FILE)


def _override(ticker: str) -> str | None:
    """A hand-entered date in state metadata always wins over the provider."""
    try:
        meta = log.load_state().get("metadata", {})
    except Exception:  # noqa: BLE001 — never let state issues break a read
        return None
    overrides = meta.get("earnings_overrides") or {}
    return overrides.get(ticker) or overrides.get(ticker.upper()) or None


def _fetch_date(ticker: str) -> str | None:
    """Soonest report date that is today or later, or None if unavailable."""
    if not alpha_vantage.configured():
        return None
    try:
        rows = alpha_vantage.earnings_calendar(ticker)
    except Exception:  # noqa: BLE001 — degrade to "unknown" on any provider error
        return None
    today = _today()
    upcoming = sorted(
        d for d in (_parse_date(r.get("reportDate")) for r in rows) if d and d >= today
    )
    return upcoming[0].isoformat() if upcoming else None


def _summary(ticker: str, date_str: str | None, source: str) -> dict:
    d = _parse_date(date_str)
    if not d:
        return {"ticker": ticker, "date": None, "days_until": None,
                "warning": False, "source": source}
    days = (d - _today()).days
    return {
        "ticker": ticker,
        "date": d.isoformat(),
        "days_until": days,
        "warning": 0 <= days <= config.EARNINGS_WARN_DAYS,
        "source": source,
    }


def cache_health() -> dict:
    """Staleness summary of the earnings cache for the data-health panel."""
    cache = _read_cache()
    stamps = [float(r.get("fetched_at") or 0) for r in cache.values()
              if isinstance(r, dict)]
    return {
        "entries": len(stamps),
        "oldest_fetched_at": (datetime.fromtimestamp(min(stamps)).strftime("%Y-%m-%dT%H:%M:%S")
                              if stamps else None),
        "newest_fetched_at": (datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%dT%H:%M:%S")
                              if stamps else None),
    }


def cached_earnings(ticker: str) -> dict:
    """Cache/override-only earnings lookup — NEVER hits a provider. Used by
    bulk paths (the Scorecard sweeps hundreds of tickers) where a cold-cache
    fetch storm would blow the Alpha Vantage budget; unknowns stay None."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _summary(ticker, None, "none")
    override = _override(ticker)
    if override:
        return _summary(ticker, override, "override")
    rec = _read_cache().get(ticker) or {}
    return _summary(ticker, rec.get("date"), "cache" if rec else "none")


def next_earnings(ticker: str, refresh: bool = False) -> dict:
    """Next earnings date for a ticker.

    Resolution order: manual override -> day-cached provider value -> live fetch.
    Always returns a dict; ``date`` is None when nothing is known.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ticker": ticker, "date": None, "days_until": None,
                "warning": False, "source": "none"}

    override = _override(ticker)
    if override:
        return _summary(ticker, override, "override")

    with _lock:
        cache = _read_cache()
        rec = cache.get(ticker)
        fresh = rec and (time.time() - float(rec.get("fetched_at") or 0) < _TTL_SECONDS)
        if refresh or not fresh:
            rec = {"date": _fetch_date(ticker), "fetched_at": time.time()}
            cache[ticker] = rec
            _write_cache(cache)
    return _summary(ticker, rec.get("date"), "alpha_vantage")
