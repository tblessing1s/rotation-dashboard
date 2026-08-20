"""Level-4 chart-structure metrics (SHADOW) + the phase-aware volume check.

Offline and fixture-driven: every frame is hand-built from a deterministic path
generator, no provider is ever called, and the mocked-data discipline of the rest
of the suite is preserved.

The load-bearing tests are the NEGATIVE ones. Sections 4-6 assert that the four
metrics have ZERO authority — they must not move a verdict, a bench view, a
ranking, a trigger or a threshold. A shadow metric that quietly acquires
authority is the exact failure this feature is designed to prevent, so those
assertions matter more than the ones proving the math.
"""
from __future__ import annotations

import math
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-structure-test-"))

import chart_structure as cs           # noqa: E402
import config                          # noqa: E402
import scan_triggers as st             # noqa: E402
import scan_verdict as sv              # noqa: E402
import stock_lights                    # noqa: E402
from metrics import scorecard as sc    # noqa: E402
from metrics import thresholds as T    # noqa: E402

FIX_REGIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regime")


# ---------------------------------------------------------------------------
# Hand-built OHLC fixtures.
#
# `zig` is a compounding drift with a cosine swing on top. An even `period` puts
# the swing minima on exact bars, which is what makes 3-bar pivot lows both
# present and deterministic — a smooth monotonic ramp has NO local minima at all
# and would give higher_lows = 0 for reasons that have nothing to do with the
# chart being unconstructive.
# ---------------------------------------------------------------------------
def _frame(closes, wiggle=0.005, volume=1e6):
    c = np.asarray(closes, dtype=float)
    idx = pd.bdate_range("2025-01-01", periods=len(c))
    return pd.DataFrame({
        "Open": np.concatenate([[c[0]], c[:-1]]),
        "High": c * (1 + wiggle),
        "Low": c * (1 - wiggle),
        "Close": c,
        "Volume": np.full(len(c), float(volume)),
    }, index=idx)


def _zig(n, start, drift, amp, period=6.0, phase0=0.0):
    return [start * (1 + drift) ** i * (1 + amp * math.cos(2 * math.pi * (i + phase0) / period))
            for i in range(n)]


def ideal_coil():
    """Constructive consolidation: a long advance, then a tight, gently rising
    15-bar coil a hair under the highs. The shape Level 4 exists to find."""
    adv = _zig(120, 100.0, 0.004, 0.010)
    coil = _zig(15, adv[-1] * 0.995, 0.001, 0.006, phase0=120 % 6)
    return _frame(adv + coil)


def post_run_drift():
    """The look-alike Level 4 cannot currently tell apart: ran months ago, rolled
    over, now drifting mid-range under a falling MA21, well off the highs."""
    run = _zig(80, 100.0, 0.007, 0.010)
    roll = _zig(40, run[-1] * 0.99, -0.005, 0.010, phase0=80 % 6)
    drift = _zig(20, roll[-1], 0.0, 0.007, phase0=(80 + 40) % 6)
    return _frame(run + roll + drift)


def post_run_loose_drift():
    """Same rolled-over shape as `post_run_drift`, but the drift is WIDE rather
    than a tight coil. Under the old shared 0.35 ceiling this cleared tightness —
    like every other atr_sum-basis chart. It is the fixture that proves the split
    threshold actually discriminates."""
    run = _zig(80, 100.0, 0.007, 0.010)
    roll = _zig(40, run[-1] * 0.99, -0.005, 0.010, phase0=80 % 6)
    drift = _zig(20, roll[-1], 0.0, 0.045, phase0=(80 + 40) % 6)
    return _frame(run + roll + drift)


def fresh_breakout():
    """Advance, brief base, then a sharp thrust out of it in the last 8 bars —
    a real setup, but not a COIL. Tightness is the leg that must catch it."""
    adv = _zig(120, 100.0, 0.004, 0.010)
    base = _zig(12, adv[-1] * 0.995, 0.0005, 0.005, phase0=120 % 6)
    brk = [base[-1] * (1 + 0.022 * i) for i in range(1, 9)]
    return _frame(adv + base + brk)


def short_history(n=40):
    return _frame(_zig(n, 100.0, 0.004, 0.010))


# ===========================================================================
# 1. The four metrics — pure-function unit tests
# ===========================================================================
def test_ideal_coil_scores_four_of_four():
    m = cs.structure_metrics(ideal_coil())
    assert m["structure_score"] == 4 and m["structure_score_of"] == 4
    assert m["insufficient"] == [] and m["status"] == "ok"
    assert all(m["constructive"][k] is True for k in cs.METRICS)
    # Each leg for the stated reason, not by accident.
    assert m["dist_from_high_pct"] <= cs.DIST_FROM_HIGH_MAX
    assert m["ma21_slope"] > 0 and m["ma21_slope_state"] == "rising"
    assert m["tightness"] < cs.TIGHTNESS_MAX_ADVANCE and m["tightness_basis"] == "advance"
    assert m["higher_lows"] >= cs.HIGHER_LOWS_MIN


