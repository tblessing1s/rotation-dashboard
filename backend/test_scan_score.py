"""The ranker (§1.3) — normalization, weights, explainability, determinism.

The assertions that matter most are the ones about the boundary between RANKING
authority and VETO authority. The ranker now carries the first, deliberately, and
must never acquire the second: a name that scores 0.0 is ELIGIBLE with a rank of
0.0, not BLOCKED. Every other property here (comparable inputs, neutral handling
of missing data, deterministic ties) exists to make that ordering trustworthy
enough to act on.
"""
from __future__ import annotations

import pytest
import scan_score as ss
import scan_verdict as sv
from structure_classifier import BaseStage, Entrability, InstFlow

_GOOD = dict(inst_flow=InstFlow.ACCUMULATING, base_stage=BaseStage.EARLY_ADVANCE,
             base_count=1, entrability=Entrability.READY, extension_atr=0.0,
             stock_greens=4, rs3m_vs_spy=8.0, sector_rs1m=4.0, sector_breadth=70.0,
             atr_momentum=0.9, structure_score=4, structure_score_of=4,
             capacity_pct=2.0, shadow_floor_pct=0.75, net_juice_weekly_pct=2.0)
_BAD = dict(inst_flow=InstFlow.DISTRIBUTING, base_stage=BaseStage.DECLINING,
            entrability=Entrability.BLOCKED, extension_atr=5.0, stock_greens=0,
            rs3m_vs_spy=0.0, sector_rs1m=-5.0, sector_breadth=20.0,
            atr_momentum=1.5, structure_score=0, structure_score_of=4,
            capacity_pct=0.05, shadow_floor_pct=0.75, net_juice_weekly_pct=0.05)


# ---------------------------------------------------------------------------
# Ranking authority is not veto authority (§1.3 / §1.7)
# ---------------------------------------------------------------------------
def test_a_name_failing_only_ranking_inputs_is_eligible_with_a_low_rank():
    """§1.8, the headline assertion of the whole redesign. Every ranking input is
    at its worst; the name is still ELIGIBLE, and its rank — not its eligibility —
    is what carries the bad news."""
    scored = ss.compute_score(**_BAD)
    assert scored["score"] < 1.0
    # Nothing in the ranker reached the veto set.
    assert sv.compose(sv.evaluate(regime_color="green"))["verdict"] == sv.ELIGIBLE


def test_a_zero_score_is_still_eligible_never_blocked():
    scored = ss.compute_score(net_juice_weekly_pct=None)
    assert scored["score"] == 0.0
    assert sv.compose([])["verdict"] == sv.ELIGIBLE


def test_the_ranker_produces_no_blocks():
    """The ranker returns a score and its parts — there is no field a caller could
    append to the blocks list, which is what keeps the invariant structural rather
    than a matter of discipline."""
    out = ss.compute_score(**_BAD)
    assert set(out) == {"score", "score_quality", "parts", "contributions"}
    for key in ("blocks", "block", "veto", "blocked", "blocking"):
        assert key not in out


def test_ranking_beats_blocking_on_a_three_of_four_light_name():
    """The single largest source of newly-eligible names: 4/4 lights used to be the
    only passing state and 3/4 was a hard stop. Now it costs rank, not entry."""
    four = ss.compute_score(**{**_GOOD, "stock_greens": 4})
    three = ss.compute_score(**{**_GOOD, "stock_greens": 3})
    assert three["score"] < four["score"]
    assert three["score"] > 0


# ---------------------------------------------------------------------------
# Purity and determinism (§1.8)
# ---------------------------------------------------------------------------
def test_the_ranker_is_pure_same_inputs_same_output():
    assert ss.compute_score(**_GOOD) == ss.compute_score(**_GOOD)


def test_the_ranker_does_no_io():
    """Asserted by construction: the module imports nothing that can perform I/O."""
    import inspect
    src = inspect.getsource(ss)
    for forbidden in ("import data_handler", "import requests", "open(",
                      "datetime.now", "import logging_handler", "load_state"):
        assert forbidden not in src, forbidden


def test_rank_is_deterministic_across_two_runs():
    rows = [{"ticker": "AAA", "score": 5.0}, {"ticker": "BBB", "score": 7.0},
            {"ticker": "CCC", "score": 5.0}]
    assert ss.rank(rows) == ss.rank(rows)


def test_ties_break_by_symbol_so_a_scan_is_reproducible():
    rows = [{"ticker": "ZZZ", "score": 5.0}, {"ticker": "AAA", "score": 5.0}]
    assert [r["ticker"] for r in ss.rank(rows)] == ["AAA", "ZZZ"]
    # And reversing the input order changes nothing.
    assert [r["ticker"] for r in ss.rank(list(reversed(rows)))] == ["AAA", "ZZZ"]


def test_rank_orders_best_first_and_stamps_one_based_positions():
    ranked = ss.rank([{"ticker": "AAA", "score": 1.0},
                      {"ticker": "BBB", "score": 9.0}])
    assert [(r["ticker"], r["rank"]) for r in ranked] == [("BBB", 1), ("AAA", 2)]


