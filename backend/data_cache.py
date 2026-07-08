"""Staleness-aware cache metadata for tiered data.

The parquet daily-bar cache (``data_handler``) tracks freshness only by file
mtime and records no provider identity on the frame. This module adds the
staleness layer the tiered scheduler needs: every datum the transport writes
carries ``fetched_at``, the ``provider`` that produced it, and the ``tier`` it was
fetched for (which derives its ``max_age`` via ``market_scheduler.max_age_seconds``).

Public surface:
  * ``put(symbol, data_kind, value, provider, tier)`` — transport records a fetch.
  * ``get_with_staleness(symbol, data_kind) -> (value, age_seconds, is_stale)`` —
    the spec accessor. Age is ``None`` and ``is_stale`` is ``True`` when nothing is
    known (unknown-fresh blocks action; it never permits it).
  * ``stale_blocks_go(symbol, ...)`` — the HARD_CFM_RULE enforcement: no GO verdict
    on stale inputs (same philosophy as the reconciliation freeze).
  * ``panel_staleness`` / ``summary`` — staleness surfaced to the frontend.

This module holds transient in-process state (like ``data_handler``'s caches); it
writes nothing to state.json.
"""
from __future__ import annotations

import threading
import time

import config
import market_scheduler as ms
from market_scheduler import BARS, CHAIN, QUOTE, Tier

# (symbol, data_kind) -> {"value", "fetched_at" (epoch), "provider", "tier"}
_store: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()

# Which data kinds a GO candidate depends on, by the tier it is being evaluated at.
# A GO on a name we'd actually enter (on-deck) needs a live quote AND fresh bars;
# these are the inputs the RS/juice/consolidation legs read.
GO_REQUIRED_KINDS = (QUOTE, BARS)


def reset() -> None:
    """Drop all staleness records — used when switching demo/live mode."""
    with _lock:
        _store.clear()


def active() -> bool:
    """True once the tiered scheduler has recorded at least one fetch — i.e. the
    staleness layer is actually populated. Used to gate unknown-fresh GO-blocking
    so a cold/offline store (warm scans, tests) never spuriously blocks."""
    with _lock:
        return bool(_store)


def put(symbol: str, data_kind: str, value, provider: str,
        tier: Tier | int, fetched_at: float | None = None) -> None:
    """Record a successful fetch with its provenance. ``fetched_at`` defaults to
    now (epoch seconds); tests pass an explicit value."""
    with _lock:
        _store[(symbol.upper(), data_kind)] = {
            "value": value,
            "fetched_at": time.time() if fetched_at is None else float(fetched_at),
            "provider": provider,
            "tier": int(tier),
        }


def record(symbol: str, data_kind: str) -> dict | None:
    with _lock:
        rec = _store.get((symbol.upper(), data_kind))
        return dict(rec) if rec is not None else None


def _max_age(tier: Tier | int | None, data_kind: str) -> float:
    t = Tier.T3 if tier is None else Tier(tier)
    return ms.max_age_seconds(t, data_kind)


def get_with_staleness(symbol: str, data_kind: str, tier: Tier | int | None = None,
                       now: float | None = None) -> tuple[object, float | None, bool]:
    """Return ``(value, age_seconds, is_stale)`` for a datum.

    ``is_stale`` is True when the datum is older than its (tier-derived) max age —
    OR when nothing is known about it (age ``None``). Unknown-fresh blocks action;
    it does not permit it. For bars with no staleness record we fall back to the
    parquet mtime so the existing daily-bar cache still reports freshness.
    """
    now = time.time() if now is None else now
    rec = record(symbol, data_kind)
    if rec is not None:
        age = max(0.0, now - rec["fetched_at"])
        t = tier if tier is not None else rec.get("tier")
        return rec["value"], age, age > _max_age(t, data_kind)
    if data_kind == BARS:
        import data_handler
        age_h = data_handler.cache_age_hours(symbol)
        if age_h is not None:
            age = age_h * 3600.0
            return None, age, age > _max_age(tier, BARS)
    # Nothing known — treat as stale (blocks, never permits).
    return None, None, True


