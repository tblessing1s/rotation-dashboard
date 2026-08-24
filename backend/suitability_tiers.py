"""Suitability suppression tiers — the entry universe, filtered by CAPACITY.

TRAVIS_EXTENSION. The scan surfaces names that can never clear the juice floor
alongside genuinely benchable ones, and the bench then advertises a CLEARABLE
condition ("pull back within 1 ATR of MA21") in front of an UNCLEARABLE juice
failure. ET at 0.14%/wk and XLE at 0.34%/wk reach WATCH/BENCH with a displayed
path to READY that leads nowhere. This module classifies every scanned name so
those two cases stop looking alike:

  ``SUITABLE``              capacity clears the floor — full scan membership.
  ``SUPPRESSED_CONDITION``  capacity clears but the CURRENT reading does not
                            (IV compression). Recoverable; rechecked weekly.
  ``SUPPRESSED_STRUCTURAL`` capacity itself is far below the floor — the
                            instrument is built low-vol. Rechecked monthly.

plus the guard that outranks all three: a capacity of
``juice_capacity.INSUFFICIENT_HISTORY`` classifies SUITABLE and is
UNSUPPRESSIBLE. A name is never hidden on a number that is not statistically
meaningful yet.

THE HARD SAFETY INVARIANT [SUPPRESSION_IS_ENTRY_ONLY]
-----------------------------------------------------
Suppression governs the ENTRY UNIVERSE ONLY. Open positions are monitored,
defended, killed and reconciled at full cadence regardless of tier.

That is not a convention this module asks callers to respect — it is structural.
Every position-management path derives its working set from
``state["positions"]``: the kill switch (``kill_switch.evaluate_all``),
defend/roll (``recommendation_runner.build_market_snapshot``), reconciliation,
assignment handling, portfolio risk, the order lifecycle and the accrual ledger.
None of them reads scan membership, the scan cache, ``bench`` or ``suitability``,
and the intraday refresh set pins open positions in a tier that is never
truncated (``refresh_policy.hot_tickers`` Tier 1). Hiding a name from the scan
therefore CANNOT remove it from any of them. ``test_suitability_tiers`` pins this
with a byte-identical open-position comparison; see
AUDIT_SUITABILITY_SUPPRESSION_PHASE0.md §3.

Suppression is applied at ONE shared function —
``metrics.scorecard.split_by_suitability`` — called at the entry-facing
boundaries only, exactly as the affordability filter is. Nothing inside the
sweep changes, so the nightly telemetry and the capacity observation feed keep
seeing every name.

HYSTERESIS
----------
Suppress below 80% of the floor, readmit at 100%: a name that flickers around
one threshold would otherwise appear and vanish from the scan week to week.
STRUCTURAL exits on CAPACITY recovering past 70% of the floor — a median moves
slowly, so a modest band suffices — and lands in CONDITION rather than jumping
straight to SUITABLE, because a recovered capacity says nothing about today.

EVENTS
------
Every tier change appends a typed event; the CURRENT tier is DERIVED by folding
the event stream, never stored as mutable state. Same shape as ``scan_diff_log``
(append-only ``events`` under ``DATA_DIR``, out of ``state.json``), and the fold
is memoized on the file's mtime so a 500-name sweep pays for it once.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import config

LOG_PATH = os.path.join(config.DATA_DIR, "suitability_tiers.json")
_lock = threading.RLock()

SCHEMA_VERSION = 1

# Tiers. UNCLASSIFIED is the from_tier of an initial classification only — it is
# never a resting state and never appears as a to_tier.
UNCLASSIFIED = "UNCLASSIFIED"
SUITABLE = "SUITABLE"
SUPPRESSED_CONDITION = "SUPPRESSED_CONDITION"
SUPPRESSED_STRUCTURAL = "SUPPRESSED_STRUCTURAL"

SUPPRESSED_TIERS = (SUPPRESSED_CONDITION, SUPPRESSED_STRUCTURAL)

# Why a name landed where it did — carried on the event and shown in the UI, so
# a classification never has to be reverse-engineered from the numbers.
REASON_INSUFFICIENT_HISTORY = "insufficient_history"
REASON_CAPACITY_BELOW_FLOOR = "capacity_below_floor"
REASON_CURRENT_BELOW_FLOOR = "current_below_floor"
REASON_HOLD_NO_READMIT = "hold_awaiting_readmit"
REASON_CLEARS = "clears_floor"
REASON_UNPRICEABLE = "current_unpriceable"
REASON_STRUCTURAL_NEEDS_LIVE_OBS = "structural_needs_live_observations"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Classification — PURE
# ---------------------------------------------------------------------------
def next_recheck_date(tier: str, day: str | None = None) -> str | None:
    """The date a suppressed name is re-evaluated. None for SUITABLE.

    NOTE [RECHECK_DATE_IS_DISPLAY_ONLY]: this date is computed, persisted on the
    transition event and displayed, but it does NOT gate evaluation — the sweep
    re-evaluates every name every day, suppressed or not. Skipping evaluation
    would remove the name's row from the day's cached sweep, which makes it
    permanently "missing" to ``scan_cache.reusable`` and re-triggers the
    incremental recompute on every request; it would also starve the capacity
    observation feed this whole feature depends on, so a STRUCTURAL name sampled
    monthly would eventually age out of its own window and flip back to
    INSUFFICIENT_HISTORY — i.e. un-suppress itself. The date is what the UI shows
    and what a future skip optimisation would key off. See
    AUDIT_SUITABILITY_SUPPRESSION_PHASE0.md §4."""
    days = {SUPPRESSED_CONDITION: config.RECHECK_CONDITION_DAYS,
            SUPPRESSED_STRUCTURAL: config.RECHECK_STRUCTURAL_DAYS}.get(tier)
    if days is None:
        return None
    base = datetime.strptime(day or _today(), "%Y-%m-%d")
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def classify(capacity, current: float | None, floor: float | None, *,
             current_tier: str | None = None,
             live_obs: int | None = None) -> dict:
    """The tier for one name. PURE — no I/O, no clock, no store.

    ``capacity``      the trailing median (``juice_capacity_wk_pct``), a number
                      or the ``INSUFFICIENT_HISTORY`` sentinel.
    ``current``       today's combined weekly-equivalent yield, or None when the
                      name cannot be priced.
    ``floor``         the profile-aware income floor
                      (``scan_triggers.floor_for_profile``).
    ``current_tier``  the name's tier before this evaluation; drives hysteresis.
                      None/UNCLASSIFIED is an initial classification.
    ``live_obs``      count of LIVE-provenance observations behind the capacity.
                      Guards the structural verdict — see below.

    Returns ``{tier, reason, capacity, current, floor, insufficient_history,
    changed_from}``.
    """
    import juice_capacity

    prior = current_tier if current_tier in (SUITABLE, *SUPPRESSED_TIERS) else None

    def out(tier: str, reason: str) -> dict:
        return {"tier": tier, "reason": reason, "capacity": capacity,
                "current": current, "floor": floor,
                "insufficient_history": capacity is juice_capacity.INSUFFICIENT_HISTORY
                or capacity == juice_capacity.INSUFFICIENT_HISTORY,
                "changed_from": prior}

    # 1. UNSUPPRESSIBLE. A number the sample cannot support must never hide a
    #    name — visibility is the safe failure, and this is the only rule that
    #    outranks every other. Deliberate consequence: a genuinely compressed new
    #    name stays visible until its history is long enough to judge.
    if capacity == juice_capacity.INSUFFICIENT_HISTORY or capacity is None:
        return out(SUITABLE, REASON_INSUFFICIENT_HISTORY)
    # An unusable floor cannot classify anything. Same failure direction.
    if floor is None or floor <= 0:
        return out(SUITABLE, REASON_INSUFFICIENT_HISTORY)

    structural_bar = floor * config.SUPPRESS_STRUCTURAL_CAPACITY_RATIO
    structural_exit = floor * config.STRUCTURAL_READMIT_CAPACITY_RATIO
    condition_bar = floor * config.SUPPRESS_CONDITION_CURRENT_RATIO
    readmit_bar = floor * config.READMIT_CURRENT_RATIO

    # 2. STRUCTURAL — the capacity leg, with its own hysteresis band.
    if prior == SUPPRESSED_STRUCTURAL:
        if capacity < structural_exit:
            return out(SUPPRESSED_STRUCTURAL, REASON_CAPACITY_BELOW_FLOOR)
        # Recovered past the exit band. Fall through and re-judge the CURRENT
        # reading from scratch: a recovered median says the instrument CAN pay,
        # not that it is paying today, so the honest landing spot may be
        # CONDITION rather than SUITABLE.
        prior = None
    elif capacity < structural_bar:
        # A backfill-dominated median is juice-only and understates a dividend
        # payer (see config.SUPPRESS_STRUCTURAL_MIN_LIVE_OBS). Permanently hiding
        # a payer on a number we know is biased low is this feature's worst
        # failure mode, so the structural verdict waits for real observations and
        # the name is judged on its current reading in the meantime.
        if (live_obs is not None
                and live_obs < config.SUPPRESS_STRUCTURAL_MIN_LIVE_OBS):
            if current is not None and current < condition_bar:
                return out(SUPPRESSED_CONDITION, REASON_STRUCTURAL_NEEDS_LIVE_OBS)
            return out(SUITABLE, REASON_STRUCTURAL_NEEDS_LIVE_OBS)
        return out(SUPPRESSED_STRUCTURAL, REASON_CAPACITY_BELOW_FLOOR)

    # 3. CONDITION — the current-reading leg. Capacity clears by here.
    if current is None:
        # Unpriceable today: hold whatever the name already was. An outage is
        # neither a reason to suppress nor evidence for readmission.
        return out(prior or SUITABLE, REASON_UNPRICEABLE)
    if prior == SUPPRESSED_CONDITION:
        if current >= readmit_bar:
            return out(SUITABLE, REASON_CLEARS)
        return out(SUPPRESSED_CONDITION, REASON_HOLD_NO_READMIT)
    if current < condition_bar:
        return out(SUPPRESSED_CONDITION, REASON_CURRENT_BELOW_FLOOR)

    # 4. Clears on both legs.
    return out(SUITABLE, REASON_CLEARS)


def classify_row(row: dict, current_tier: str | None = None) -> dict:
    """``classify`` driven off an already-computed scan row. No recomputation:
    capacity, the floor and the source counts all ride on the row's
    ``juice_capacity`` block, and ``current`` is the row's own combined yield."""
    cap = row.get("juice_capacity") or {}
    by_source = cap.get("by_source") or {}
    import juice_capacity
    live_obs = (by_source.get(juice_capacity.SOURCE_LIVE, 0)
                + by_source.get(juice_capacity.SOURCE_SEED, 0))
    current = row.get("combined_weekly_yield_pct")
    if current is None:
        current = row.get("juice_weekly_pct")
    return classify(cap.get("capacity"), current, cap.get("floor_pct"),
                    current_tier=current_tier, live_obs=live_obs)


