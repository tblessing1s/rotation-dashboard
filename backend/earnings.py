"""Next-earnings tracking for CFM positions.

Earnings approach matters for this strategy: into a report we either roll the
short deep-ITM for protection or close the position entirely, so the next report
date is surfaced on every open position (tracker + kill switch + checklist).

Dates come from Alpha Vantage's EARNINGS_CALENDAR, cross-checked against Schwab
fundamentals when that endpoint exposes a next-earnings field (used to fill a
blank AV date and to flag a conflict when the two disagree). Free-tier calendars
are often wrong or late-updated and fail silently, so every record also carries
its last-refresh time and a ``stale`` flag; the EARNINGS_DATE_STALE alert makes a
held name's un-refreshed (or conflicting) date audible. Results are cached per
ticker for a day in DATA_DIR/earnings_cache.json so the position/kill-switch
routes stay cheap and we stay inside Alpha Vantage's request budget. A manual
override in state.json metadata (``earnings_overrides: {TICKER: "YYYY-MM-DD"}``)
always wins, so a date can be set by hand when the provider has none.
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


def _fetch_av_date(ticker: str) -> str | None:
    """Soonest Alpha Vantage report date that is today or later, or None."""
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


# Schwab's fundamental block has no documented next-earnings field, but some
# vintages expose one under a handful of names — probe them best-effort so we can
# cross-check Alpha Vantage "when available" (the safe no-op is None).
_SCHWAB_EARNINGS_KEYS = ("nextEarningsDate", "earningsDate", "nextEarningsReportDate",
                         "earningsAnnouncementDate", "nextReportDate")


def _fetch_schwab_date(ticker: str) -> str | None:
    """Best-effort next-earnings date from Schwab fundamentals, today or later,
    or None when Schwab isn't connected / exposes no such field."""
    import schwab_api
    if not schwab_api.configured():
        return None
    try:
        import data_handler
        fund = data_handler.client().get_instrument_fundamental(ticker)
    except Exception:  # noqa: BLE001 — a fundamentals hiccup never breaks the lookup
        return None
    today = _today()
    for k in _SCHWAB_EARNINGS_KEYS:
        d = _parse_date(fund.get(k))
        if d and d >= today:
            return d.isoformat()
    return None


def _fetch_combined(ticker: str) -> dict:
    """Alpha Vantage (primary) cross-checked against Schwab fundamentals. Fills
    from Schwab when AV is blank; flags a conflict when both are present and
    disagree by more than EARNINGS_CONFLICT_DAYS."""
    av = _fetch_av_date(ticker)
    schwab = _fetch_schwab_date(ticker)
    chosen = av or schwab
    source = "alpha_vantage" if av else ("schwab" if schwab else "alpha_vantage")
    conflict = False
    if av and schwab:
        da, ds = _parse_date(av), _parse_date(schwab)
        if da and ds and abs((da - ds).days) > config.EARNINGS_CONFLICT_DAYS:
            conflict = True
    return {"date": chosen, "av_date": av, "schwab_date": schwab,
            "source": source, "conflict": conflict}


def _is_stale(fetched_at, source: str) -> bool:
    """A cached earnings date is stale when its last successful refresh is older
    than EARNINGS_STALE_DAYS. Overrides never go stale (hand-set); an unknown
    fetch time can't be judged (not stale)."""
    if source == "override" or not fetched_at:
        return False
    try:
        return (time.time() - float(fetched_at)) > config.EARNINGS_STALE_DAYS * 86400
    except (TypeError, ValueError):
        return False


def _summary(ticker: str, date_str: str | None, source: str, rec: dict | None = None) -> dict:
    rec = rec or {}
    fetched_at = rec.get("fetched_at")
    d = _parse_date(date_str)
    out = {
        "ticker": ticker,
        "date": d.isoformat() if d else None,
        "days_until": (d - _today()).days if d else None,
        "warning": bool(d) and 0 <= (d - _today()).days <= config.EARNINGS_WARN_DAYS,
        "source": source,
        # Cross-check + staleness surfacing (silent-failure guardrail).
        "fetched_at": (datetime.fromtimestamp(float(fetched_at)).strftime("%Y-%m-%dT%H:%M:%S")
                       if fetched_at else None),
        "stale": _is_stale(fetched_at, source),
        "conflict": bool(rec.get("conflict")),
        "av_date": rec.get("av_date"),
        "schwab_date": rec.get("schwab_date"),
    }
    return out


def cache_health() -> dict:
    """Staleness summary of the earnings cache for the data-health panel."""
    cache = _read_cache()
    recs = [r for r in cache.values() if isinstance(r, dict)]
    stamps = [float(r.get("fetched_at") or 0) for r in recs if r.get("fetched_at")]
    return {
        "entries": len(recs),
        "oldest_fetched_at": (datetime.fromtimestamp(min(stamps)).strftime("%Y-%m-%dT%H:%M:%S")
                              if stamps else None),
        "newest_fetched_at": (datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%dT%H:%M:%S")
                              if stamps else None),
        "stale_entries": sum(1 for r in recs
                             if _is_stale(r.get("fetched_at"), r.get("source", "cache"))),
        "conflicts": sum(1 for r in recs if r.get("conflict")),
    }


def cached_earnings(ticker: str) -> dict:
    """Cache/override-only earnings lookup — NEVER hits a provider. Used by
    bulk paths (the Scorecard sweeps hundreds of tickers) where a cold-cache
    fetch storm would blow the Alpha Vantage budget; unknowns stay None. Carries
    the staleness + cross-check fields so the guardrail can flag a date that
    hasn't refreshed."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _summary(ticker, None, "none")
    override = _override(ticker)
    if override:
        return _summary(ticker, override, "override")
    rec = _read_cache().get(ticker) or {}
    return _summary(ticker, rec.get("date"), "cache" if rec else "none", rec=rec)


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
            rec = {**_fetch_combined(ticker), "fetched_at": time.time()}
            cache[ticker] = rec
            _write_cache(cache)
    return _summary(ticker, rec.get("date"), rec.get("source", "alpha_vantage"), rec=rec)
