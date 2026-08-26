"""Forward-looking TRIGGER + the profile-aware SHADOW income floor.

WHAT WAS DELETED, AND WHY IT IS NOT COMING BACK
-----------------------------------------------
This module used to carry the whole trigger-emission machinery: four trigger KINDS
(CALENDAR / CONDITIONAL / ESTIMATED / SAFETY), a per-check kind table, a
days-to-trigger estimator, the "path to READY" renderer, the BENCH derived view,
and ``compose_row_verdict`` — the severity fold that turned a signal composition
plus every failing gate block into READY / CAUTION / WATCH / BLOCKED.

All of it existed to answer "when will this name clear the filter?", and it was
proportional to the size of the veto set. With the veto set reduced to the exit
mirrors plus hard account constraints (``scan_verdict.VETOES``), almost every case
disappeared: a CONDITIONAL trigger for a gate that no longer blocks is a countdown
to nothing, and an ESTIMATED days-to-trigger for it is a fabricated number about a
condition that never mattered. A name that fails only ranking inputs is ELIGIBLE
today — there is nothing to wait for.

ONE trigger survives, because one veto is genuinely a calendar fact:

    EARNINGS. ``account_gate``'s ``earnings_in_cycle`` blocks the whole planned
    cycle, so the name becomes enterable on a DETERMINISTIC date — the trading day
    after the report, plus a settle buffer. That is a real answer to "when", not an
    extrapolation, and it is the one an operator actually plans around.

The SHADOW income floor below is unchanged and still carries ZERO authority.

Everything here is PURE: no I/O, no clock, no fetching. The caller supplies the
values, including today's date where one is needed.
"""
from __future__ import annotations

from datetime import date, timedelta

import config

# The only surviving trigger kind. A deterministic date, never an estimate.
CALENDAR = "calendar"

# PROPOSED_DEFAULT — the post-earnings settle buffer. The Level-5 gate blocks the
# WHOLE planned cycle (0..CYCLE_WEEKS_MAX*7 days), so the veto clears the trading
# day AFTER the report; this buffer is the only knob and is NOT an existing
# HARD_CFM_RULE (there is no earnings-buffer constant in config today).
EARNINGS_TRIGGER_BUFFER_DAYS = 1        # PROPOSED_DEFAULT


def _add_days(iso: str | None, days: int) -> str | None:
    """ISO date string + ``days`` calendar days (pure — no clock)."""
    if not iso:
        return None
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None
    return (d + timedelta(days=days)).isoformat()


def earnings_trigger(blocks: list[dict] | None) -> dict | None:
    """The one forward trigger the thin veto set still supports, or None.

    Reads the ``earnings_in_cycle`` veto block emitted by ``scan_verdict.evaluate``
    and turns its observed earnings date into the date the name becomes enterable.
    A READ of an already-computed block — it never re-evaluates the earnings rule
    and never fetches a date.

    Returns None when no earnings veto fired, or when the block carries no usable
    date: an unknown report date has no calendar answer, and inventing one would be
    exactly the false precision the ESTIMATED kind was deleted for.
    """
    for b in blocks or []:
        if b.get("veto") != "earnings_in_cycle":
            continue
        earn = (b.get("observed") or {}).get("earnings") or {}
        eligible = _add_days(earn.get("date"), EARNINGS_TRIGGER_BUFFER_DAYS)
        if not eligible:
            return None
        return {"kind": CALENDAR, "id": "earnings_in_cycle",
                "clears_when": "the report is out",
                "eligible_date": eligible,
                "earnings_date": earn.get("date")}
    return None


# ---------------------------------------------------------------------------
# Combined weekly-equivalent yield + the profile-aware SHADOW floor (schema v21)
# ---------------------------------------------------------------------------
# TRAVIS_EXTENSION. Everything below is SHADOW: it is computed, returned and
# logged, and it has ZERO authority. Nothing here is ever appended to the `blocks`
# list that ``scan_verdict.compose`` reads — that list is what gives a finding
# verdict authority, so keeping the shadow observation OUT of it is the load-
# bearing invariant of this whole feature. It reaches the RANKER (scan_score) and
# nothing else. There is deliberately NO config switch that would turn any of this
# into a block; graduating a floor to veto authority is a future work item
# contingent on logged real-data calibration, reviewed on its own.

def combined_weekly_yield(juice_weekly_pct: float | None,
                          annual_dividend_yield_pct: float | None) -> dict:
    """The combined weekly-equivalent yield and its two separable components.

        combined = juice%/wk + (trailing annual dividend % / DIVIDEND_WEEKS_PER_YEAR)

    DAY-COUNT [COMBINED_YIELD_DAY_COUNT]: the juice leg rides the 7-calendar-day
    week already pinned by ``burn.net_juice_per_week`` ([NET_JUICE_TIME_BASE]); the
    dividend leg is a QUOTED ANNUAL RATE divided by 52, the conventional
    weekly-equivalent reading of one. The two conventions differ by ~1.8% OF THE
    DIVIDEND LEG (52 vs 365/7 = 51.07 weeks) — immaterial against the floors, and
    pinned by test_dividend_profile so it cannot drift.

    Components stay separable in the return value so the UI can show what is juice
    and what is dividend rather than one blended figure. A None juice leg gives a
    None combined (a name we can't price is not a name we can rank); a None
    dividend leg is treated as a genuine zero contribution but flagged
    ``dividend_known: False`` so a fundamentals outage is never displayed as a
    confident 0. PURE."""
    div_wk = None
    if annual_dividend_yield_pct is not None:
        div_wk = round(annual_dividend_yield_pct / config.DIVIDEND_WEEKS_PER_YEAR, 4)
    combined = None
    if juice_weekly_pct is not None:
        combined = round(juice_weekly_pct + (div_wk or 0.0), 4)
    return {
        "combined_weekly_yield_pct": combined,
        "juice_weekly_pct": juice_weekly_pct,
        "dividend_weekly_pct": div_wk,
        "annual_dividend_yield_pct": annual_dividend_yield_pct,
        "dividend_known": annual_dividend_yield_pct is not None,
        "weeks_per_year": config.DIVIDEND_WEEKS_PER_YEAR,
    }


