"""Per-provider / per-tier / per-day API-call accounting + the shed ladder.

The codebase has no call counter today (only a coarse fallback-events int). This
module logs every provider call the transport makes, keyed by day → provider →
tier → data_kind, and persists the counters to a small JSON file **outside
state.json** (telemetry must never pollute the execution record). It also owns the
budget-pressure shed decision: as a provider approaches its configured daily
limit, shed cheap tiers first — Tier 3, then Tier 2, then reduce Tier 1 cadence —
and NEVER Tier 0 (HARD_CFM_RULE: open-position monitoring is never sacrificed).

Counters are keyed by the ET calendar day and reset automatically at the day
boundary. Every function that needs "today" accepts an explicit ``day`` so the
logic is testable without a wall clock.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from market_scheduler import Tier

logger = logging.getLogger("cfm.budget")
ET = ZoneInfo("America/New_York")

# Where the counters persist. A module attribute so tests can point it at a tmp
# file; kept out of state.json by design.
PATH = os.path.join(config.DATA_DIR, "data_budget.json")

_lock = threading.RLock()
# {"date": "YYYY-MM-DD", "counts": {provider: {tier: {kind: int}}}, "shed_log": [...]}
_state: dict | None = None

_PROVIDER_LIMITS = {
    "schwab": lambda: config.SCHWAB_DAILY_CALL_LIMIT,
    "alphavantage": lambda: config.ALPHA_VANTAGE_DAILY_CALL_LIMIT,
}


def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _blank(day: str) -> dict:
    return {"date": day, "counts": {}, "shed_log": []}


def _load() -> dict:
    global _state
    if _state is not None:
        return _state
    try:
        with open(PATH, encoding="utf-8") as fh:
            _state = json.load(fh)
    except (OSError, ValueError):
        _state = _blank(today_et())
    return _state


def _persist() -> None:
    try:
        os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_state, fh)
        os.replace(tmp, PATH)
    except OSError as e:  # noqa: BLE001 — telemetry persistence is best-effort
        logger.warning("data-budget persist failed: %s", e)


def _roll(day: str) -> dict:
    """Ensure the in-memory state is for ``day``; reset at a day boundary."""
    st = _load()
    if st.get("date") != day:
        st = _blank(day)
        globals()["_state"] = st
        _persist()
    return st


def reset(day: str | None = None) -> None:
    """Clear counters (tests + demo-mode switch). Removes the persisted file."""
    global _state
    with _lock:
        _state = _blank(day or today_et())
        try:
            if os.path.exists(PATH):
                os.remove(PATH)
        except OSError:
            pass


def record(provider: str, tier: Tier | int, kind: str, n: int = 1,
           day: str | None = None) -> None:
    """Log ``n`` calls to ``provider`` for ``tier``/``kind`` on ``day`` (default
    today ET). Persisted immediately (small file, atomic write)."""
    day = day or today_et()
    with _lock:
        st = _roll(day)
        counts = st["counts"].setdefault(provider, {})
        by_tier = counts.setdefault(str(int(tier)), {})
        by_tier[kind] = by_tier.get(kind, 0) + n
        _persist()


def counts(day: str | None = None) -> dict:
    day = day or today_et()
    with _lock:
        return json.loads(json.dumps(_roll(day)["counts"]))  # deep copy


def provider_used(provider: str, day: str | None = None) -> int:
    """Total calls to a provider today, across all tiers/kinds."""
    by_tier = counts(day).get(provider, {})
    return sum(v for kinds in by_tier.values() for v in kinds.values())


def provider_limit(provider: str) -> int | None:
    fn = _PROVIDER_LIMITS.get(provider)
    return fn() if fn else None


def usage_pct(provider: str, day: str | None = None) -> float:
    limit = provider_limit(provider)
    if not limit:
        return 0.0
    return provider_used(provider, day) / limit * 100.0


# ---- Shed ladder -----------------------------------------------------------
# Graduated by how far over the soft limit a provider is. The order is strict:
# Tier 3 sheds first, then Tier 2, then Tier 1 cadence is reduced. Tier 0 never.

def _midpoint() -> float:
    """Usage% at which Tier 2 joins Tier 3 in being shed — halfway from the soft
    limit to the hard limit (PROPOSED_DEFAULT curve)."""
    return (config.BUDGET_SOFT_LIMIT_PCT + 100.0) / 2.0


def shed_level(provider: str, day: str | None = None) -> int:
    """0 = nothing shed · 1 = Tier 3 · 2 = Tier 3+2 · 3 = Tier 3+2 + Tier 1 slowed."""
    pct = usage_pct(provider, day)
    if pct >= 100.0:
        return 3
    if pct >= _midpoint():
        return 2
    if pct >= config.BUDGET_SOFT_LIMIT_PCT:
        return 1
    return 0


def drop_tier(tier: Tier | int, provider: str, day: str | None = None) -> bool:
    """Should this tier's fetches be dropped entirely under current pressure?
    Tier 3 at level ≥1, Tier 2 at level ≥2. Tier 0 and Tier 1 are never dropped
    (Tier 1 is only slowed — see ``t1_cadence_multiplier``)."""
    tier = Tier(tier)
    if config.TIER0_NEVER_SHED and tier == Tier.T0:
        return False
    level = shed_level(provider, day)
    if tier == Tier.T3:
        return level >= 1
    if tier == Tier.T2:
        return level >= 2
    return False


def t1_cadence_multiplier(provider: str, day: str | None = None) -> float:
    """Factor to stretch the Tier 1 quote interval by under deep pressure (level 3).
    1.0 normally; 2.0 when Tier 1 cadence is being reduced (PROPOSED_DEFAULT)."""
    return 2.0 if shed_level(provider, day) >= 3 else 1.0


def note_shed(provider: str, level: int, day: str | None = None) -> None:
    """Record + log a shed-level transition so it's visible in the UI and logs.
    Only logs on a CHANGE (rising or falling edge), never every tick."""
    day = day or today_et()
    with _lock:
        st = _roll(day)
        prev = st.get("shed_level", {}).get(provider) if isinstance(st.get("shed_level"), dict) else None
        st.setdefault("shed_level", {})
        if prev != level:
            st["shed_level"][provider] = level
            entry = {"provider": provider, "level": level, "at": datetime.now(ET).strftime("%H:%M:%S"),
                     "usage_pct": round(usage_pct(provider, day), 1)}
            st["shed_log"].append(entry)
            st["shed_log"] = st["shed_log"][-50:]
            _persist()
            if level > 0:
                logger.warning("data-budget shed: %s at level %d (%.0f%% of daily limit) — "
                               "shedding %s", provider, level, entry["usage_pct"],
                               {1: "Tier 3", 2: "Tier 3+2", 3: "Tier 3+2, Tier 1 slowed"}[level])
            else:
                logger.info("data-budget shed cleared: %s back under soft limit", provider)


def snapshot(day: str | None = None) -> dict:
    """The /api/data-budget payload: today's counts, per-provider usage vs limit,
    current shed level per provider, and the shed transition log."""
    day = day or today_et()
    with _lock:
        st = _roll(day)
        providers = {}
        for prov in set(list(_PROVIDER_LIMITS) + list(st["counts"])):
            lvl = shed_level(prov, day)
            providers[prov] = {
                "used": provider_used(prov, day),
                "limit": provider_limit(prov),
                "usage_pct": round(usage_pct(prov, day), 1),
                "shed_level": lvl,
                "shed": {"tier3": drop_tier(Tier.T3, prov, day),
                         "tier2": drop_tier(Tier.T2, prov, day),
                         "tier1_cadence_mult": t1_cadence_multiplier(prov, day),
                         "tier0": False},
            }
        return {"date": day, "by_tier": json.loads(json.dumps(st["counts"])),
                "providers": providers, "shed_log": list(st["shed_log"])}