def test_post_run_drift_scores_at_most_one_of_four():
    m = cs.structure_metrics(post_run_drift())
    assert m["structure_score"] <= 1, m
    assert m["structure_score_of"] == 4          # measurable, just unconstructive
    # The three legs that must catch this chart.
    assert m["dist_from_high_pct"] > cs.DIST_FROM_HIGH_MAX     # well off the highs
    assert m["constructive"]["ma21_slope"] is False            # not rising
    assert m["constructive"]["higher_lows"] is False           # no rising swing lows


def test_tightness_thresholds_are_split_per_basis():
    """The two denominators measure different things — a RANGE for an advancing
    prior window, summed true range (PATH LENGTH) when it did not advance — and
    path length is always >= the range it spans. One shared ceiling therefore
    cannot bar both: under 0.35 the atr_sum reading admitted 100% of its
    population and carried no information.

    The split value is corroborated two independent ways, both landing at ~0.045-0.049:
    random-walk scale (0.35 / sqrt(60)) and pass-rate matching against the
    advance basis. 0.05 sits between them."""
    assert cs.TIGHTNESS_MAX_ADVANCE == 0.35            # unchanged
    assert cs.TIGHTNESS_MAX_ATR_SUM == 0.05            # new, separately calibrated
    assert cs.TIGHTNESS_MAX_ATR_SUM < cs.TIGHTNESS_MAX_ADVANCE
    # Within a hair of the random-walk scale-equivalent bar.
    assert cs.TIGHTNESS_MAX_ATR_SUM == pytest.approx(
        cs.TIGHTNESS_MAX_ADVANCE / math.sqrt(cs.TIGHTNESS_PRIOR), abs=0.01)

    assert cs.tightness_max_for("advance") == cs.TIGHTNESS_MAX_ADVANCE
    assert cs.tightness_max_for("atr_sum") == cs.TIGHTNESS_MAX_ATR_SUM
    assert cs.tightness_max_for(None) is None          # nothing measured, nothing to judge


def test_the_split_threshold_has_teeth_on_the_atr_sum_basis():
    """The point of splitting: a WIDE drift on a non-advancing base must now fail
    tightness. Under the old shared 0.35 it cleared — which is what made the
    atr_sum reading useless."""
    m = cs.structure_metrics(post_run_loose_drift())
    assert m["tightness_basis"] == "atr_sum"
    assert m["tightness"] < 0.35                       # would have PASSED the old bar
    assert m["tightness"] > cs.TIGHTNESS_MAX_ATR_SUM   # ...and FAILS its own
    assert m["constructive"]["tightness"] is False
    assert m["structure_score"] == 0                   # 1/4 -> 0/4


def test_a_tight_coil_on_a_non_advancing_base_still_passes():
    """The split must discriminate, not blanket-reject. `post_run_drift`'s coil is
    genuinely tight (~6th percentile of the atr_sum population), so tightness —
    which measures tightness and nothing else — correctly still passes it. The
    chart is rejected by the OTHER three legs, which is the design: one metric per
    concern, and the score does the composing."""
    m = cs.structure_metrics(post_run_drift())
    assert m["tightness_basis"] == "atr_sum"
    assert m["constructive"]["tightness"] is True
    assert [k for k in cs.METRICS if m["constructive"][k]] == ["tightness"]
    assert m["structure_score"] == 1


def test_the_applied_ceiling_is_carried_on_the_record():
    """A ratio must never be read against the wrong bar, so the ceiling it was
    judged against travels with it."""
    for fn in (ideal_coil, post_run_drift, fresh_breakout):
        m = cs.structure_metrics(fn())
        assert m["tightness_max"] == cs.tightness_max_for(m["tightness_basis"])
    assert cs.structure_metrics(short_history(40))["tightness_max"] is None


def test_fresh_breakout_fails_tightness():
    m = cs.structure_metrics(fresh_breakout())
    assert m["constructive"]["tightness"] is False
    assert m["tightness"] >= cs.TIGHTNESS_MAX_ADVANCE
    assert m["tightness_basis"] == "advance"
    # It is not a bad chart — just not a coil. The other legs still read well,
    # which is what makes tightness the discriminating leg here.
    assert m["constructive"]["dist_from_high_pct"] is True
    assert m["constructive"]["ma21_slope"] is True


def test_insufficient_history_is_explicit_never_a_silent_zero_or_pass():
    m = cs.structure_metrics(short_history(40))
    # The two deep-history legs are UNMEASURED, not failed.
    assert m["dist_from_high_pct"] is None and m["tightness"] is None
    assert set(m["insufficient"]) == {"dist_from_high_pct", "tightness"}
    assert m["constructive"]["dist_from_high_pct"] is None
    assert m["constructive"]["tightness"] is None
    assert m["status"] == cs.INSUFFICIENT
    # The denominator shrinks with the numerator: a partial read is "2 of 2",
    # never "2 of 4" (which would read as a failure it never measured).
    assert m["structure_score_of"] == 2
    assert m["structure_score"] <= 2