# ---------------------------------------------------------------------------
# Enforcement gate — shadow-first, and the flip is manual
# ---------------------------------------------------------------------------
def enforcement(today: str | None = None) -> dict:
    """Whether HIDING and bench-ineligibility are live: ``{active, reason,
    shadow_days_elapsed, shadow_days_required, review_date}``.

    Three conditions, all required, none automated:

      1. ``config.SUPPRESSION_ENFORCE`` is on (a deliberate config change).
      2. ``config.SUPPRESSION_REVIEW_DATE`` is set — the dated review of the
         CAPACITY numbers against real names.
      3. ``SUPPRESSION_SHADOW_DAYS`` have elapsed SINCE THAT REVIEW.

    The clock runs from the review rather than the deploy on purpose: before the
    capacity numbers are reviewed every name reads INSUFFICIENT_HISTORY and
    classifies SUITABLE, so a shadow period started at deploy would observe
    nothing at all. Nothing here ever flips itself."""
    out = {"active": False, "reason": None,
           "shadow_days_elapsed": None,
           "shadow_days_required": config.SUPPRESSION_SHADOW_DAYS,
           "review_date": config.SUPPRESSION_REVIEW_DATE}
    if not config.SUPPRESSION_ENFORCE:
        out["reason"] = "SUPPRESSION_ENFORCE is off (shadow mode)"
        return out
    if not config.SUPPRESSION_REVIEW_DATE:
        out["reason"] = ("no dated capacity review — set SUPPRESSION_REVIEW_DATE "
                         "once the capacity numbers have been checked against real names")
        return out
    try:
        review = datetime.strptime(config.SUPPRESSION_REVIEW_DATE, "%Y-%m-%d")
        now = datetime.strptime(today or _today(), "%Y-%m-%d")
    except ValueError:
        out["reason"] = f"unparseable SUPPRESSION_REVIEW_DATE {config.SUPPRESSION_REVIEW_DATE!r}"
        return out
    elapsed = (now - review).days
    out["shadow_days_elapsed"] = elapsed
    if elapsed < config.SUPPRESSION_SHADOW_DAYS:
        out["reason"] = (f"shadow period: {elapsed} of "
                         f"{config.SUPPRESSION_SHADOW_DAYS} days since the capacity review")
        return out
    out["active"] = True
    out["reason"] = f"enforced ({elapsed} days since the capacity review)"
    return out


