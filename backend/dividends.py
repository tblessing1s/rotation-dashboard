"""Per-ticker dividend yield, for dividend-adjusted option greeks.

A call holder forgoes the underlying's dividends, so a dividend yield q lowers
the call's delta (delta = e^(−qT)·N(d1)). The effect scales with time, so it's
negligible on the weekly short but ~1–1.5% on a 171-DTE LEAP for a ~3% payer —
enough to nudge a strike across the LEAP delta band.

Yields come from Schwab fundamentals (`divYield`, a percent) first, then Alpha
Vantage's OVERVIEW (`DividendYield`, a decimal). Results are day-cached in
DATA_DIR/dividends_cache.json so the option-chain route stays cheap. A manual
override in state.json metadata (``dividend_overrides: {TICKER: 0.03}``) always
wins; values > 1 are read as percent (3 → 0.03). All values returned as a decimal
yield; unknown → 0.0 (i.e. treat as non-paying, the safe no-op).
"""
from __future__ import annotations

import json
import os
import threading
import time

import alpha_vantage
import config
import dividend_calendar
import logging_handler as log
import schwab_api

_CACHE_FILE = os.path.join(config.DATA_DIR, "dividends_cache.json")
_TTL_SECONDS = 24 * 3600
_lock = threading.Lock()


def _normalize(value) -> float | None:
    """Coerce a yield to a sane decimal. Accepts decimals (0.03), percents (3 →
    0.03), and provider junk ('None', '-', '')."""
    try:
        q = float(value)
    except (TypeError, ValueError):
        return None
    if q != q or q < 0:  # NaN / negative
        return None
    if q > 1.0:  # given as a percent
        q /= 100.0
    return q if q < 1.0 else None  # a yield ≥ 100% is bad data


# Parsed-cache memo, keyed on the cache file's mtime+size (the same invalidation
# trick data_handler uses for frames). A full-universe sweep asks for hundreds of
# tickers; re-opening and re-parsing the whole JSON per ticker was the dominant
# cost of the cache-only read path.
_parsed: tuple[tuple[float, int], dict] | None = None


def _read_cache() -> dict:
    global _parsed
    try:
        stat = os.stat(_CACHE_FILE)
        stamp = (stat.st_mtime, stat.st_size)
    except OSError:
        return {}
    if _parsed is not None and _parsed[0] == stamp:
        return _parsed[1]
    try:
        with open(_CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (FileNotFoundError, ValueError):
        return {}
    _parsed = (stamp, data)
    return data


def _write_cache(data: dict) -> None:
    global _parsed
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _CACHE_FILE)
    _parsed = None  # force a re-read; the mtime stamp is now stale


def _metadata(state: dict | None = None) -> dict:
    """State metadata, from an already-loaded state when the caller has one.

    ``state`` matters: a bulk caller (a ~500-name scan sweep) passes the state it
    already holds instead of triggering a full ``state.json`` load — which parses
    the entire execution log and runs the migration chain — once per ticker."""
    if state is not None:
        return state.get("metadata") or {}
    try:
        return log.load_state().get("metadata", {})
    except Exception:  # noqa: BLE001
        return {}


def _override(ticker: str, state: dict | None = None) -> float | None:
    overrides = _metadata(state).get("dividend_overrides") or {}
    raw = overrides.get(ticker) or overrides.get(ticker.upper())
    return _normalize(raw) if raw is not None else None


def _fetch_yield(ticker: str) -> float | None:
    """Provider dividend yield as a decimal, or None if unavailable."""
    if schwab_api.configured():
        try:
            import data_handler
            fund = data_handler.client().get_instrument_fundamental(ticker)
            q = _normalize(fund.get("divYield"))  # Schwab reports a percent
            if q is not None:
                return q
        except Exception:  # noqa: BLE001 — degrade to the next source
            pass
    if alpha_vantage.configured():
        try:
            q = _normalize(alpha_vantage.overview(ticker).get("DividendYield"))  # decimal
            if q is not None:
                return q
        except Exception:  # noqa: BLE001
            pass
    return None