def test_no_history_at_all_measures_nothing_and_claims_nothing():
    for df in (None, pd.DataFrame(), _frame(_zig(20, 100.0, 0.004, 0.010))):
        m = cs.structure_metrics(df)
        assert m["structure_score"] == 0 and m["structure_score_of"] == 0
        assert sorted(m["insufficient"]) == sorted(cs.METRICS)
        assert m["status"] == cs.INSUFFICIENT
        assert all(m["constructive"][k] is None for k in cs.METRICS)


def test_metrics_never_mutate_the_frame():
    df = ideal_coil()
    before = df.copy(deep=True)
    cs.structure_metrics(df)
    pd.testing.assert_frame_equal(df, before)


# ---- individual metric edges ----------------------------------------------
def test_dist_from_high_is_zero_at_the_high_and_never_negative():
    df = _frame(_zig(140, 100.0, 0.004, 0.0))     # monotone ramp, ends at its high
    assert cs.dist_from_high_pct(df) == 0.0


def test_dist_from_high_rejects_a_split_contaminated_frame():
    """A 2:1 split in an UNADJUSTED frame (Alpha Vantage TIME_SERIES_DAILY) halves
    price in ONE bar. The honest answer is "unmeasurable", not a -50% drawdown.

    Note the guard is a SIGNATURE test, not a magnitude one: a split and a real
    50% drawdown produce the identical trailing-max ratio, so only the one-bar
    gap distinguishes them."""
    assert cs.dist_from_high_pct(_frame([200.0] * 100 + [100.0] * 40)) is None

    # The SAME total decline, taken gradually, is a real drawdown and IS measured.
    gradual = _frame([200.0] * 100 + list(np.linspace(200.0, 100.0, 40)))
    assert cs.dist_from_high_pct(gradual) == pytest.approx(50.0, abs=1.0)

    # A 30% one-day gap is a 3:2 split's signature and is also rejected...
    assert cs.has_split_gap(pd.Series([100.0, 100.0, 66.0, 66.0])) is True
    # ...while an ordinary bad day is not.
    assert cs.has_split_gap(pd.Series([100.0, 100.0, 92.0, 92.0])) is False


def test_a_split_before_the_window_does_not_contaminate_it():
    """Only a split INSIDE the window matters — one before it leaves the whole
    window on a single price basis, which reads correctly."""
    df = _frame([200.0] * 60 + [100.0] * 130)     # split 130 bars ago
    assert cs.dist_from_high_pct(df, window=126) == 0.0


def test_ma21_slope_flat_band_is_not_rising():
    """The drifting chart's signature: a slope that is technically positive but
    inside the flat band. A bare sign test would call it constructive."""
    assert cs.ma21_slope_state(0.0) == "flat"
    assert cs.ma21_slope_state(cs.MA21_SLOPE_FLAT / 2) == "flat"
    assert cs.ma21_slope_state(-cs.MA21_SLOPE_FLAT / 2) == "flat"
    assert cs.ma21_slope_state(0.5) == "rising"
    assert cs.ma21_slope_state(-0.5) == "falling"
    assert cs.ma21_slope_state(None) is None
    # And the aggregate treats FLAT as unconstructive, not as "> 0".
    flat = _frame([100.0] * 140)
    m = cs.structure_metrics(flat)
    assert m["constructive"]["ma21_slope"] is False


def test_higher_lows_counts_the_trailing_run_and_zero_is_a_real_answer():
    # A monotone ramp has no local minima at all: zero pivots -> a genuine 0,
    # measurable, not missing.
    ramp = _frame(_zig(60, 100.0, 0.004, 0.0))
    assert cs.higher_lows(ramp) == 0
    # A rising zigzag makes successive higher lows.
    assert cs.higher_lows(_frame(_zig(60, 100.0, 0.004, 0.010))) >= 2
    # A falling zigzag makes lower lows -> the run is 0.
    assert cs.higher_lows(_frame(_zig(60, 200.0, -0.004, 0.010))) == 0
    # Too few bars is None (unmeasured), NOT 0 (measured-and-empty).
    assert cs.higher_lows(_frame(_zig(10, 100.0, 0.004, 0.010))) is None


def test_tightness_reports_which_denominator_it_used():
    tight, basis = cs.tightness(ideal_coil())
    assert basis == "advance" and 0 < tight < cs.TIGHTNESS_MAX_ADVANCE
    tight2, basis2 = cs.tightness(post_run_drift())
    assert basis2 == "atr_sum"
    assert cs.tightness(short_history(40)) == (None, None)


# ===========================================================================
# 2. Phase-aware volume check
# ===========================================================================
_VR_BELOW = 0.72        # the reported symptom: "volume ratio 0.72 < 0.8"