def enforcing(today: str | None = None) -> bool:
    return bool(enforcement(today)["active"])


# ---------------------------------------------------------------------------
# Event store — append-only; the current tier is DERIVED, never stored
# ---------------------------------------------------------------------------
_parsed: tuple[tuple, dict] | None = None


def _load_raw() -> dict:
    """A fresh parse off disk. Writers use this — the memo hands back a shared
    dict and mutating it in place would expose a half-applied batch."""
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"events": []}


def _load() -> dict:
    """The store, re-parsed only when the file changed. READ PATH ONLY — the
    returned dict is shared, so callers must treat it as immutable."""
    global _parsed
    try:
        st = os.stat(LOG_PATH)
        stamp = (LOG_PATH, st.st_mtime_ns, st.st_size)
    except OSError:
        _parsed = None
        return {"events": []}
    memo = _parsed
    if memo is not None and memo[0] == stamp:
        return memo[1]
    data = _load_raw()
    _parsed = (stamp, data)
    return data


def _save(data: dict) -> None:
    tmp = f"{LOG_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, LOG_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def events(symbol: str | None = None) -> list[dict]:
    """The transition events, oldest first; one symbol's when given."""
    evs = _load()["events"]
    if symbol is None:
        return list(evs)
    sym = (symbol or "").strip().upper()
    return [e for e in evs if e.get("symbol") == sym]