def yield_for(ticker: str, refresh: bool = False) -> float:
    """Continuous dividend yield (decimal) for a ticker; 0.0 when unknown.

    Resolution: manual override -> day-cached provider value -> live fetch.
    Never raises — option greeks must not break on a fundamentals hiccup.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return 0.0

    override = _override(ticker)
    if override is not None:
        return override

    with _lock:
        cache = _read_cache()
        rec = cache.get(ticker)
        fresh = rec and (time.time() - float(rec.get("fetched_at") or 0) < _TTL_SECONDS)
        if refresh or not fresh:
            rec = {"yield": _fetch_yield(ticker), "fetched_at": time.time()}
            cache[ticker] = rec
            _write_cache(cache)
    q = rec.get("yield")
    return q if isinstance(q, (int, float)) and q >= 0 else 0.0


def cached_yield_with_source(ticker: str, state: dict | None = None) -> tuple[float | None, str]:
    """(annual yield as a decimal, source) read from CACHE ONLY — never a fetch.

    Mirrors ``cached_dividend``: a bulk scan sweeps hundreds of names, and calling
    ``yield_for`` across them on a cold cache would fire one provider request per
    ticker on the request path. Returns ``(None, "unknown")`` when nothing is
    cached, which callers must keep DISTINCT from a genuine non-payer — an
    unresolved yield is never displayed as a confident 0 and never auto-enrols a
    name into the dividend sleeve.

    Manual overrides in state metadata still win, exactly as ``yield_for``
    resolves them; pass ``state`` from a bulk caller so the override lookup does
    not re-load ``state.json`` per ticker.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None, "unknown"
    override = _override(ticker, state)
    if override is not None:
        return override, "override"
    with _lock:
        rec = _read_cache().get(ticker)
    if not rec or "yield" not in rec:
        return None, "unknown"
    q = rec.get("yield")
    if not isinstance(q, (int, float)) or q < 0:
        return None, "none"          # cached miss — a resolved non-payer/unknown
    return (q, "dividend_yield") if q > 0 else (0.0, "none")


def cached_annual_yield_pct(ticker: str, state: dict | None = None) -> float | None:
    """Trailing annual dividend yield in PERCENT from cache only, or None when it
    is unresolved. Never raises, never fetches.

    The one place the decimal->percent conversion and the "unknown is not zero"
    rule live: the entry gate, the scan row and the profile heuristic all read
    through here, so they can never disagree about whether a name pays a dividend.
    """
    try:
        q, _src = cached_yield_with_source(ticker, state)
    except Exception:  # noqa: BLE001 — a fundamentals hiccup is an unknown, not a 0
        return None
    return None if q is None else round(q * 100, 4)


def q_with_source(ticker: str, refresh: bool = False) -> tuple[float, str]:
    """(q, source) — the continuous dividend yield AND where it came from, so a
    risk path can LOG the provenance instead of silently defaulting q to 0.

    source is "dividend_yield" when a real (>0) yield resolved (override or cached
    provider value via the continuous trailing-12m ÷ spot approximation), or
    "none" when unknown / a genuine non-payer — in which case q is 0.0, the
    explicit fallback [Q_FALLBACK]. Never raises, never makes a NEW network call
    beyond what yield_for already does (providers gated on configured())."""
    q = yield_for(ticker, refresh=refresh)
    return (q, "dividend_yield") if q and q > 0 else (0.0, "none")


def cache_health() -> dict:
    """Staleness summary of the dividends cache for the data-health panel."""
    from datetime import datetime
    cache = _read_cache()
    stamps = [float(r.get("fetched_at") or 0)
              for r in list(cache.values()) + list((cache.get("events") or {}).values())
              if isinstance(r, dict) and r.get("fetched_at")]
    return {
        "entries": len(stamps),
        "oldest_fetched_at": (datetime.fromtimestamp(min(stamps)).strftime("%Y-%m-%dT%H:%M:%S")
                              if stamps else None),
        "newest_fetched_at": (datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%dT%H:%M:%S")
                              if stamps else None),
    }


# ---------------------------------------------------------------------------
# Next dividend EVENT (ex-date + per-payment amount) — assignment-risk input.
# ---------------------------------------------------------------------------
# The annual->per-payment conversion the assignment guard's UNITS depend on lives
# in ONE place (dividend_calendar), so the Schwab probe below and the calendar's
# adapters can never drift apart on it.
_parse_amount = dividend_calendar.per_payment_amount


