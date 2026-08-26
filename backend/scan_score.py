"""The scan RANKER — "of the names that are eligible, which is the best entry today?"

This module used to be a SHADOW composite with zero authority. It now carries
RANKING authority, deliberately and by reviewed change: the scan is a thin hard
floor (``scan_verdict``) plus this ranker, and everything the old serial filter
vetoed on that did not mirror an exit rule was moved here.

**RANKING AUTHORITY IS NOT VETO AUTHORITY.** Nothing in this module may ever be
appended to the ``blocks`` list that ``scan_verdict.compose`` reads. A rank orders
the eligible; it cannot make a name ineligible, and a name scoring 0.0 is still
ELIGIBLE with a rank of 0.0 — never BLOCKED. That invariant survives this change
unchanged and is asserted directly by ``test_juice_capacity`` and
``test_scan_score``.

WHAT RANKS (§1.2, exhaustive)
-----------------------------
Sector relative strength, sector breadth, sector ATR expansion, the per-name
four-light vote, RS3M-vs-SPY MAGNITUDE above zero, ATR% of price, ATR vs its
5-EMA, extension above MA21, structure entrability, and the shadow features
(income floor, chart structure, trailing juice capacity). None of them vetoes.
None may be reintroduced as a floor without a separate reviewed change.

NORMALIZATION
-------------
Every input is mapped to a 0..1 sub-score BEFORE weighting, because the raw units
are not comparable — ATR extension is in ATR multiples, juice is a percent per
week, chart structure is a count out of a VARIABLE denominator, and capacity is a
percent that may instead be the string ``INSUFFICIENT_HISTORY``. Weighting raw
units would silently let whichever input happened to have the largest numbers
dominate the order.

A MISSING input scores the NEUTRAL 0.5, never 0.0. "We could not measure this" and
"this is bad" are opposite facts, and scoring absence as badness would rebuild the
multiplicative collapse this redesign removed — a name with short history would
sink to the bottom of every list regardless of its chart.

Pure: no I/O, no clock, no provider access. Every input is passed in.
"""
from __future__ import annotations

import structure_classifier as sclf

# ---------------------------------------------------------------------------
# THE WEIGHTS TABLE — one table, one module. Every weight is PROPOSED_DEFAULT.
#
# These are guesses. They are the whole reason the gate-rejection telemetry and
# the scan rejection log keep recording per-input contributions: a weight can only
# be calibrated against the forward returns of the names it ranked, and that
# dataset accrues one trading day at a time.
# ---------------------------------------------------------------------------
W_INST_FLOW = 2.0      # PROPOSED_DEFAULT — accumulation is the strongest tell
W_BASE = 2.0           # PROPOSED_DEFAULT — where the name sits in its cycle
W_STRUCTURE = 1.5      # PROPOSED_DEFAULT — structure entrability, demoted from a veto
W_EXTENSION = 1.5      # PROPOSED_DEFAULT — extension above MA21, demoted from the L4 veto
W_LIGHTS = 1.5         # PROPOSED_DEFAULT — the four-light vote, demoted from the L3 veto
W_RS_MAGNITUDE = 1.0   # PROPOSED_DEFAULT — RS3M vs SPY ABOVE zero (below zero vetoes)
W_SECTOR = 1.0         # PROPOSED_DEFAULT — tailwind from a strong sector
W_ATR = 1.0            # PROPOSED_DEFAULT — contracting vol is a CFM positive
W_CHART_STRUCTURE = 1.0  # PROPOSED_DEFAULT — shadow chart-structure metrics
W_CAPACITY = 0.5       # PROPOSED_DEFAULT — trailing juice capacity (thin history is common)