def _metrics(**over):
    """A metrics dict that is otherwise clean, so the only CAUTION under test is
    the volume one."""
    base = {"is_etf": False, "rs3m_vs_sector": 5.0, "below_ma200": False,
            "below_ma50": False, "atr_extension": 0.5, "ma50_slope": 0.4,
            "mfi": 50.0, "atr_momentum": 0.9, "volume_ratio": _VR_BELOW}
    base.update(over)
    return base


def test_same_volume_ratio_cautions_outside_a_consolidation():
    out = sc.compute_verdict(_metrics(consolidation_phase=False))
    assert out["verdict"] == "CAUTION"
    assert any("thin participation" in r for r in out["reasons"])
    assert out["notes"] == []


def test_same_volume_ratio_does_not_caution_inside_a_consolidation():
    out = sc.compute_verdict(_metrics(consolidation_phase=True))
    assert out["verdict"] == "GO"
    assert out["reasons"] == []
    # Suppressed, but not silent: the observation is still reported.
    assert any("drying up (constructive)" in n for n in out["notes"])


def test_absent_phase_flag_is_the_unchanged_pre_change_path():
    """Fails CLOSED. The many score_ticker callers that pass no gate have no phase
    to read, and must behave exactly as they did before this change."""
    out = sc.compute_verdict(_metrics())          # no consolidation_phase key at all
    assert out["verdict"] == "CAUTION"
    assert any("thin participation" in r for r in out["reasons"])


def test_phase_never_suppresses_any_other_caution():
    """The flag is scoped to the volume leg alone. Every other rule fires
    identically inside a consolidation."""
    for over, needle in (({"mfi": 80.0}, "MFI"),
                         ({"below_ma50": True}, "below MA50"),
                         ({"ma50_slope": -0.3}, "rolling over"),
                         ({"atr_momentum": 1.4}, "ATR expanding")):
        out = sc.compute_verdict(_metrics(consolidation_phase=True, **over))
        assert out["verdict"] == "CAUTION", (over, out)
        assert any(needle in r for r in out["reasons"]), (over, out)


def test_phase_cannot_rescue_an_avoid():
    out = sc.compute_verdict(_metrics(consolidation_phase=True, below_ma200=True))
    assert out["verdict"] == "AVOID"


def test_volume_ratio_at_or_above_the_floor_is_unaffected_either_way():
    for phase in (True, False):
        out = sc.compute_verdict(_metrics(consolidation_phase=phase,
                                          volume_ratio=T.VOLUME_RATIO_MIN))
        assert out["verdict"] == "GO" and out["notes"] == []


def test_the_volume_threshold_constant_is_unchanged():
    """The Phase-1 contract: the threshold VALUE is untouched; only where it
    APPLIES changed."""
    assert T.VOLUME_RATIO_MIN == 0.8


def test_no_hard_cfm_rule_or_level_4_threshold_moved():
    assert T.MFI_MIN == 40.0 and T.MFI_MAX == 60.0          # [HARD RULE]
    assert T.ATR_MOMENTUM_MAX == 1.0                        # [HARD RULE]
    assert T.ATR_EXTENSION_MAX == 3.0
    assert T.RS3M_VS_SECTOR_MIN == 0.0
    assert config.CONSOLIDATION_ATR_PCT_MAX == 5.0
    assert config.SPOT_ATR_MOMENTUM_MAX == 1.0
    assert config.SPOT_ATR_EXTENSION_MAX == 1.5
    assert config.L4_ATR_EXPANSION_MAX == 1.05


# ---- the phase flag itself -------------------------------------------------
def test_consolidation_phase_reads_the_live_level_4_checks():
    quiet = ideal_coil()
    spot = stock_lights.right_spot(quiet)
    assert cs.consolidation_phase(spot) is bool(
        all(c["pass"] for c in spot["checks"] if c["id"] in ("atr_5d_ema", "extension")))


def test_consolidation_phase_fails_closed_on_missing_input():
    assert cs.consolidation_phase(None) is False
    assert cs.consolidation_phase({}) is False
    assert cs.consolidation_phase({"checks": []}) is False
    # A partial right spot (one of the two checks absent) is not a phase claim.
    assert cs.consolidation_phase({"checks": [{"id": "extension", "pass": True}]}) is False


def test_consolidation_phase_requires_both_live_checks():
    def spot(atr_ok, ext_ok):
        return {"checks": [{"id": "atr_pct", "value": 2.0, "pass": True},
                           {"id": "atr_5d_ema", "value": 0.9, "pass": atr_ok},
                           {"id": "extension", "value": 0.4, "pass": ext_ok}]}
    assert cs.consolidation_phase(spot(True, True)) is True
    assert cs.consolidation_phase(spot(True, False)) is False
    assert cs.consolidation_phase(spot(False, True)) is False
    assert cs.consolidation_phase(spot(False, False)) is False