def tiers_now() -> dict[str, dict]:
    """Every symbol's CURRENT tier, DERIVED by folding the whole event stream.

    The single source of truth for "what tier is this name". There is no
    persisted current-tier field to drift out of step with the events — the fold
    is the derivation, memoized on the store's mtime so a full-universe sweep
    pays for it once rather than 500 times."""
    latest: dict[str, dict] = {}
    for e in _load()["events"]:
        sym = e.get("symbol")
        if sym:
            latest[sym] = e
    return {sym: {"tier": e.get("to_tier"),
                  "reason": e.get("reason"),
                  "next_recheck_date": e.get("next_recheck_date"),
                  "since": e.get("at"),
                  "capacity": e.get("capacity"),
                  "current": e.get("current"),
                  "floor": e.get("floor")}
            for sym, e in latest.items()}


def current_tier(symbol: str) -> str:
    """One symbol's current tier, or UNCLASSIFIED when it has no events."""
    rec = tiers_now().get((symbol or "").strip().upper())
    return (rec or {}).get("tier") or UNCLASSIFIED


def _event(symbol: str, from_tier: str, verdict: dict, day: str) -> dict:
    return {
        "symbol": symbol,
        "schema": SCHEMA_VERSION,
        "at": _now_iso(),
        "date": day,
        "from_tier": from_tier,
        "to_tier": verdict["tier"],
        "reason": verdict["reason"],
        "capacity": verdict["capacity"],
        "current": verdict["current"],
        "floor": verdict["floor"],
        "next_recheck_date": next_recheck_date(verdict["tier"], day),
    }


def record_classification(rows: list[dict], day: str | None = None) -> dict:
    """Classify every row and append an event for each name whose tier CHANGED.

    The initial classification of the universe is a batch of events with
    ``from_tier: UNCLASSIFIED``. Unchanged tiers append nothing — the stream is a
    transition log, not a daily snapshot, so "what changed" stays readable and
    the store does not grow by the universe every night.

    Best-effort: never raises into the sweep that calls it.
    Returns ``{ok, classified, transitions, day}``."""
    day = day or _today()
    try:
        now = tiers_now()
        new_events: list[dict] = []
        classified = 0
        for row in rows or []:
            sym = (row.get("ticker") or "").strip().upper()
            if not sym:
                continue
            classified += 1
            was = (now.get(sym) or {}).get("tier") or UNCLASSIFIED
            verdict = classify_row(row, current_tier=was)
            if verdict["tier"] != was:
                new_events.append(_event(sym, was, verdict, day))
        if new_events:
            with _lock:
                data = _load_raw()
                data["events"].extend(new_events)
                _save(data)
        return {"ok": True, "classified": classified,
                "transitions": len(new_events), "day": day}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e), "day": day}


def recheck(symbol: str, row: dict | None = None, day: str | None = None) -> dict:
    """Force an immediate re-evaluation of one name — the manual "recheck now".

    Suppression must never make a name unreachable pending a date, so this
    bypasses the recheck cadence entirely. Computes a FRESH scan row when one is
    not supplied (which re-runs the full juice computation, so the capacity
    observation series continues at this cadence too), reclassifies, and appends
    an event if the tier moved. Returns the verdict plus whether it changed."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "no symbol"}
    day = day or _today()
    try:
        if row is None:
            from metrics import scorecard as scorecard_metrics
            rows = scorecard_metrics.scorecard([sym]).get("results") or []
            row = next((r for r in rows
                        if (r.get("ticker") or "").upper() == sym), None)
        if row is None:
            return {"ok": False, "error": f"no scan row for {sym}"}
        was = current_tier(sym)
        verdict = classify_row(row, current_tier=was)
        changed = verdict["tier"] != was
        if changed:
            with _lock:
                data = _load_raw()
                data["events"].append(_event(sym, was, verdict, day))
                _save(data)
        return {"ok": True, "symbol": sym, "from_tier": was,
                "tier": verdict["tier"], "reason": verdict["reason"],
                "changed": changed, "capacity": verdict["capacity"],
                "current": verdict["current"], "floor": verdict["floor"],
                "next_recheck_date": next_recheck_date(verdict["tier"], day)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "symbol": sym}