def _fetch_event(ticker: str) -> dict:
    """Best-effort {ex_date, amount} from Schwab fundamentals, then Alpha
    Vantage OVERVIEW. Field names vary by provider vintage, so several
    candidates are tried; amount falls back to annual/frequency."""
    if schwab_api.configured():
        try:
            import data_handler
            fund = data_handler.client().get_instrument_fundamental(ticker)
            # LIVE_VERIFY — none of these ex-div key names is confirmed against a
            # live Schwab fundamental payload; they are best-effort candidates. The
            # whole covered-call assignment-risk surface depends on one resolving.
            # Dump a live fundamental for a known payer (KO/JNJ) and confirm which
            # key exists + its units before trusting it (see migration audit §6).
            ex = next((str(fund[k])[:10] for k in
                       ("nextDivExDate", "divExDate", "dividendDate", "divDate")
                       if fund.get(k)), None)
            amount = _parse_amount(fund.get("divPayAmount") or fund.get("divAmount"),
                                   fund.get("divFreq"))
            # divPayAmount is already per-payment on newer payloads; prefer it raw.
            try:
                pay = float(fund.get("divPayAmount"))
                if pay == pay and 0 < pay:
                    amount = round(pay, 4)
            except (TypeError, ValueError):
                pass
            if ex or amount:
                return {"ex_date": ex, "amount": amount, "source": "schwab"}
        except Exception:  # noqa: BLE001 — degrade to the next source
            pass
    # Everything else resolves through the contract-checked calendar
    # (dividend_calendar), which owns the shape, the UNITS (per-share, per-payment
    # — never annual, never a yield) and the never-invent-a-date rule. The Schwab
    # probe above stays as a pre-step because it is the only place those unverified
    # key names are still tried; the calendar's schwab_adapter is a documented TODO
    # rather than the same guess repeated.
    event = dividend_calendar.next_dividend(ticker)
    if not dividend_calendar.is_empty(event):
        return {"ex_date": event["ex_date"], "amount": event["amount"],
                "pay_date": event.get("pay_date"), "source": event["source"]}
    return {"ex_date": None, "amount": None, "source": "none"}


def next_dividend(ticker: str, refresh: bool = False) -> dict:
    """Next dividend event for a ticker: {ex_date, amount, source}.

    Resolution: manual override in state metadata (``dividend_event_overrides:
    {TICKER: {ex_date, amount}}``) -> day-cached provider value -> live fetch.
    Unknown fields come back None (treated as "no dividend risk").
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ex_date": None, "amount": None, "source": "none"}
    # Operator overrides resolve THROUGH the calendar contract, so a hand-entered
    # ex-date or amount gets the same unit checks and sentinel-date rejection every
    # provider value does — previously they were returned raw, unvalidated.
    ov = dividend_calendar.next_dividend(
        ticker, fixtures=_metadata().get("dividend_event_overrides"))
    if not dividend_calendar.is_empty(ov):
        return {"ex_date": ov["ex_date"], "amount": ov["amount"],
                "pay_date": ov.get("pay_date"), "source": "override"}
    with _lock:
        cache = _read_cache()
        rec = (cache.get("events") or {}).get(ticker)
        fresh = rec and (time.time() - float(rec.get("fetched_at") or 0) < _TTL_SECONDS)
        if refresh or not fresh:
            rec = {**_fetch_event(ticker), "fetched_at": time.time()}
            cache.setdefault("events", {})[ticker] = rec
            _write_cache(cache)
    return {"ex_date": rec.get("ex_date"), "amount": rec.get("amount"),
            "source": rec.get("source", "none")}


def cached_dividend(ticker: str) -> dict:
    """Cache-only next-dividend read — NEVER triggers a live fetch. Mirrors
    earnings.cached_earnings so a bulk scan over many payers can't cause a fetch
    storm. Returns the day-cached value with a ``stale`` flag (True when the cached
    record is older than the TTL, or when nothing is cached). Manual overrides in
    state metadata still win, exactly as next_dividend resolves them."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ex_date": None, "amount": None, "source": "none", "stale": True}
    ov = dividend_calendar.next_dividend(
        ticker, fixtures=_metadata().get("dividend_event_overrides"))
    if not dividend_calendar.is_empty(ov):
        return {"ex_date": ov["ex_date"], "amount": ov["amount"],
                "source": "override", "stale": False}
    with _lock:
        rec = (_read_cache().get("events") or {}).get(ticker)
    if not rec:
        return {"ex_date": None, "amount": None, "source": "none", "stale": True}
    stale = (time.time() - float(rec.get("fetched_at") or 0)) >= _TTL_SECONDS
    return {"ex_date": rec.get("ex_date"), "amount": rec.get("amount"),
            "source": rec.get("source", "none"), "stale": stale}