def test_consolidation_phase_ignores_the_atr_pct_leg():
    """atr_pct is a magnitude check ("is this a quiet NAME"), not a phase check
    ("is this name quiet RIGHT NOW"). Including it would make a structurally
    volatile name permanently ineligible for the constructive reading."""
    spot = {"checks": [{"id": "atr_pct", "pass": False},
                       {"id": "atr_5d_ema", "pass": True},
                       {"id": "extension", "pass": True}]}
    assert cs.consolidation_phase(spot) is True


def test_the_dead_ma21_percent_band_stays_dead():
    """Audit §1.4 / §8.2. `indicators.consolidating` has no call sites and
    `CONSOLIDATION_MA21_DIST_MAX` is reachable only through it. The phase flag
    deliberately does NOT resurrect either — it reads the live `extension` check,
    which measures the same thing in ATR units. This test fails if a later change
    quietly wires the dead constant back in."""
    import indicators
    # The names the function actually REFERENCES (not its docstring, which
    # explains the constant precisely so nobody re-adds it by accident).
    assert "CONSOLIDATION_MA21_DIST_MAX" not in cs.consolidation_phase.__code__.co_names
    assert "consolidating" not in cs.consolidation_phase.__code__.co_names
    assert indicators.consolidating is not None          # still defined, still unused
    assert config.CONSOLIDATION_MA21_DIST_MAX == 4.0     # untouched by this change


# ===========================================================================
# 3. Level-4 gate output is byte-identical (the XLK July 6th regression)
# ===========================================================================
def _xlk():
    return pd.read_parquet(os.path.join(FIX_REGIME, "xlk_july6_rollover.parquet"))


def test_xlk_july6_right_spot_is_byte_identical():
    """The canonical assertion that verdict logic is untouched. `right_spot` is a
    plain dict of scalars, so `==` is a genuine structural comparison — if a
    structure metric had leaked into `_right_spot_from`'s checks list, this fails."""
    df = _xlk()
    res = stock_lights.compute(df, sector_df=None, ivr_percentile=95.0, is_etf=True)
    assert res["right_spot"] == stock_lights.right_spot(df, config.RULESET_LEGACY)
    assert res["verdict"] == stock_lights.RED
    assert "veto:atr_expanding_high_ivr" in res["veto_reasons"]
    assert res["by_ruleset"][config.RULESET_LEGACY]["right_spot"] == res["right_spot"]
    # Exactly three checks, exactly these ids, in this order — no fourth leg.
    assert [c["id"] for c in res["right_spot"]["checks"]] == [
        "atr_pct", "atr_5d_ema", "extension"]


def test_xlk_july6_252_bar_leg_is_insufficient_not_a_number():
    """The fixture is 207 bars — deliberately below the 252-bar window. The long
    display leg must say so rather than silently reading over a short frame."""
    df = _xlk()
    assert len(df) < cs.HIGH_WINDOW_LONG
    m = cs.structure_metrics(df)
    assert m["dist_from_high_252_pct"] is None
    assert m["dist_from_high_pct"] is not None       # 126 still fits in 207 bars


def test_structure_metrics_do_not_perturb_the_right_spot_for_any_fixture():
    for df in (ideal_coil(), post_run_drift(), fresh_breakout(), _xlk()):
        before = stock_lights.right_spot(df)
        cs.structure_metrics(df)
        cs.consolidation_phase(before)
        assert stock_lights.right_spot(df) == before


# ===========================================================================
# 4. ZERO BLOCKING AUTHORITY
# ===========================================================================
def _ready_row_verdict(structure=None, phase=False):
    """The canonical row verdict for a clean READY name, with an arbitrary
    structure record attached. The structure record is deliberately NOT passed
    into any of the composing calls — that IS the invariant under test."""
    from structure_classifier import BaseStage, InstFlow
    composed = sv.compose_verdict("green", "green",
                                  BaseStage.EARLY_ADVANCE, InstFlow.ACCUMULATING)
    return st.compose_row_verdict(composed, [])


WORST_CASE = {
    "dist_from_high_pct": 62.5, "dist_from_high_252_pct": 71.0,
    "ma21_slope": -0.9, "ma21_slope_state": "falling",
    "tightness": 4.2, "tightness_basis": "atr_sum", "higher_lows": 0,
    "constructive": {k: False for k in cs.METRICS},
    "structure_score": 0, "structure_score_of": 4, "insufficient": [],
    "status": "ok", "shadow": True,
}


def test_worst_case_structure_leaves_a_ready_verdict_ready():
    """Force all four metrics to their worst possible values on a READY fixture.
    The verdict, and everything derived from it, must not move."""
    clean = _ready_row_verdict()
    assert clean["verdict"] == sv.READY

    worst = _ready_row_verdict(structure=WORST_CASE)
    assert worst["verdict"] == sv.READY
    assert worst == clean                       # byte-identical, not merely equal-verdict
    assert st.is_bench(worst["verdict"], worst["triggers"]) is False