_WEIGHTS = {
    "inst_flow": W_INST_FLOW,
    "base": W_BASE,
    "structure": W_STRUCTURE,
    "extension": W_EXTENSION,
    "lights": W_LIGHTS,
    "rs_magnitude": W_RS_MAGNITUDE,
    "sector": W_SECTOR,
    "atr": W_ATR,
    "chart_structure": W_CHART_STRUCTURE,
    "capacity": W_CAPACITY,
}
_TOTAL_WEIGHT = sum(_WEIGHTS.values())   # 13.0

# Net juice/week is NOT an additive weight. It is a MULTIPLICATIVE viability factor
# (see `compute_score`): a chart-quality rank, then scaled by whether the setup
# actually pays. A beautiful chart that pays nothing ranks near zero. This is the
# single most important shape in the module — with no income floor left in the veto
# set, the viability factor is the ONLY thing keeping an unpayable name off the top
# of the list, and it is a rank, so it can still be entered deliberately.
JUICE_TARGET_WK = 1.5   # PROPOSED_DEFAULT — net juice/wk (%) for full viability

# ---------------------------------------------------------------------------
# Sub-score maps (each returns a 0..1 quality) — all PROPOSED_DEFAULT.
# ---------------------------------------------------------------------------
_INST_FLOW_SUB = {
    sclf.InstFlow.ACCUMULATING: 1.0,
    sclf.InstFlow.EARLY_INTEREST: 0.6,
    sclf.InstFlow.NO_INTEREST: 0.3,
    sclf.InstFlow.DISTRIBUTING: 0.0,
    sclf.InstFlow.INSUFFICIENT_DATA: 0.5,   # unmeasured -> neutral, never 0
}
_BASE_STAGE_SUB = {
    sclf.BaseStage.EARLY_ADVANCE: 1.0,
    sclf.BaseStage.BASING: 0.5,
    sclf.BaseStage.LATE_ADVANCE: 0.5,
    sclf.BaseStage.TOPPING: 0.1,
    sclf.BaseStage.DECLINING: 0.0,
    sclf.BaseStage.INSUFFICIENT_DATA: 0.5,  # unmeasured -> neutral, never 0
}
# Structure entrability, demoted from the Level-3.5 veto. TOPPING/DECLINING/
# DISTRIBUTING cells used to BLOCK; they now rank at the bottom and remain
# enterable. The ordering is the old grid's severity, read as quality.
_ENTRABILITY_SUB = {
    sclf.Entrability.READY: 1.0,
    sclf.Entrability.CAUTION: 0.7,
    sclf.Entrability.WATCH: 0.4,
    sclf.Entrability.BLOCKED: 0.0,
}

# Extension above MA21, in ATR units — the SAME value `scan_verdict.route` keys
# off, so the rank and the route can never disagree about how extended a name is.
# At or below the MA is ideal; the old Level-4 veto bar is the midpoint of the
# decay rather than a cliff; twice that bar scores zero.
EXT_IDEAL_MAX = 0.0     # PROPOSED_DEFAULT — at/below MA21 scores 1.0
EXT_ZERO = 3.0          # PROPOSED_DEFAULT — >= 3 ATR above MA21 scores 0.0
# ATR posture (atr / atr_5ema).
ATR_CONTRACTING = 1.0   # PROPOSED_DEFAULT — <= 1.0 is contracting/flat (ideal)
ATR_EXPANDING = 1.2     # PROPOSED_DEFAULT — >= 1.2 scores 0 (volatility blowing out)
# RS3M vs SPY MAGNITUDE above zero. Below zero is a VETO, so this band only ever
# sees non-negative values; the old +5% growth-leader bar is now the point of full
# credit rather than the point of admission.
RS_FULL_CREDIT = 5.0    # PROPOSED_DEFAULT — RS3M vs SPY at/above +5% scores 1.0


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _linear(x: float | None, zero_at: float, one_at: float) -> float:
    """Linear 0..1 ramp between two points, clamped. None -> neutral 0.5."""
    if x is None:
        return 0.5
    if zero_at == one_at:
        return 0.5
    return _clamp01((x - zero_at) / (one_at - zero_at))


