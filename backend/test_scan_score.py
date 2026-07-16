"""Composite scan SCORE (0–10) — the shadow RANK tier. Pure function tests."""
from __future__ import annotations

import rs_state as rss
import scan_score
import structure_classifier as sclf
from structure_classifier import BaseStage, InstFlow


def _score(**kw):
    return scan_score.compute_score(**kw)["score"]


def test_score_is_bounded_0_to_10():
    lo = _score(inst_flow=InstFlow.DISTRIBUTING, base_stage=BaseStage.DECLINING,
                rs_state_value=rss.FALLING, sector_rs1m=-20.0, atr_momentum=2.0,
                pct_above_ma21=40.0, net_juice_weekly_pct=0.0)
    hi = _score(inst_flow=InstFlow.ACCUMULATING, base_stage=BaseStage.EARLY_ADVANCE,
                base_count=1, rs_state_value=rss.RISING, sector_rs1m=10.0,
                atr_momentum=0.8, pct_above_ma21=3.0, net_juice_weekly_pct=5.0)
    assert 0.0 <= lo <= 10.0 and 0.0 <= hi <= 10.0
    assert hi > lo


def test_worst_case_is_zero():
    assert _score(inst_flow=InstFlow.DISTRIBUTING, base_stage=BaseStage.DECLINING,
                  rs_state_value=rss.FALLING, sector_rs1m=-20.0, atr_momentum=2.0,
                  pct_above_ma21=40.0, net_juice_weekly_pct=0.0) == 0.0


def test_best_case_is_ten():
    assert _score(inst_flow=InstFlow.ACCUMULATING, base_stage=BaseStage.EARLY_ADVANCE,
                  base_count=0, rs_state_value=rss.RISING, sector_rs1m=10.0,
                  atr_momentum=0.5, pct_above_ma21=2.0, net_juice_weekly_pct=5.0) == 10.0


def test_accumulating_outscores_early_interest_all_else_equal():
    common = dict(base_stage=BaseStage.EARLY_ADVANCE, rs_state_value=rss.RISING,
                  sector_rs1m=5.0, atr_momentum=0.9, pct_above_ma21=3.0,
                  net_juice_weekly_pct=2.0)
    assert _score(inst_flow=InstFlow.ACCUMULATING, **common) > \
        _score(inst_flow=InstFlow.EARLY_INTEREST, **common)


def test_turning_outscores_fading_all_else_equal():
    common = dict(inst_flow=InstFlow.EARLY_INTEREST, base_stage=BaseStage.EARLY_ADVANCE,
                  sector_rs1m=0.0, atr_momentum=1.0, pct_above_ma21=3.0,
                  net_juice_weekly_pct=2.0)
    assert _score(rs_state_value=rss.TURNING, **common) > \
        _score(rs_state_value=rss.FADING, **common)


def test_extended_from_ma21_scores_lower_than_near():
    common = dict(inst_flow=InstFlow.ACCUMULATING, base_stage=BaseStage.EARLY_ADVANCE,
                  rs_state_value=rss.RISING, sector_rs1m=5.0, atr_momentum=0.9,
                  net_juice_weekly_pct=2.0)
    assert _score(pct_above_ma21=3.0, **common) > _score(pct_above_ma21=30.0, **common)


def test_missing_inputs_are_neutral_not_crashing():
    out = scan_score.compute_score(inst_flow=None, base_stage=None)
    assert 0.0 <= out["score"] <= 10.0
    assert set(out["parts"]) == {"inst_flow", "base", "rs_state", "sector",
                                 "atr", "dist_ma21", "net_juice", "juice_viability"}


def test_juice_viability_factor_orders_viable_first():
    # SCORE acceptance property: the PNC shape (0.12%/wk) must NOT outscore the XBI
    # shape (0.33%/wk) when quality is identical — economics decides the tie.
    quality = dict(inst_flow=InstFlow.ACCUMULATING, base_stage=BaseStage.EARLY_ADVANCE,
                   base_count=1, rs_state_value=rss.RISING, sector_rs1m=5.0,
                   atr_momentum=0.9, pct_above_ma21=3.0)
    pnc = scan_score.compute_score(net_juice_weekly_pct=0.12, **quality)
    xbi = scan_score.compute_score(net_juice_weekly_pct=0.33, **quality)
    assert pnc["score"] < xbi["score"]                 # viable-first, not prettiest-first
    # Same quality is preserved; only the viability factor differs.
    assert pnc["score_quality"] == xbi["score_quality"]
    assert pnc["parts"]["juice_viability"] < xbi["parts"]["juice_viability"]


def test_juice_viability_clamps_at_zero_for_nonpositive_and_full_above_target():
    quality = dict(inst_flow=InstFlow.ACCUMULATING, base_stage=BaseStage.EARLY_ADVANCE,
                   rs_state_value=rss.RISING, sector_rs1m=5.0, atr_momentum=0.9,
                   pct_above_ma21=3.0)
    assert scan_score.compute_score(net_juice_weekly_pct=-0.5, **quality)["score"] == 0.0
    assert scan_score.compute_score(net_juice_weekly_pct=0.0, **quality)["score"] == 0.0
    # At/above the target the factor is 1.0 -> score == quality.
    full = scan_score.compute_score(net_juice_weekly_pct=scan_score.JUICE_TARGET_WK + 5, **quality)
    assert full["parts"]["juice_viability"] == 1.0 and full["score"] == full["score_quality"]


def test_parts_are_reported_for_the_calibration_log():
    out = scan_score.compute_score(
        inst_flow=InstFlow.ACCUMULATING, base_stage=BaseStage.EARLY_ADVANCE,
        base_count=1, rs_state_value=rss.RISING, sector_rs1m=5.0, atr_momentum=0.9,
        pct_above_ma21=3.0, net_juice_weekly_pct=2.0)
    assert out["parts"]["inst_flow"] == 1.0
    assert all(0.0 <= v <= 1.0 for v in out["parts"].values())
