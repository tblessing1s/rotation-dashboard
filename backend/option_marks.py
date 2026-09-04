"""Live short-call marks — a process-local cache fed by the tiered quote poller.

The poller prices every open position's STOCK every 2 minutes during market
hours. Its batched Schwab quote call takes option symbols too, so each open
short call rides the same request as its underlying at no extra provider
cost, and its live mark lands here. Consumers read the cache instead of
fetching:

* ``event_runner.detect_signals`` — the 75% buyback rule and the
  extrinsic-captured threshold now react to the OPTION's own price intraday,
  not only to the stock's effect on intrinsic;
* the engine's snapshot (``recommendation_runner``) — a pass reads the same
  marks the event was detected on;
* the positions view (``position_manager._live_short_marks``) — served from
  here when fresh, so opening the Positions tab no longer costs a quote call
  of its own.

Marks expire after ``OPTION_MARK_MAX_AGE_SECONDS``; a stale entry is treated as
absent and every consumer falls back to the stored entry mark exactly as
before. Nothing here is persisted — a restart simply starts empty until the
next poll — and nothing here carries authority: it is a price feed.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_marks: dict[str, dict] = {}   # OCC symbol -> {mark, bid, ask, last, at}


def is_option_symbol(symbol: str) -> bool:
    """The 21-char OCC layout Schwab uses: 6-char root, YYMMDD, C/P, 8-digit strike."""
    s = symbol or ""
    return len(s) == 21 and s[12] in ("C", "P") and s[6:12].isdigit() and s[13:].isdigit()


def symbol_for(ticker: str, sc: dict) -> str | None:
    """The OCC symbol of one short-call leg, or None when the leg can't be
    quoted (no expiration / strike — a legacy record)."""
    import schwab_api
    exp, strike = sc.get("expiration"), sc.get("strike")
    if not exp or strike is None:
        return None
    try:
        return schwab_api.occ_option_symbol(ticker, exp, float(strike), call=True)
    except (TypeError, ValueError):
        return None


def short_symbols(state: dict) -> dict[str, tuple[str, float, str]]:
    """Every quotable open short call in the book: OCC symbol -> (ticker,
    strike, expiration)."""
    out: dict[str, tuple[str, float, str]] = {}
    for pos in state.get("positions", []) or []:
        if pos.get("status") == "closed":
            continue
        t = (pos.get("ticker") or "").upper()
        for sc in pos.get("short_calls", []) or []:
            sym = symbol_for(t, sc)
            if sym:
                out[sym] = (t, sc.get("strike"), sc.get("expiration"))
    return out


def remember(symbol: str, node: dict, at: datetime | None = None) -> float | None:
    """Store one option quote node ({mark, bid, ask, last}); returns the mark
    kept (mid preferred, bid as the conservative fallback, else last)."""
    node = node or {}
    mark = node.get("mark")
    if mark is None:
        mark = node.get("bid")
    if mark is None:
        mark = node.get("last")
    if mark is None:
        return None
    at = at or datetime.now(timezone.utc)
    with _lock:
        _marks[symbol] = {"mark": float(mark), "bid": node.get("bid"), "ask": node.get("ask"),
                          "last": node.get("last"), "at": at}
    return float(mark)


def _fresh(entry: dict, now: datetime) -> bool:
    at = entry.get("at")
    if at is None:
        return False
    if at.tzinfo is None or now.tzinfo is None:
        at, now = at.replace(tzinfo=None), now.replace(tzinfo=None)
    return (now - at).total_seconds() <= float(config.OPTION_MARK_MAX_AGE_SECONDS)


def mark_for(ticker: str, sc: dict, now: datetime | None = None) -> float | None:
    """The live per-share mark for one leg, or None when absent or stale."""
    sym = symbol_for(ticker, sc)
    if not sym:
        return None
    now = now or datetime.now(timezone.utc)
    with _lock:
        entry = _marks.get(sym)
        return entry["mark"] if entry and _fresh(entry, now) else None


def marks_for(ticker: str, shorts: list[dict], now: datetime | None = None) -> dict[tuple, float]:
    """Fresh live marks for a position's shorts keyed by (strike, expiration) —
    the shape position_manager.enrich_position already consumes."""
    now = now or datetime.now(timezone.utc)
    out: dict[tuple, float] = {}
    for sc in shorts or []:
        m = mark_for(ticker, sc, now)
        if m is not None:
            out[(sc.get("strike"), sc.get("expiration"))] = m
    return out


def reset() -> None:
    with _lock:
        _marks.clear()


def status(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    with _lock:
        fresh = [s for s, e in _marks.items() if _fresh(e, now)]
        latest = max((e["at"] for e in _marks.values()), default=None)
    return {"cached": len(_marks), "fresh": len(fresh),
            "max_age_seconds": config.OPTION_MARK_MAX_AGE_SECONDS,
            "last_at": latest.isoformat() if latest else None}