# ---- STALE_BLOCKS_GO enforcement -------------------------------------------

def stale_blocks_go(symbol: str, tier: Tier | int | None = None, *,
                    required_kinds=GO_REQUIRED_KINDS, market_open: bool = True,
                    live: bool = True, now: float | None = None) -> tuple[bool, list[dict]]:
    """The HARD_CFM_RULE: a GO verdict must be refused when any input datum is
    stale beyond its tier tolerance.

    Returns ``(blocked, stale_inputs)``. A datum blocks when:
      * we have a record for it and it is stale beyond max_age; or
      * we have NO record for it AND we're live with the market open — i.e. we
        *should* have fresh data and its absence is unknown-fresh.

    Offline/demo (``live`` false or market closed) does not block on merely-absent
    records, so warm scans and the test suite behave normally; a record that
    exists and is stale always blocks, live or not. When ``STALE_BLOCKS_GO`` is off
    the rule is disabled entirely (never blocks)."""
    if not config.STALE_BLOCKS_GO:
        return False, []
    now = time.time() if now is None else now
    stale: list[dict] = []
    for kind in required_kinds:
        rec = record(symbol, kind)
        _, age, is_stale = get_with_staleness(symbol, kind, tier=tier, now=now)
        if rec is None:
            # unknown datum: only blocking in a live, open-market context
            if live and market_open:
                stale.append({"kind": kind, "reason": "no fresh data",
                              "age_seconds": None, "provider": None})
            continue
        if is_stale:
            stale.append({"kind": kind, "reason": "stale",
                          "age_seconds": round(age, 1) if age is not None else None,
                          "provider": rec.get("provider")})
    return (len(stale) > 0), stale


# ---- Staleness surfaced to the frontend ------------------------------------

def symbol_staleness(symbol: str, tier: Tier | int | None = None,
                     kinds=(QUOTE, BARS), now: float | None = None) -> dict:
    """Per-kind staleness for one symbol, for badges/panels."""
    now = time.time() if now is None else now
    out = {}
    for kind in kinds:
        rec = record(symbol, kind)
        _, age, is_stale = get_with_staleness(symbol, kind, tier=tier, now=now)
        out[kind] = {
            "age_seconds": round(age, 1) if age is not None else None,
            "is_stale": bool(is_stale),
            "provider": (rec or {}).get("provider"),
            "max_age_seconds": _max_age(tier if tier is not None else (rec or {}).get("tier"), kind),
        }
    return out


def panel_staleness(symbols, tiers: dict | None = None, kinds=(QUOTE, BARS),
                    now: float | None = None) -> dict:
    """Roll a set of symbols up to a single panel-level staleness verdict: the
    panel is ``stale`` if ANY tracked datum is stale beyond tolerance. Returns the
    flag plus the offending (symbol, kind) list so the badge can explain itself."""
    now = time.time() if now is None else now
    tiers = tiers or {}
    offenders = []
    for s in symbols:
        s = s.upper()
        detail = symbol_staleness(s, tier=tiers.get(s), kinds=kinds, now=now)
        for kind, info in detail.items():
            if info["is_stale"]:
                offenders.append({"symbol": s, "kind": kind,
                                  "age_seconds": info["age_seconds"],
                                  "provider": info["provider"]})
    return {"stale": len(offenders) > 0, "offenders": offenders}


def summary(now: float | None = None) -> dict:
    """A compact dump of the staleness store for /api/data-health — counts + the
    currently-stale records, so a silent freshness failure is visible."""
    now = time.time() if now is None else now
    records, stale = [], []
    with _lock:
        items = list(_store.items())
    for (sym, kind), rec in items:
        age = max(0.0, now - rec["fetched_at"])
        is_stale = age > _max_age(rec.get("tier"), kind)
        row = {"symbol": sym, "kind": kind, "provider": rec.get("provider"),
               "tier": rec.get("tier"), "age_seconds": round(age, 1),
               "is_stale": is_stale}
        records.append(row)
        if is_stale:
            stale.append(row)
    return {"count": len(records), "stale_count": len(stale), "stale": stale}