def _base_sub(base_stage: str | None, base_count) -> float:
    """Stage-dominant maturity with a small count nudge: a fresh EARLY_ADVANCE
    (0-2 prior bases) is preferred to a many-based one (later in its run)."""
    sub = _BASE_STAGE_SUB.get(base_stage, 0.5)
    if base_stage == sclf.BaseStage.EARLY_ADVANCE and base_count is not None:
        sub = max(0.5, sub - 0.1 * max(0, int(base_count) - 2))
    return _clamp01(sub)


def _sector_sub(sector_rs1m: float | None, sector_breadth: float | None = None,
                sector_atr_expanding: bool | None = None) -> float:
    """Sector tailwind, demoted from the Level-2 veto: RS1M vs SPY as the base
    read, nudged by breadth and penalised for expanding ATR. All three used to be
    veto legs; together they are now one weighted input."""
    if sector_rs1m is None and sector_breadth is None:
        return 0.5
    rs = 0.5 if sector_rs1m is None else _clamp01(0.5 + sector_rs1m / 10.0)
    breadth = 0.5 if sector_breadth is None else _linear(sector_breadth, 30.0, 70.0)
    sub = 0.65 * rs + 0.35 * breadth
    if sector_atr_expanding:
        sub -= 0.1     # PROPOSED_DEFAULT — expanding sector vol is a mild negative
    return _clamp01(sub)


def _atr_sub(atr_momentum: float | None) -> float:
    """Contracting/flat vol (<= 1.0) is ideal; expanding (>= 1.2) scores 0."""
    if atr_momentum is None:
        return 0.5
    if atr_momentum <= ATR_CONTRACTING:
        return 1.0
    if atr_momentum >= ATR_EXPANDING:
        return 0.0
    return _clamp01(1.0 - (atr_momentum - ATR_CONTRACTING)
                    / (ATR_EXPANDING - ATR_CONTRACTING))


def _extension_sub(extension_atr: float | None) -> float:
    """Extension above MA21 in ATR units. At or below the MA is ideal (1.0),
    decaying to 0 by EXT_ZERO. Below the MA is NOT penalised — a name that has
    pulled back to its mean is exactly the entry this strategy wants."""
    if extension_atr is None:
        return 0.5
    if extension_atr <= EXT_IDEAL_MAX:
        return 1.0
    return _clamp01(1.0 - (extension_atr - EXT_IDEAL_MAX) / (EXT_ZERO - EXT_IDEAL_MAX))


def _lights_sub(greens: int | None) -> float:
    """The four-light vote, demoted from the Level-3 veto. 4/4 used to be the ONLY
    passing state and 3/4 was a hard stop; the vote is now a linear quality read,
    which is the single largest source of newly-eligible names in this change."""
    if greens is None:
        return 0.5
    return _clamp01(int(greens) / 4.0)


def _rs_magnitude_sub(rs3m_vs_spy: float | None) -> float:
    """RS3M vs SPY ABOVE zero. Negative is a veto (`scan_verdict`), so a negative
    value reaching here scores 0 rather than being re-blocked — the veto owns that
    decision and a rank must not duplicate it."""
    if rs3m_vs_spy is None:
        return 0.5
    if rs3m_vs_spy <= 0:
        return 0.0
    return _linear(rs3m_vs_spy, 0.0, RS_FULL_CREDIT)


def _chart_structure_sub(structure_score, structure_score_of) -> float:
    """The shadow chart-structure metrics, normalized against their own VARIABLE
    denominator — the raw count is not comparable across names because a name with
    fewer measurable metrics has a smaller ceiling."""
    if not structure_score_of:
        return 0.5
    return _clamp01(float(structure_score or 0) / float(structure_score_of))