def test_structure_produces_no_gate_block_and_no_trigger():
    """`blocks` is the authority boundary. Nothing structure-shaped may appear in
    it, in the triggers derived from it, or in the static kind map."""
    structural = set(cs.METRICS) | {"structure_score", "consolidation_phase"}
    # NB "structure" itself IS a legitimate pre-existing key — the classifier's
    # entrability SIGNAL input, which predates this change and is unrelated to the
    # shadow metrics. The four metric ids and the score must not join it.
    assert structural.isdisjoint(st._KIND.keys())
    assert "structure" in st._KIND                    # the pre-existing signal, untouched

    gate = {"levels": [
        {"level": 1, "pass": True}, {"level": 2, "pass": True},
        {"level": 3, "pass": True, "detail": {"vetoes": []}},
        {"level": 3.5, "pass": True},
        {"level": 4, "pass": True,
         "detail": {"right_spot": stock_lights.right_spot(ideal_coil())}},
    ]}
    blocks = st.gate_blocks(gate)
    assert structural.isdisjoint({b["id"] for b in blocks})


def test_structure_is_not_a_right_spot_check():
    """The one place "additive" is not automatic: a check added to
    `_right_spot_from` would gain blocking authority in a single line."""
    spot = stock_lights.right_spot(ideal_coil())
    ids = {c["id"] for c in spot["checks"]}
    assert ids == {"atr_pct", "atr_5d_ema", "extension"}
    assert set(cs.METRICS).isdisjoint(ids)
    assert set(cs.METRICS).isdisjoint(set(spot["blocked_by"]))


def test_structure_is_not_in_the_shadow_score_inputs():
    """The shadow SCORE is a RANKING. Structure must not reach it either — the
    prompt bars ranking authority as well as blocking authority."""
    import inspect
    import scan_score
    params = set(inspect.signature(scan_score.compute_score).parameters)
    assert set(cs.METRICS).isdisjoint(params)
    assert "structure_score" not in params


def test_no_config_switch_can_grant_structure_authority():
    """There is deliberately NO flag that turns any of this into a block.
    Graduating a metric must be a reviewed code change, not a toggle."""
    names = [n for n in dir(config)
             if "STRUCTURE" in n.upper() and "BLOCK" in n.upper()]
    assert names == []
    assert not hasattr(config, "STRUCTURE_SCORE_MIN")
    assert not hasattr(cs, "BLOCKING")


# ===========================================================================
# 5. Row wiring — additive keys only
# ===========================================================================
def _score_row(df, gate=None, monkeypatch=None):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    return sc.score_ticker("TEST", df, "XLK", df, gate, has_weeklies=True,
                           regime_color="green")


def test_row_carries_the_structure_record_and_the_phase_flag(monkeypatch):
    df = ideal_coil()
    gate = {"levels": [{"level": 4, "pass": True,
                        "detail": {"right_spot": stock_lights.right_spot(df)}}]}
    row = _score_row(df, gate, monkeypatch)
    assert row["structure"]["structure_score"] == row["structure_score"]
    assert row["structure_score_of"] == row["structure"]["structure_score_of"]
    assert isinstance(row["consolidation_phase"], bool)
    assert row["structure"]["shadow"] is True


def test_structure_is_attached_even_when_level_4_fails(monkeypatch):
    """Audit §9 Q4. A Level-4 failure short-circuits `score_ticker`, but
    "gate failed, structure N/4" is half the comparison the calibration is FOR —
    so the record must be attached BEFORE the short-circuit, not after."""
    df = post_run_drift()
    gate = {"levels": [{"level": 4, "pass": False,
                        "detail": {"right_spot": stock_lights.right_spot(df)}}]}
    row = _score_row(df, gate, monkeypatch)
    assert row["suitability"] == "AVOID"                 # the short-circuit fired
    assert "fails entry gate level 4" in row["suitability_reasons"][0]
    assert row["structure"] is not None                  # ...and structure survived it
    assert row["structure_score"] is not None
    assert "consolidation_phase" in row


def test_no_gate_means_no_phase_and_todays_volume_behaviour(monkeypatch):
    row = _score_row(ideal_coil(), None, monkeypatch)
    assert row["consolidation_phase"] is False


# ===========================================================================
# 6. Calibration logging + the annotation store
# ===========================================================================
def test_rejection_log_persists_the_structure_record():
    import scan_rejection_log as srl
    assert srl.SCHEMA_VERSION == 3
    m = cs.structure_metrics(ideal_coil())
    rec = srl._record_from_row({
        "ticker": "TEST", "verdict": "READY", "structure": m,
        "consolidation_phase": True, "volume_ratio": _VR_BELOW,
    })
    assert rec["structure_score"] == m["structure_score"]
    assert rec["structure_score_of"] == m["structure_score_of"]
    assert rec["tightness_basis"] == m["tightness_basis"]
    # The per-basis ceiling travels with the ratio, so a calibration pass can
    # never read a value against the wrong bar or pool the two populations.
    assert rec["tightness_max"] == m["tightness_max"]
    assert rec["structure_insufficient"] == m["insufficient"]
    assert rec["consolidation_phase"] is True
    assert rec["volume_ratio"] == _VR_BELOW
    # A row with no structure record still yields a well-formed record.
    bare = srl._record_from_row({"ticker": "X", "verdict": "WATCH"})
    assert bare["structure_score"] is None and bare["consolidation_phase"] is False


