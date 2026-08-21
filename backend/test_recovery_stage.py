"""BaseStage.RECOVERING — the V-shaped-recovery fix (2026-08-21).

A decline and a rally of similar magnitude inside one 150-bar least-squares fit
net to ~zero, so a violent V read as flat and fell through to BASING. The short
window disambiguates it. See AUDIT_BASING_RECOVERY_PHASE0.md.

The load-bearing property throughout: RECOVERING is a LABELING change, not a
gate one. It must be WATCH-only, never bench, and must not move any verdict.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-recovery-"))

import scan_diff                              # noqa: E402
import scan_score                             # noqa: E402
import scan_triggers as st                    # noqa: E402
import scan_verdict as sv                     # noqa: E402
import structure_classifier as sc             # noqa: E402
from structure_classifier import BaseStage, Entrability, InstFlow   # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "structure")


def _load(name):
    return pd.read_parquet(os.path.join(FIX, f"{name}.parquet"))


def _frame(closes, vol=1.2e6, wiggle=0.008):
    c = np.asarray(closes, dtype=float)
    idx = pd.bdate_range("2025-01-01", periods=len(c))
    return pd.DataFrame({"Open": np.concatenate([[c[0]], c[:-1]]),
                         "High": c * (1 + wiggle), "Low": c * (1 - wiggle),
                         "Close": c, "Volume": np.full(len(c), vol)}, index=idx)


# ===========================================================================
# 1. The canonical regression pin
# ===========================================================================
def test_v_shape_recovery_is_not_basing():
    """THE regression pin for this change. Synthetic reproduction of the GDDY
    2026-08-21 observation (see the fixture docstring — real bars for that date
    cannot be fetched in the test environment). Any future classifier edit that
    re-labels this chart BASING must fail here."""
    df = _load("recovery_v_shape")
    sig = sc._signals(df)

    # The signature: flat over 150 bars, strongly rising over 40, under the 200-day.
    assert -sc.SLOPE_FALLING_PCT > sig["slope_pct"] > sc.SLOPE_FALLING_PCT
    assert sig["slope_pct"] < sc.SLOPE_RISING_PCT          # inside the flat band
    assert sig["slope_pct_short"] > sc.SHORT_SLOPE_RISING_PCT
    assert sig["above_sma200"] is False
    # ...and ~0.7% below SMA200, the measurement the observation reported.
    assert (sig["price"] / sig["sma200"] - 1) * 100 == pytest.approx(-0.7, abs=0.5)
    assert sig["above_sma50"] is True                       # above a rising SMA50

    assert sc._base_stage(sig) == BaseStage.RECOVERING
    assert sc.classify_symbol(df)[0] == BaseStage.RECOVERING


def test_the_recovery_stays_watch_only_and_off_the_bench():
    """The consequence contract: a clearer label, not a better verdict."""
    base, inst = sc.classify_symbol(_load("recovery_v_shape"))
    assert sc.structure_entrability(base, inst) == Entrability.WATCH

    composed = sv.compose_verdict("green", "green", base, inst)
    rv = st.compose_row_verdict(composed, [])
    assert rv["verdict"] == sv.WATCH
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False


# ===========================================================================
# 2. RECOVERING did not swallow legitimate bases
# ===========================================================================
def test_a_true_base_is_still_basing():
    """Same below-200 position and the same geometry as the V fixture; the ONLY
    difference is that the short window sees no rally."""
    df = _load("flat_base_below_200")
    sig = sc._signals(df)
    assert sig["above_sma200"] is False
    assert abs(sig["slope_pct"]) < sc.SLOPE_RISING_PCT          # flat long
    assert sig["slope_pct_short"] <= sc.SHORT_SLOPE_RISING_PCT  # flat short
    assert sc._base_stage(sig) == BaseStage.BASING


def test_the_short_window_is_the_only_difference():
    """Drive _base_stage directly: identical below-200 flat inputs, short slope
    on either side of the band, nothing else changed."""
    def stage(short):
        return sc._base_stage({"bars": 250, "slope_pct": -2.0, "above_sma200": False,
                               "above_sma50": True, "pct_above_sma50": 5.0,
                               "atr_posture": 0.95, "base_count": 0, "roc_long": -5.0,
                               "slope_pct_short": short})
    assert stage(sc.SHORT_SLOPE_RISING_PCT + 0.1) == BaseStage.RECOVERING
    assert stage(sc.SHORT_SLOPE_RISING_PCT) == BaseStage.BASING     # boundary: not >
    assert stage(0.0) == BaseStage.BASING
    assert stage(-30.0) == BaseStage.BASING
    assert stage(None) == BaseStage.BASING                          # missing -> base


# ===========================================================================
# 3. Isolation — the long window still owns every other claim
# ===========================================================================
def test_the_short_window_never_reaches_the_advance_or_topping_claims():
    """A rising LONG slope must produce the same answer whatever the short
    window says, and likewise for the topping tests. The short slope is bound to
    a separate local consulted only in the below-200 flat region."""
    def stage(long_slope, above200, above50, short, **over):
        sig = {"bars": 250, "slope_pct": long_slope, "above_sma200": above200,
               "above_sma50": above50, "pct_above_sma50": 2.0, "atr_posture": 0.95,
               "base_count": 0, "roc_long": 5.0, "slope_pct_short": short}
        sig.update(over)
        return sc._base_stage(sig)

    for short in (-50.0, 0.0, 50.0, None):
        # advance claims (long slope rising)
        assert stage(20.0, True, True, short) == BaseStage.EARLY_ADVANCE
        assert stage(20.0, True, True, short, base_count=5) == BaseStage.LATE_ADVANCE
        assert stage(20.0, False, True, short) == BaseStage.EARLY_ADVANCE
        # topping claims (above 200, stalled)
        assert stage(0.0, True, False, short) == BaseStage.TOPPING
        assert stage(0.0, True, True, short, roc_long=50.0) == BaseStage.TOPPING
        assert stage(0.0, True, True, short, atr_posture=1.5) == BaseStage.TOPPING
        # declining (falling below the 200-day)
        assert stage(-20.0, False, True, short) == BaseStage.DECLINING
        # path A base
        assert stage(0.0, True, True, short) == BaseStage.BASING


def test_path_a_fixtures_are_unchanged():
    """Every committed fixture whose long slope sits outside the flat band, or
    which is above the 200-day, must classify exactly as before."""
    assert sc.classify_symbol(_load("topping_distribution"))[0] == BaseStage.TOPPING
    assert sc.classify_symbol(_load("early_advance_accum"))[0] == BaseStage.EARLY_ADVANCE
    assert sc.classify_symbol(_load("early_advance_extended"))[0] == BaseStage.EARLY_ADVANCE
    assert sc.classify_symbol(_load("turning_recovery"))[0] == BaseStage.EARLY_ADVANCE


def test_the_short_slope_is_not_a_sufficiency_input():
    """It must never force INSUFFICIENT_DATA — MIN_BARS_BASE already guarantees
    it is computable, and adding it to the guard would be a trap if the window
    were ever lengthened."""
    sig = {"bars": 250, "slope_pct": -2.0, "above_sma200": False, "above_sma50": True,
           "pct_above_sma50": 5.0, "atr_posture": 0.95, "base_count": 0,
           "roc_long": -5.0, "slope_pct_short": None}
    assert sc._base_stage(sig) != BaseStage.INSUFFICIENT_DATA


def test_the_dead_declining_branch_is_gone():
    """Path B's `if falling: return DECLINING` was unreachable — reaching it
    required below-200, which the earlier guard already reduced to `not falling`
    (audit §0.A). Nothing below the 200-day with a flat slope may read DECLINING."""
    for short in (-50.0, 0.0, 50.0):
        for slope in (-7.9, 0.0, 7.9):
            out = sc._base_stage({"bars": 250, "slope_pct": slope, "above_sma200": False,
                                  "above_sma50": True, "pct_above_sma50": 5.0,
                                  "atr_posture": 0.95, "base_count": 0, "roc_long": 0.0,
                                  "slope_pct_short": short})
            assert out in (BaseStage.BASING, BaseStage.RECOVERING), (slope, short, out)


# ===========================================================================
# 4. Registration at every consumer the audit found
# ===========================================================================
@pytest.mark.parametrize("inst,expected", [
    (InstFlow.ACCUMULATING, Entrability.WATCH),
    (InstFlow.EARLY_INTEREST, Entrability.WATCH),
    (InstFlow.NO_INTEREST, Entrability.WATCH),
    (InstFlow.DISTRIBUTING, Entrability.BLOCKED),
    (InstFlow.INSUFFICIENT_DATA, Entrability.BLOCKED),
])
def test_recovering_entrability_row(inst, expected):
    assert sc.structure_entrability(BaseStage.RECOVERING, inst) == expected
    # Identical consequence shape to BASING, column for column.
    assert sc.structure_entrability(BaseStage.RECOVERING, inst) == \
        sc.structure_entrability(BaseStage.BASING, inst)


def test_no_stage_can_reach_ready_or_caution_by_being_unregistered():
    """READY/CAUTION are exact-equality gated on the advance stages, so the grid
    fails OPEN to WATCH. This is what makes a new label structurally safe — pin
    it directly so a future edit to the grid cannot quietly break it."""
    for inst in (InstFlow.ACCUMULATING, InstFlow.EARLY_INTEREST, InstFlow.NO_INTEREST):
        assert sc.structure_entrability("A_STAGE_THAT_DOES_NOT_EXIST", inst) == \
            Entrability.WATCH
    assert sc.structure_entrability("A_STAGE_THAT_DOES_NOT_EXIST",
                                    InstFlow.DISTRIBUTING) == Entrability.BLOCKED


def test_the_shadow_score_registers_recovering():
    """Unregistered it would score 0.0 — BELOW TOPPING's 0.1 — so a recovery
    would rank beneath a topping name. Equal to BASING on purpose: this change
    re-labels, it does not re-rank."""
    assert scan_score._BASE_STAGE_SUB[BaseStage.RECOVERING] == \
        scan_score._BASE_STAGE_SUB[BaseStage.BASING]
    assert scan_score._BASE_STAGE_SUB[BaseStage.RECOVERING] > \
        scan_score._BASE_STAGE_SUB[BaseStage.TOPPING]
    common = dict(inst_flow=InstFlow.EARLY_INTEREST, sector_rs1m=0.0,
                  atr_momentum=1.0, pct_above_ma21=3.0, net_juice_weekly_pct=2.0)
    assert scan_score.compute_score(base_stage=BaseStage.RECOVERING, **common)["score"] == \
        scan_score.compute_score(base_stage=BaseStage.BASING, **common)["score"]


def test_the_pipeline_entrant_alert_still_fires_for_a_recovery():
    """scan_diff keyed the entrant event off the literal "BASING"; the label
    split alone must not silently stop the alert (audit §0.D)."""
    def row(stage):
        return {"ticker": "GDDY", "verdict": "WATCH", "base_stage": stage,
                "inst_flow": "EARLY_INTEREST", "sector": "XLK"}
    prev = {"ticker": "GDDY", "verdict": "WATCH", "base_stage": "DECLINING",
            "inst_flow": "NO_INTEREST", "sector": "XLK"}
    for stage in ("BASING", "RECOVERING"):
        evs = scan_diff.diff_symbol(prev, row(stage))
        assert any(e["type"] == scan_diff.PIPELINE_ENTRANT for e in evs), stage
    # Still deduped: entrant yesterday -> no repeat today.
    assert not any(e["type"] == scan_diff.PIPELINE_ENTRANT
                   for e in scan_diff.diff_symbol(row("RECOVERING"), row("RECOVERING")))
    # And BASING -> RECOVERING is NOT a degradation.
    assert not any(e["type"] == scan_diff.DEGRADED
                   for e in scan_diff.diff_symbol(row("BASING"), row("RECOVERING")))


def test_recovering_is_registered_in_the_ui_maps():
    """Unregistered, the BASE column renders an em-dash indistinguishable from
    NO DATA and sorts last (audit §0.C)."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "frontend", "src", "components", "Scorecard.jsx"),
               encoding="utf-8").read()
    for block in ("BASE_LABELS", "BASE_TONE", "BASE_ORDER"):
        start = src.index(f"const {block}")
        assert "RECOVERING" in src[start:start + 400], block


