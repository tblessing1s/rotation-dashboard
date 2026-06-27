"""Option-chain viewer for CFM: auto-picks the deep-ITM LEAP strike (delta ~0.90,
closest to 180 DTE) and a regime-aware ATR-based weekly short strike, each with
live bid/ask/extrinsic, so the user can eyeball both before executing.

Chains come from Schwab only (Alpha Vantage has no usable options data) and are
cached for 5 minutes per ticker so repeated modal opens don't hammer the API.

Market regime sets the ATR multiplier on the weekly short strike:
    GREEN  -> 1.5x  (more juice, less protection)
    YELLOW -> 2.0x  (balanced protection)
    RED    -> entry blocked (RegimeBlocked is raised; the route returns 403)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import config
import data_handler
import indicators
import schwab_api
import screening

# ATR multiplier by regime. RED is intentionally absent — it blocks entry.
REGIME_ATR_MULT = {"green": 1.5, "yellow": 2.0}

_CHAIN_TTL = 300  # seconds — 5-minute per-ticker cache
_chain_cache: dict[str, tuple[float, dict]] = {}
_chain_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class RegimeBlocked(RuntimeError):
    """Raised when the market regime is RED — no entries are allowed."""


def _chain_lock(ticker: str) -> threading.Lock:
    with _locks_guard:
        return _chain_locks.setdefault(ticker, threading.Lock())


def _fetch_chain(ticker: str) -> dict:
    """Raw Schwab CALL chain spanning near-term through ~LEAP expirations, cached
    for 5 minutes per ticker. One lock per ticker collapses concurrent opens."""
    hit = _chain_cache.get(ticker)
    if hit and time.time() - hit[0] < _CHAIN_TTL:
        return hit[1]
    with _chain_lock(ticker):
        hit = _chain_cache.get(ticker)
        if hit and time.time() - hit[0] < _CHAIN_TTL:
            return hit[1]
        if not schwab_api.configured():
            raise schwab_api.SchwabError(
                "Schwab is not connected — re-authorize at /auth/schwab to load option chains")
        today = datetime.now()
        to_date = (today + timedelta(days=config.LEAP_TARGET_DTE + 90)).strftime("%Y-%m-%d")
        payload = data_handler.client().get_option_chain(
            ticker, strike_count=100, from_date=today.strftime("%Y-%m-%d"), to_date=to_date)
        status = (payload or {}).get("status")
        if status and status != "SUCCESS":
            raise schwab_api.SchwabError(f"Schwab returned status '{status}' for {ticker}")
        _chain_cache[ticker] = (time.time(), payload)
        return payload


def option_chain(ticker: str, strategy: str = "atr") -> dict:
    """Build the option-chain view: regime banner, auto-picked LEAP, and the
    ATR-suggested weekly short with nearby strikes. Raises RegimeBlocked on RED."""
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker is required")

    reg = screening.regime()
    regime_status = reg.get("status")
    if regime_status == "red":
        raise RegimeBlocked("Market is RED. No entries.")
    atr_mult = REGIME_ATR_MULT.get(regime_status, REGIME_ATR_MULT["yellow"])

    payload = _fetch_chain(ticker)
    underlying, contracts = schwab_api.parse_call_chain(payload)
    if not contracts:
        raise schwab_api.SchwabError(f"no call contracts returned for {ticker}")

    # Anchor the spot price: chain quote first, then a live quote, then last close.
    if underlying is None:
        quote = data_handler.latest_quote(ticker)
        underlying = quote["price"] if quote else None

    # --- LEAP (auto-picked, delta ~0.90, closest to 180 DTE) ----------------
    leap = indicators.find_leap_strike(contracts, underlying)
    if leap:
        leap = {**leap, "target_contracts": config.LEAP_CONTRACTS}

    # --- Weekly short (regime-aware ATR strike + nearby strikes) ------------
    df = data_handler.get_daily(ticker)
    atr_val = indicators.atr(df)
    price = underlying if underlying is not None else indicators.last(df)
    weekly: dict | None = None
    if atr_val is not None and price is not None:
        suggested_strike = indicators.short_strike(price, atr_val, atr_mult)
        # Nearest expiration with at least one day left = this week's short.
        dated = [c for c in contracts if c.get("dte") is not None and c["dte"] >= 0]
        weekly_exp = None
        if dated:
            weekly_exp = min(dated, key=lambda c: c["dte"])["expiration"]
        exp_contracts = [c for c in contracts if c["expiration"] == weekly_exp] if weekly_exp else []
        strikes = indicators.get_nearby_strikes(exp_contracts, suggested_strike, underlying)
        weekly = {
            "expiration": weekly_exp,
            "dte": exp_contracts[0]["dte"] if exp_contracts else None,
            "suggested_strike": suggested_strike,
            "atr": round(atr_val, 2),
            "atr_mult": atr_mult,
            "strikes": strikes,
        }

    return {
        "ticker": ticker,
        "strategy": strategy,
        "regime": regime_status,
        "atr_mult": atr_mult,
        "underlying_price": round(underlying, 2) if underlying is not None else None,
        "leap": leap,
        "weekly": weekly,
    }
