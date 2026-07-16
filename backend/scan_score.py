"""Composite scan SCORE (0–10) — the RANK tier, SHADOW ONLY.

A single quality number over the NON-BLOCKING inputs already computed for a scan
row: sector strength, base maturity (stage + count), institutional-flow grade, ATR
posture, distance from MA21, net juice/week, and the two-speed RS state. It answers
"of the names that are *entrable*, which are the strongest setups" — a RANK, not a
gate.

**ZERO AUTHORITY.** SCORE is a pure function of already-computed row fields and is
read by NOBODY in the decision path:
  * it is NOT an input to ``scan_verdict.compose_verdict`` (which takes only
    regime/SYM colors + the two structure enums),
  * it does NOT enter the entry gate, ``/api/scan/ready`` selection, sizing, or the
    recommendation/refresh pipeline,
  * it makes NO data fetches — every input is passed in.
It is displayed (a column) and logged (the calibration dataset). Nothing else.

Every weight and sub-score threshold here is ``PROPOSED_DEFAULT`` — the whole point
of shadowing SCORE is to gather the data that calibrates these before any of it
could ever graduate to authority.

Pure: no I/O, no clock.
"""
from __future__ import annotations

import rs_state
import structure_classifier as sclf

# ---------------------------------------------------------------------------
# Component weights (sum = 10.0) — PROPOSED_DEFAULT.
# ---------------------------------------------------------------------------
W_INST_FLOW = 2.0          # PROPOSED_DEFAULT — accumulation is the strongest tell
W_BASE = 2.0               # PROPOSED_DEFAULT — where the name sits in its cycle
W_RS_STATE = 1.5           # PROPOSED_DEFAULT — leading + improving vs its sector
W_NET_JUICE = 1.5          # PROPOSED_DEFAULT — the income the setup actually pays
W_SECTOR = 1.0             # PROPOSED_DEFAULT — tailwind from a strong sector
W_ATR = 1.0                # PROPOSED_DEFAULT — contracting vol is a CFM positive
W_DIST_MA21 = 1.0          # PROPOSED_DEFAULT — near the MA (not extended) is best
_TOTAL_WEIGHT = (W_INST_FLOW + W_BASE + W_RS_STATE + W_NET_JUICE
                 + W_SECTOR + W_ATR + W_DIST_MA21)   # 10.0

# ---------------------------------------------------------------------------
# Sub-score maps (each returns a 0..1 quality) — PROPOSED_DEFAULT.
# ---------------------------------------------------------------------------
_INST_FLOW_SUB = {          # PROPOSED_DEFAULT — ACCUM > EARLY_INT (spec ordering)
    sclf.InstFlow.ACCUMULATING: 1.0,
    sclf.InstFlow.EARLY_INTEREST: 0.6,
    sclf.InstFlow.NO_INTEREST: 0.3,
    sclf.InstFlow.DISTRIBUTING: 0.0,
    sclf.InstFlow.INSUFFICIENT_DATA: 0.0,
}
_BASE_STAGE_SUB = {         # PROPOSED_DEFAULT — only EARLY_ADVANCE is READY-eligible
    sclf.BaseStage.EARLY_ADVANCE: 1.0,
    sclf.BaseStage.BASING: 0.5,
    sclf.BaseStage.LATE_ADVANCE: 0.5,
    sclf.BaseStage.TOPPING: 0.1,
    sclf.BaseStage.DECLINING: 0.0,
    sclf.BaseStage.INSUFFICIENT_DATA: 0.0,
}
_RS_STATE_SUB = {           # PROPOSED_DEFAULT — RISING best, FALLING worst
    rs_state.RISING: 1.0,
    rs_state.TURNING: 0.6,
    rs_state.FADING: 0.3,
    rs_state.FALLING: 0.0,
}

