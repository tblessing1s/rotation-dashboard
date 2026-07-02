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


def _override(ticker: str) -> float | None:
    try:
        meta = log.load_state().get("metadata", {})
    except Exception:  # noqa: BLE001
        return None
    overrides = meta.get("dividend_overrides") or {}
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


# ---------------------------------------------------------------------------
# Next dividend EVENT (ex-date + per-payment amount) — assignment-risk input.
# ---------------------------------------------------------------------------
def _parse_amount(annual, freq) -> float | None:
    """Per-payment dividend from an annual amount and payment frequency
    (defaults to quarterly when the provider omits the frequency)."""
    try:
        a = float(annual)
    except (TypeError, ValueError):
        return None
    if a != a or a <= 0:
        return None
    try:
        f = int(freq) or 4
    except (TypeError, ValueError):
        f = 4
    return round(a / f, 4)


def _fetch_event(ticker: str) -> dict:
    """Best-effort {ex_date, amount} from Schwab fundamentals, then Alpha
    Vantage OVERVIEW. Field names vary by provider vintage, so several
    candidates are tried; amount falls back to annual/frequency."""
    if schwab_api.configured():
        try:
            import data_handler
            fund = data_handler.client().get_instrument_fundamental(ticker)
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
    if alpha_vantage.configured():
        try:
            ov = alpha_vantage.overview(ticker)
            ex = str(ov.get("ExDividendDate") or "")[:10] or None
            if ex in ("None", "0000-00-00"):
                ex = None
            amount = _parse_amount(ov.get("DividendPerShare"), 4)  # annual, quarterly payer
            if ex or amount:
                return {"ex_date": ex, "amount": amount, "source": "alpha_vantage"}
        except Exception:  # noqa: BLE001
            pass
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
    try:
        meta = log.load_state().get("metadata", {})
        ov = (meta.get("dividend_event_overrides") or {}).get(ticker)
        if ov:
            return {"ex_date": ov.get("ex_date"), "amount": ov.get("amount"),
                    "source": "override"}
    except Exception:  # noqa: BLE001
        pass
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