def juice_clears_slippage(weekly_extrinsic_per_share: float | None,
                          roundtrip_haircut_pct: float | None = None) -> dict:
    """Does the weekly extrinsic still pay after the estimated round-trip spread
    cost? A CFM cycle crosses the spread twice (sell the short, buy it back), so a
    name whose weekly time premium can't survive its own crossing is a
    buy-and-hold, not a CFM position.

    Expressed as an ABSOLUTE per-share floor on the post-haircut extrinsic. A
    proportional haircut can never flip the sign of a positive yield, so a
    "> 0 after slippage" test written proportionally would be vacuous and could
    never fire; ``config.MIN_WEEKLY_EXTRINSIC_AFTER_SLIPPAGE_PS`` is what makes it
    a real test.

    ``roundtrip_haircut_pct`` is the two-crossing haircut in PERCENT of premium
    (``slippage.report(state)["roundtrip_haircut_pct"]`` when a caller has state;
    the assumed default otherwise, so this stays PURE). Unknown extrinsic -> no
    verdict (``clears: None``), never a false trip."""
    if roundtrip_haircut_pct is None:
        roundtrip_haircut_pct = config.ASSUMED_SLIPPAGE_PCT * 2 * 100
    if weekly_extrinsic_per_share is None:
        return {"clears": None, "after_slippage_per_share": None,
                "roundtrip_haircut_pct": roundtrip_haircut_pct,
                "floor_per_share": config.MIN_WEEKLY_EXTRINSIC_AFTER_SLIPPAGE_PS}
    after = weekly_extrinsic_per_share * (1 - roundtrip_haircut_pct / 100.0)
    return {
        "clears": bool(after > config.MIN_WEEKLY_EXTRINSIC_AFTER_SLIPPAGE_PS),
        "after_slippage_per_share": round(after, 4),
        "roundtrip_haircut_pct": roundtrip_haircut_pct,
        "floor_per_share": config.MIN_WEEKLY_EXTRINSIC_AFTER_SLIPPAGE_PS,
    }


def shadow_floor(profile: str | None,
                 juice_weekly_pct: float | None,
                 annual_dividend_yield_pct: float | None = None,
                 weekly_extrinsic_per_share: float | None = None,
                 roundtrip_haircut_pct: float | None = None) -> dict:
    """The profile-aware income floor, evaluated in SHADOW — logged, displayed,
    and carrying ZERO blocking authority.

    ``JUICE_ENGINE``        — juice alone vs ``config.SHARES_JUICE_FLOOR_PCT``
                              (0.75%/wk, share-denominated; PROPOSED_DEFAULT).
    ``DIVIDEND_COMPOUNDER`` — the COMBINED weekly-equivalent yield vs
                              ``config.COMBINED_YIELD_FLOOR_WK`` (0.5%/wk), PLUS a
                              sub-floor that the juice component alone must still
                              clear the estimated spread cost. A compounder that
                              fails only the sub-floor is reported with
                              ``JUICE_BELOW_SLIPPAGE`` in its reasons.

    ``pass`` is None (not False) when the inputs can't be priced — an unmeasurable
    name is unmeasured, never a recorded failure. PURE; the caller supplies the
    yields. Returns an observation, NOT a block: callers must keep this out of the
    list passed to ``compose_row_verdict``."""
    import income_profile
    profile = income_profile.normalize(profile)
    parts = combined_weekly_yield(juice_weekly_pct, annual_dividend_yield_pct)
    slip = juice_clears_slippage(weekly_extrinsic_per_share, roundtrip_haircut_pct)

    reasons: list[str] = []
    if profile == income_profile.DIVIDEND_COMPOUNDER:
        floor, measured, basis = (config.COMBINED_YIELD_FLOOR_WK,
                                  parts["combined_weekly_yield_pct"], "combined")
        if measured is not None and measured < floor:
            reasons.append("COMBINED_BELOW_FLOOR")
        # The sub-floor is a SEPARATE reason, so a name that clears the combined
        # bar purely on its dividend while its juice can't pay for its own
        # crossing is still visibly flagged.
        if slip["clears"] is False:
            reasons.append("JUICE_BELOW_SLIPPAGE")
    else:
        floor, measured, basis = (config.SHARES_JUICE_FLOOR_PCT,
                                  parts["juice_weekly_pct"], "juice")
        if measured is not None and measured < floor:
            reasons.append("JUICE_BELOW_FLOOR")
    # An unmeasurable name is UNMEASURED (None), never a recorded failure.
    passed = None if measured is None else not reasons

    return {
        "profile": profile,
        "basis": basis,
        "floor_pct": floor,
        "measured_pct": measured,
        "pass": passed,
        "reasons": reasons,
        # SHADOW is a literal, not a flag read from config: there is no switch that
        # can make this blocking, and a reader (UI, log, test) can rely on that.
        "shadow": True,
        "blocking": False,
        **{k: v for k, v in parts.items() if k != "juice_weekly_pct"},
        "slippage": slip,
    }