# Distance-from-MA21 posture (percent). PROPOSED_DEFAULT.
DIST_IDEAL_MAX = 5.0        # PROPOSED_DEFAULT — up to +5% above MA21 is the sweet spot
DIST_EXTENDED = 15.0        # PROPOSED_DEFAULT — >= +15% above MA21 scores 0 (extended)
# ATR posture (atr / atr_5ema). PROPOSED_DEFAULT.
ATR_CONTRACTING = 1.0       # PROPOSED_DEFAULT — <= 1.0 is contracting/flat (ideal)
ATR_EXPANDING = 1.2         # PROPOSED_DEFAULT — >= 1.2 scores 0 (volatility blowing out)
# Net juice/week (% of LEAP cost). PROPOSED_DEFAULT.
JUICE_FULL = 3.8           # PROPOSED_DEFAULT — net juice/wk at/above this scores 1.0 (~2x the ~1.9% bar)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _base_sub(base_stage: str | None, base_count) -> float:
    """Stage-dominant maturity, with a small count nudge: a fresh EARLY_ADVANCE
    (0–2 prior bases) is preferred to a many-based one (later in its run)."""
    sub = _BASE_STAGE_SUB.get(base_stage, 0.0)
    if base_stage == sclf.BaseStage.EARLY_ADVANCE and base_count is not None:
        # 0–2 bases keep full credit; each extra base past 2 shaves a little.
        sub = max(0.5, sub - 0.1 * max(0, int(base_count) - 2))
    return _clamp01(sub)


def _sector_sub(sector_rs1m: float | None) -> float:
    """Sector strength from its RS1M vs SPY (%): 0 -> neutral 0.5, scaling to 1.0
    by +5% and to 0.0 by −5%. None -> neutral 0.5 (never penalize missing data)."""
    if sector_rs1m is None:
        return 0.5
    return _clamp01(0.5 + sector_rs1m / 10.0)


def _atr_sub(atr_momentum: float | None) -> float:
    """Contracting/flat vol (<= 1.0) is ideal (1.0); expanding (>= 1.2) scores 0.
    None -> neutral 0.5."""
    if atr_momentum is None:
        return 0.5
    if atr_momentum <= ATR_CONTRACTING:
        return 1.0
    if atr_momentum >= ATR_EXPANDING:
        return 0.0
    return _clamp01(1.0 - (atr_momentum - ATR_CONTRACTING) / (ATR_EXPANDING - ATR_CONTRACTING))


def _dist_ma21_sub(pct_above_ma21: float | None) -> float:
    """Near the MA (0..+5%) is best (1.0); +15% or more is extended (0.0); below
    the MA is a weaker 0.5 (not the right spot but not extended). None -> 0.5."""
    if pct_above_ma21 is None:
        return 0.5
    if pct_above_ma21 < 0:
        return 0.5
    if pct_above_ma21 <= DIST_IDEAL_MAX:
        return 1.0
    if pct_above_ma21 >= DIST_EXTENDED:
        return 0.0
    return _clamp01(1.0 - (pct_above_ma21 - DIST_IDEAL_MAX) / (DIST_EXTENDED - DIST_IDEAL_MAX))


def _juice_sub(net_juice_weekly_pct: float | None) -> float:
    """Net juice/week scaled linearly to JUICE_FULL. None or negative -> 0."""
    if net_juice_weekly_pct is None or net_juice_weekly_pct <= 0:
        return 0.0
    return _clamp01(net_juice_weekly_pct / JUICE_FULL)


def compute_score(*, inst_flow: str | None, base_stage: str | None, base_count=None,
                  rs_state_value: str | None = None, sector_rs1m: float | None = None,
                  atr_momentum: float | None = None, pct_above_ma21: float | None = None,
                  net_juice_weekly_pct: float | None = None) -> dict:
    """The composite SCORE (0–10) + its component parts, from already-computed row
    inputs. PURE. ``parts`` are the raw 0..1 sub-scores (for the calibration log so
    weight sensitivity is measurable later, per the Phase 0 Q9 gaps)."""
    parts = {
        "inst_flow": _INST_FLOW_SUB.get(inst_flow, 0.0),
        "base": _base_sub(base_stage, base_count),
        "rs_state": _RS_STATE_SUB.get(rs_state_value, 0.0) if rs_state_value is not None else 0.0,
        "sector": _sector_sub(sector_rs1m),
        "atr": _atr_sub(atr_momentum),
        "dist_ma21": _dist_ma21_sub(pct_above_ma21),
        "net_juice": _juice_sub(net_juice_weekly_pct),
    }
    weighted = (parts["inst_flow"] * W_INST_FLOW
                + parts["base"] * W_BASE
                + parts["rs_state"] * W_RS_STATE
                + parts["net_juice"] * W_NET_JUICE
                + parts["sector"] * W_SECTOR
                + parts["atr"] * W_ATR
                + parts["dist_ma21"] * W_DIST_MA21)
    # _TOTAL_WEIGHT is 10.0, so the weighted sum is already on a 0–10 scale.
    return {"score": round(weighted / _TOTAL_WEIGHT * 10.0, 2),
            "parts": {k: round(v, 3) for k, v in parts.items()}}