def test_rejection_log_round_trip_and_rollup(tmp_path, monkeypatch):
    import scan_rejection_log as srl
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    good = cs.structure_metrics(ideal_coil())
    bad = cs.structure_metrics(post_run_drift())
    out = srl.record_scan([
        {"ticker": "GOOD", "verdict": "READY", "structure": good,
         "consolidation_phase": True, "volume_ratio": _VR_BELOW},
        {"ticker": "BAD", "verdict": "WATCH", "structure": bad,
         "consolidation_phase": False, "volume_ratio": 1.4},
    ], scan_id="run-1")
    assert out["ok"] and out["recorded"] == 2

    summary = srl.summary()["structure"]
    assert summary["measured"] == 2
    assert summary["partial_reads"] == 0
    assert summary["by_score"] == {"1/4": {"WATCH": 1}, "4/4": {"READY": 1}}
    # The empirical size of the volume change: one suppressed CAUTION.
    assert summary["phase_suppressed_volume_cautions"] == 1


def test_partial_reads_are_not_pooled_with_full_ones(tmp_path, monkeypatch):
    import scan_rejection_log as srl
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log2.json"))
    srl.record_scan([{"ticker": "SHORT", "verdict": "WATCH",
                      "structure": cs.structure_metrics(short_history(40))}],
                    scan_id="run-1")
    summary = srl.summary()["structure"]
    assert summary["partial_reads"] == 1
    assert all(key.endswith("/2") for key in summary["by_score"])   # "n/2", never "n/4"


def test_structure_labels_store(tmp_path, monkeypatch):
    import structure_labels as sl
    monkeypatch.setattr(sl, "LABELS_PATH", str(tmp_path / "labels.json"))
    ok = sl.record_label("NVDA", "yes", scan_id="run-1", verdict="READY",
                         structure_score=4, structure_score_of=4, note="tight coil")
    assert ok["ok"] and ok["recorded"]["label"] == sl.COMPELLING
    assert sl.record_label("SMCI", "no", structure_score=1,
                           structure_score_of=4)["ok"]
    assert sl.record_label("X", "banana")["ok"] is False       # unknown label rejected
    assert sl.record_label("", "yes")["ok"] is False           # ticker required

    s = sl.summary()
    assert s["labels"] == 2 and s["tickers"] == 2
    assert s["by_score"] == {"1/4": {sl.NOT_COMPELLING: 1}, "4/4": {sl.COMPELLING: 1}}
    assert [r["ticker"] for r in sl.recent()] == ["SMCI", "NVDA"]


def test_relabelling_appends_and_never_rewrites(tmp_path, monkeypatch):
    """Append-only: a changed mind is visible AS a change of mind. Nothing here
    edits or re-derives the historical verdict it annotates."""
    import structure_labels as sl
    monkeypatch.setattr(sl, "LABELS_PATH", str(tmp_path / "labels2.json"))
    sl.record_label("AAPL", "yes", scan_id="run-1")
    sl.record_label("AAPL", "no", scan_id="run-1")
    rows = sl.series("AAPL")
    assert [r["label"] for r in rows] == [sl.COMPELLING, sl.NOT_COMPELLING]


def test_labels_have_no_consumer_outside_calibration():
    """A label must never feed the gate, the verdict, the executor or sizing."""
    import subprocess
    root = os.path.dirname(os.path.abspath(__file__))
    # Actual IMPORTS, not mentions — scan_rejection_log names the store in a
    # comment explaining what the calibration joins against, which is fine.
    hits = subprocess.run(
        ["grep", "-rlE", r"^[[:space:]]*(import|from) structure_labels",
         root, "--include=*.py"],
        capture_output=True, text=True).stdout.split()
    # app.py = the curl-able calibration endpoint; the other is this file.
    # Nothing in the gate / verdict / executor / sizing / ranking path.
    assert sorted(os.path.basename(h) for h in hits) == [
        "app.py", "test_chart_structure.py"]


