"""Day-trade sleeve's OWN ticker roster — a separate editable JSON store from
CFM's universe (``sector_data.py`` / ``config.UNIVERSE_PATH``).

CFM's universe is picked for dividend/options fit (weeklies, sector RS); the
day-trade sleeve wants intraday-liquidity fit ($20-150, high volume, 2-5%
ATR%) and has no reason to be limited to (or grow/shrink with) CFM's list.
So this store is seeded ONCE, as a plain copy of ``sector_data.all_tickers()``
at first load, and is entirely independent from then on: CFM adding or
removing a name never changes this roster, and vice versa. ``daytrade/
universe.py``'s nightly screener reads it via ``all_tickers()`` and filters
it down to that day's picks the same way it always has.

No sectors/groups here (unlike ``sector_data.py``) — the day-trade sleeve
doesn't compute RS-vs-sector, so a flat, deduplicated ticker list is all it
needs.
"""
from __future__ import annotations

import json
import os
import threading

import config

SCHEMA_VERSION = 1
_lock = threading.RLock()


def _read_store() -> list[str] | None:
    try:
        with open(config.DAYTRADE_TICKERS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        tickers = data.get("tickers")
        if not (isinstance(tickers, list) and tickers):
            return None
        return [str(t).strip().upper() for t in tickers if str(t).strip()]
    except (OSError, ValueError):
        return None


def _write_store(tickers: list[str]) -> None:
    import logging_handler as log  # reuse the atomic-write machinery (fsync + rename)
    payload = json.dumps({"schema_version": SCHEMA_VERSION,
                          "tickers": sorted(set(tickers))}, indent=2)
    log._atomic_write(config.DAYTRADE_TICKERS_PATH, payload)


def all_tickers() -> list[str]:
    """The day-trade sleeve's own roster, sorted. Self-heals by seeding a
    ONE-TIME copy of CFM's current universe (``sector_data.all_tickers()``)
    if the store is missing or empty — after that first write, this list
    lives entirely on its own; a later CFM universe change never touches it."""
    with _lock:
        tickers = _read_store()
        if tickers is None:
            import sector_data
            tickers = sorted(set(sector_data.all_tickers()))
            _write_store(tickers)
        return tickers


def add_ticker(ticker: str) -> dict:
    """Add one ticker to the day-trade roster. Rejects blanks and duplicates."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    with _lock:
        tickers = all_tickers()
        if ticker in tickers:
            raise ValueError(f"{ticker} is already in the day-trade universe")
        tickers.append(ticker)
        _write_store(tickers)
    return {"added": ticker}


def remove_ticker(ticker: str) -> dict:
    """Remove one ticker from the day-trade roster. Raises if it isn't there."""
    ticker = (ticker or "").strip().upper()
    with _lock:
        tickers = all_tickers()
        if ticker not in tickers:
            raise ValueError(f"{ticker} is not in the day-trade universe")
        tickers.remove(ticker)
        _write_store(tickers)
    return {"removed": ticker}


def remove_tickers(tickers: list[str]) -> dict:
    """Bulk remove; unknown/blank entries are skipped rather than raising —
    same forgiving shape as ``sector_data.remove_tickers`` for a
    'clear all these at once' bulk action."""
    wanted = {str(t).strip().upper() for t in (tickers or []) if str(t).strip()}
    with _lock:
        current = all_tickers()
        removed = [t for t in current if t in wanted]
        if removed:
            remaining = [t for t in current if t not in wanted]
            _write_store(remaining)
    return {"removed": removed}