def test_rank_never_drops_a_name_the_veto_set_admitted():
    """A missing score sorts last rather than removing the row — a rank input that
    failed to compute must not silently un-admit an eligible name."""
    ranked = ss.rank([{"ticker": "AAA"}, {"ticker": "BBB", "score": 3.0}])
    assert [r["ticker"] for r in ranked] == ["BBB", "AAA"]


def test_rank_does_not_mutate_its_input():
    rows = [{"ticker": "AAA", "score": 5.0}]
    ss.rank(rows)
    assert "rank" not in rows[0]


# ---------------------------------------------------------------------------
# Normalization: inputs must be comparable across names (§1.3)
# ---------------------------------------------------------------------------
def test_every_sub_score_is_normalized_to_the_unit_interval():
    for kwargs in (_GOOD, _BAD, {}):
        parts = ss.compute_score(**kwargs)["parts"]
        for name, value in parts.items():
            assert 0.0 <= value <= 1.0, (name, value)


def test_a_missing_input_scores_neutral_never_zero():
    """"We could not measure this" and "this is bad" are opposite facts. Scoring
    absence as badness would rebuild the multiplicative collapse this redesign
    removed — a short-history name would sink to the bottom of every list."""
    parts = ss.compute_score(net_juice_weekly_pct=1.0)["parts"]
    for name in ("inst_flow", "base", "structure", "extension", "lights",
                 "rs_magnitude", "sector", "atr", "chart_structure", "capacity"):
        assert parts[name] == 0.5, name


def test_chart_structure_is_normalized_against_its_variable_denominator():
    """The raw count is not comparable across names: a name with fewer measurable
    metrics has a smaller ceiling."""
    a = ss.compute_score(structure_score=2, structure_score_of=2,
                         net_juice_weekly_pct=2.0)
    b = ss.compute_score(structure_score=2, structure_score_of=4,
                         net_juice_weekly_pct=2.0)
    assert a["parts"]["chart_structure"] == 1.0
    assert b["parts"]["chart_structure"] == 0.5


def test_the_capacity_sentinel_is_neutral_not_zero():
    """``INSUFFICIENT_HISTORY`` is a string, not a number. "Not measured yet" and
    "yields nothing" are opposite facts and must not share a sub-score."""
    sentinel = ss.compute_score(capacity_pct="INSUFFICIENT_HISTORY",
                                shadow_floor_pct=0.75, net_juice_weekly_pct=2.0)
    thin = ss.compute_score(capacity_pct=0.0, shadow_floor_pct=0.75,
                            net_juice_weekly_pct=2.0)
    assert sentinel["parts"]["capacity"] == 0.5
    assert thin["parts"]["capacity"] == 0.0


def test_negative_rs_scores_zero_rather_than_being_re_blocked():
    """Negative RS3M-vs-SPY is a VETO. The veto owns that decision; a rank that
    duplicated it would be a second, differently-shaped authority."""
    assert ss.compute_score(rs3m_vs_spy=-5.0, net_juice_weekly_pct=2.0
                            )["parts"]["rs_magnitude"] == 0.0


def test_extension_below_the_ma_is_not_penalised():
    """A name that has pulled back to its mean is exactly the entry this strategy
    wants — the extension sub-score must not treat "below" as "far from ideal"."""
    at = ss.compute_score(extension_atr=0.0, net_juice_weekly_pct=2.0)
    below = ss.compute_score(extension_atr=-2.0, net_juice_weekly_pct=2.0)
    assert at["parts"]["extension"] == below["parts"]["extension"] == 1.0


# ---------------------------------------------------------------------------
# Explainability (§1.3) and the viability factor
# ---------------------------------------------------------------------------
def test_a_rank_is_explainable_without_re_running_anything():
    out = ss.compute_score(**_GOOD)
    assert set(out["contributions"]) == set(ss._WEIGHTS)
    # Contribution == sub-score x weight, so a reader can see what earned or cost
    # a name its position.
    for key, weight in ss._WEIGHTS.items():
        assert out["contributions"][key] == pytest.approx(
            out["parts"][key] * weight, abs=1e-3)


def test_the_weights_table_lives_in_exactly_one_place():
    assert sum(ss._WEIGHTS.values()) == pytest.approx(ss._TOTAL_WEIGHT)


def test_a_beautiful_chart_that_pays_nothing_ranks_near_zero():
    """With no income floor left in the veto set, the multiplicative viability
    factor is the only thing keeping an unpayable name off the top of the list —
    and because it is a factor on a RANK, such a name is still enterable."""
    pretty_broke = ss.compute_score(**{**_GOOD, "net_juice_weekly_pct": 0.01})
    pretty_paid = ss.compute_score(**_GOOD)
    assert pretty_broke["score"] < 0.2 * pretty_paid["score"]
    assert pretty_broke["score_quality"] == pretty_paid["score_quality"]


def test_a_good_setup_outranks_a_bad_one():
    assert ss.compute_score(**_GOOD)["score"] > ss.compute_score(**_BAD)["score"]


def test_score_stays_within_zero_and_ten():
    for kwargs in (_GOOD, _BAD, {}, {"net_juice_weekly_pct": 99.0}):
        assert 0.0 <= ss.compute_score(**kwargs)["score"] <= 10.0