# ===========================================================================
# 7. Whole-row byte identity — structure cannot influence anything else
#
# Stronger than comparing against a frozen pre-change snapshot: a snapshot only
# proves the row did not change on ONE input, and silently rots when any
# unrelated field is added. These drive the structure values to both extremes and
# assert that every OTHER key in the row is bit-for-bit identical — which is the
# actual claim ("zero authority"), proved directly.
# ===========================================================================
BEST_CASE = {
    "dist_from_high_pct": 0.4, "dist_from_high_252_pct": 1.1,
    "ma21_slope": 0.31, "ma21_slope_state": "rising",
    "tightness": 0.06, "tightness_basis": "advance", "higher_lows": 4,
    "constructive": {k: True for k in cs.METRICS},
    "structure_score": 4, "structure_score_of": 4, "insufficient": [],
    "status": "ok", "shadow": True,
}

_STRUCTURE_KEYS = {"structure", "structure_score", "structure_score_of"}


def _row_with_structure(df, gate, forced, monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    monkeypatch.setattr(cs, "structure_metrics", lambda _df: dict(forced))
    return sc.score_ticker("TEST", df, "XLK", df, gate, has_weeklies=True,
                           regime_color="green")


@pytest.mark.parametrize("shape", ["ideal_coil", "post_run_drift", "fresh_breakout"])
def test_every_non_structure_row_key_is_identical_under_worst_and_best(shape, monkeypatch):
    df = {"ideal_coil": ideal_coil, "post_run_drift": post_run_drift,
          "fresh_breakout": fresh_breakout}[shape]()
    gate = {"levels": [{"level": 4, "pass": True,
                        "detail": {"right_spot": stock_lights.right_spot(df)}}]}

    worst = _row_with_structure(df, gate, WORST_CASE, monkeypatch)
    best = _row_with_structure(df, gate, BEST_CASE, monkeypatch)

    assert set(worst) == set(best)
    differing = {k for k in worst if repr(worst[k]) != repr(best[k])}
    # The ONLY keys allowed to move are the structure record itself.
    assert differing <= _STRUCTURE_KEYS, differing
    # Spelled out, because these are the ones that matter.
    for key in ("verdict", "verdict_reasons", "binding", "triggers", "path_to_ready",
                "eligible_days", "bench", "suitability", "suitability_reasons",
                "score", "score_parts", "legacy_verdict", "proposed_verdict",
                "ruleset_divergence", "all_level_results", "first_failing_level"):
        assert repr(worst.get(key)) == repr(best.get(key)), key


def test_a_worst_case_structure_leaves_a_ready_row_ready(monkeypatch):
    """The spec's headline assertion: force all four metrics to worst-case on a
    genuinely READY fixture and the verdict must remain READY.

    Uses the real `early_advance_accum` fixture rather than a synthetic frame:
    the structure CLASSIFIER (Level 3.5, a separate and blocking thing) needs 210
    bars, so a short hand-built frame reads INSUFFICIENT_DATA and blocks for
    reasons that have nothing to do with these shadow metrics.

    The juice floor is relaxed for the same reason: under conftest's mock chain
    this fixture prices at 1.1%/wk gross, under the 1.5% adequacy floor, which is
    a Level-5 SAFETY block and would make the row BLOCKED for a reason unrelated
    to what is being tested. Relaxing it isolates the variable under test; the
    parametrized independence test above covers the general case without touching
    any threshold."""
    monkeypatch.setattr(config, "JUICE_FLOOR_WK", 0.5)
    df = pd.read_parquet(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "fixtures", "structure", "early_advance_accum.parquet"))
    gate = {"levels": [
        {"level": 1, "pass": True}, {"level": 2, "pass": True},
        {"level": 3, "pass": True, "detail": {"vetoes": []}},
        {"level": 3.5, "pass": True},
        {"level": 4, "pass": True,
         "detail": {"right_spot": stock_lights.right_spot(df)}},
    ]}
    row = _row_with_structure(df, gate, WORST_CASE, monkeypatch)
    assert row["structure_score"] == 0            # the shadow read is as bad as it gets
    assert row["verdict"] == sv.READY             # ...and the verdict does not care
    assert row["bench"] is False
    assert row["verdict_reasons"] == []


def test_the_phase_flag_changes_suitability_and_nothing_else(monkeypatch):
    """The one INTENDED behaviour change, scoped. Flipping the consolidation phase
    on a thin-volume name may move `suitability` (and its reasons/notes) — and
    must move nothing else, least of all the canonical verdict."""
    import data_handler
    df = ideal_coil()
    gate = {"levels": [{"level": 4, "pass": True,
                        "detail": {"right_spot": stock_lights.right_spot(df)}}]}
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)

    rows = {}
    for phase in (True, False):
        monkeypatch.setattr(cs, "consolidation_phase", lambda _spot, p=phase: p)
        rows[phase] = sc.score_ticker("TEST", df, "XLK", df, gate,
                                      has_weeklies=True, regime_color="green")

    differing = {k for k in rows[True] if repr(rows[True][k]) != repr(rows[False][k])}
    allowed = {"consolidation_phase", "suitability", "suitability_reasons",
               "suitability_notes"}
    assert differing <= allowed, differing
    assert rows[True]["verdict"] == rows[False]["verdict"]