# ===========================================================================
# 5. Structure duration — DERIVED, not counted
# ===========================================================================
def test_days_in_current_structure_is_derived_from_the_scan_log(tmp_path, monkeypatch):
    import scan_rejection_log as srl
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))

    def rec(day, stage, scan_id=None):
        srl.record_scan([{"ticker": "GDDY", "verdict": "WATCH", "base_stage": stage}],
                        day=day, scan_id=scan_id)

    rec("2026-08-17", "BASING")
    rec("2026-08-18", "RECOVERING")
    rec("2026-08-19", "RECOVERING")
    rec("2026-08-20", "RECOVERING", scan_id="a")
    rec("2026-08-20", "RECOVERING", scan_id="b")     # same-day re-scan counts once
    assert srl.days_in_current_structure("GDDY") == 3
    assert srl.structure_durations()["GDDY"] == {
        "stage": "RECOVERING", "days": 3, "since": "2026-08-18"}

    # A label change resets the run to 1.
    rec("2026-08-21", "EARLY_ADVANCE")
    assert srl.days_in_current_structure("GDDY") == 1
    assert srl.days_in_current_structure("NOSUCH") is None


def test_the_classifier_stayed_pure(tmp_path, monkeypatch):
    """The duration is derived precisely so `classify` keeps its contract: no
    I/O, no clock, and the SAME frame always returns the SAME answer. A per-run
    counter would have broken the prefix-causal replay the fixtures rely on."""
    df = _load("recovery_v_shape")
    first = sc.classify(df)
    for _ in range(3):
        assert sc.classify(df) == first
    # Prefix-causal: a prefix classifies as of its own last bar, unaffected by
    # anything after it.
    prefix = df.iloc[:250]
    assert sc.classify(prefix) == sc.classify(df.iloc[:250].copy())
    assert "days_in_current_structure" not in first      # no state leaked in


# ===========================================================================
# 6. The gate is untouched
# ===========================================================================
def test_xlk_july6_is_unaffected():
    """The classifier feeds context around the gate; assert no interaction."""
    fix = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regime")
    df = pd.read_parquet(os.path.join(fix, "xlk_july6_rollover.parquet"))
    # 207 bars -> below MIN_BARS_BASE, so INSUFFICIENT_DATA, exactly as before.
    assert sc.classify_symbol(df)[0] == BaseStage.INSUFFICIENT_DATA
    assert sc.structure_entrability(*sc.classify_symbol(df)) == Entrability.BLOCKED
    assert sv.compose_verdict("GREEN", "GREEN", "TOPPING", "ACCUMULATING")["verdict"] == "BLOCKED"