def _capacity_sub(capacity_pct, floor_pct: float | None) -> float:
    """Trailing juice CAPACITY vs the shadow income floor.

    ``capacity_pct`` may be the ``INSUFFICIENT_HISTORY`` sentinel string rather
    than a number — "not measured yet" and "yields nothing" are opposite facts, so
    the sentinel maps to the NEUTRAL 0.5 and is never coerced to 0. A name at the
    floor scores 0.5; twice the floor scores 1.0.
    """
    if not isinstance(capacity_pct, (int, float)) or not floor_pct:
        return 0.5
    return _clamp01(float(capacity_pct) / (2.0 * float(floor_pct)))


def compute_score(*, inst_flow: str | None = None, base_stage: str | None = None,
                  base_count=None, entrability: str | None = None,
                  extension_atr: float | None = None, stock_greens: int | None = None,
                  rs3m_vs_spy: float | None = None, sector_rs1m: float | None = None,
                  sector_breadth: float | None = None,
                  sector_atr_expanding: bool | None = None,
                  atr_momentum: float | None = None,
                  structure_score=None, structure_score_of=None,
                  capacity_pct=None, shadow_floor_pct: float | None = None,
                  net_juice_weekly_pct: float | None = None) -> dict:
    """The rank score (0-10) plus every per-input contribution. PURE.

    ``parts`` are the raw 0..1 sub-scores and ``contributions`` are those sub-scores
    times their weights, so a rank is EXPLAINABLE without re-running anything: a
    reader can see which input earned or cost a name its position. That is the whole
    reason both are returned rather than just the total.
    """
    parts = {
        "inst_flow": _INST_FLOW_SUB.get(inst_flow, 0.5),
        "base": _base_sub(base_stage, base_count),
        "structure": _ENTRABILITY_SUB.get(entrability, 0.5),
        "extension": _extension_sub(extension_atr),
        "lights": _lights_sub(stock_greens),
        "rs_magnitude": _rs_magnitude_sub(rs3m_vs_spy),
        "sector": _sector_sub(sector_rs1m, sector_breadth, sector_atr_expanding),
        "atr": _atr_sub(atr_momentum),
        "chart_structure": _chart_structure_sub(structure_score, structure_score_of),
        "capacity": _capacity_sub(capacity_pct, shadow_floor_pct),
    }
    contributions = {k: round(parts[k] * w, 3) for k, w in _WEIGHTS.items()}
    quality = sum(contributions.values()) / _TOTAL_WEIGHT * 10.0

    # The multiplicative VIABILITY factor. With no income floor in the veto set,
    # this is the only thing that keeps a name which cannot pay off the top of the
    # ranked list — and because it is a factor on a RANK, such a name is still
    # ELIGIBLE and can still be entered deliberately. Missing or non-positive
    # income clamps it to zero.
    viability = (0.0 if (net_juice_weekly_pct is None or net_juice_weekly_pct <= 0)
                 else min(net_juice_weekly_pct / JUICE_TARGET_WK, 1.0))
    parts["juice_viability"] = round(viability, 3)
    return {"score": round(quality * viability, 2),
            "score_quality": round(quality, 2),
            "parts": {k: round(v, 3) for k, v in parts.items()},
            "contributions": contributions}


def rank(rows: list[dict] | None) -> list[dict]:
    """Order ELIGIBLE rows best-first, stamping each with its 1-based ``rank``.

    Rows are expected to carry ``ticker`` and ``score``; a row with no score sorts
    as 0.0 rather than being dropped, because a missing rank input must never
    silently remove a name the veto set admitted.

    **Ties break deterministically by symbol**, so two runs over identical inputs
    produce byte-identical ordering. A scan that reordered on re-run would make
    "the #1 name" a function of dict iteration order, and the whole point of
    ranking is that the order is a claim.

    Returns NEW dicts; the inputs are never mutated.
    """
    ordered = sorted(list(rows or []),
                     key=lambda r: (-(r.get("score") or 0.0),
                                    str(r.get("ticker") or "")))
    return [{**r, "rank": i} for i, r in enumerate(ordered, start=1)]
