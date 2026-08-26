"""What survives in ``scan_triggers``: the earnings CALENDAR trigger, the combined
weekly-equivalent yield, and the profile-aware SHADOW income floor.

WHAT THESE TESTS USED TO COVER, AND WHY THEY ARE GONE. This file used to pin the
whole trigger-emission machinery — four trigger kinds, the days-to-trigger
estimator, ``gate_blocks``, ``compose_row_verdict``, the BENCH derived view and
the "path to READY" renderer — plus the two-tier juice floor. All of it was
deleted with the serial filter it described (see the module docstring): a
countdown to a gate that no longer blocks is a countdown to nothing, and the
LEAP-denominated juice floor could not fire in shares mode at all.

The verdict assertions those tests carried now live in ``test_scan_verdict``,
against the veto set that replaced them. The Fixture D case in particular —
"entrable structure, extended past the right spot" — inverted: that name is now
ELIGIBLE with an extension-penalised rank and a PUT route, which is asserted in
``test_scan_score`` and ``test_scan_verdict`` respectively.
"""
from __future__ import annotations

import config
import scan_triggers as st
import scan_verdict as sv


# ---------------------------------------------------------------------------
# The one surviving trigger: earnings clears on a DETERMINISTIC date.
# ---------------------------------------------------------------------------
def _earnings_blocks(date_str):
    gate = {"checks": [{"id": "earnings_in_cycle",
                        "detail": {"earnings": {"date": date_str}}}],
            "blocking_failures": ["earnings_in_cycle"]}
    return sv.evaluate(account_gate=gate)


def test_earnings_trigger_is_a_calendar_date():
    trig = st.earnings_trigger(_earnings_blocks("2026-09-10"))
    assert trig["kind"] == st.CALENDAR
    # The report date plus the settle buffer — the name is enterable after it.
    assert trig["eligible_date"] == "2026-09-11"
    assert trig["earnings_date"] == "2026-09-10"


def test_earnings_trigger_is_none_without_an_earnings_veto():
    assert st.earnings_trigger([]) is None
    assert st.earnings_trigger(sv.evaluate(regime_color="red")) is None


def test_an_unknown_report_date_yields_no_trigger_rather_than_a_guess():
    """No calendar answer exists for an unknown date, and inventing one would be
    exactly the false precision the ESTIMATED trigger kind was deleted for."""
    assert st.earnings_trigger(_earnings_blocks(None)) is None
    assert st.earnings_trigger(_earnings_blocks("not-a-date")) is None


def test_only_the_calendar_kind_survives():
    assert st.CALENDAR == "calendar"
    for dead in ("CONDITIONAL", "ESTIMATED", "SAFETY"):
        assert not hasattr(st, dead), f"{dead} should have been deleted"


def test_the_deleted_verdict_machinery_is_actually_gone():
    """Anti-inflation rule 6: the codebase is not append-only. No compatibility
    shim, no deprecation wrapper — the old path is deleted, not retained."""
    for dead in ("compose_row_verdict", "gate_blocks", "juice_floor_block",
                 "triggers_for_blocks", "is_bench", "path_to_ready",
                 "earliest_eligible_days", "classify"):
        assert not hasattr(st, dead), f"{dead} should have been deleted"


def test_earnings_trigger_is_pure():
    blocks = _earnings_blocks("2026-09-10")
    before = repr(blocks)
    st.earnings_trigger(blocks)
    st.earnings_trigger(blocks)
    assert repr(blocks) == before


# ---------------------------------------------------------------------------
# Combined weekly-equivalent yield (schema v21) — unchanged.
# ---------------------------------------------------------------------------
def test_combined_yield_sums_juice_and_the_weekly_dividend():
    out = st.combined_weekly_yield(0.80, 5.2)
    assert out["dividend_weekly_pct"] == round(5.2 / config.DIVIDEND_WEEKS_PER_YEAR, 4)
    assert out["combined_weekly_yield_pct"] == round(0.80 + out["dividend_weekly_pct"], 4)
    assert out["dividend_known"] is True


def test_an_unknown_dividend_is_flagged_not_shown_as_a_confident_zero():
    out = st.combined_weekly_yield(0.80, None)
    assert out["dividend_known"] is False
    assert out["combined_weekly_yield_pct"] == 0.80


def test_an_unpriceable_juice_leg_gives_no_combined_figure():
    assert st.combined_weekly_yield(None, 5.2)["combined_weekly_yield_pct"] is None


def test_juice_clears_slippage_uses_an_absolute_floor():
    """A PROPORTIONAL haircut can never flip the sign of a positive yield, so the
    absolute per-share floor is what makes this a real test rather than a vacuous
    one."""
    assert st.juice_clears_slippage(0.01, 10.0)["clears"] is False
    assert st.juice_clears_slippage(1.00, 10.0)["clears"] is True
    assert st.juice_clears_slippage(None)["clears"] is None


# ---------------------------------------------------------------------------
# The SHADOW income floor. It reaches the RANKER and nothing else.
# ---------------------------------------------------------------------------
def test_shadow_floor_declares_itself_shadow_and_non_blocking():
    out = st.shadow_floor("JUICE_ENGINE", 0.01)
    assert out["shadow"] is True and out["blocking"] is False
    assert out["pass"] is False          # it EVALUATES...


def test_a_failing_shadow_floor_does_not_block():
    """The load-bearing invariant, restated for the veto set: the income floor is
    below its bar and the name is still ELIGIBLE. There is no config switch that
    can change this and no parameter on ``evaluate`` through which the floor could
    reach the blocks list."""
    floor = st.shadow_floor("JUICE_ENGINE", 0.01)
    assert floor["pass"] is False
    assert sv.compose(sv.evaluate(regime_color="green"))["verdict"] == sv.ELIGIBLE


def test_an_unmeasurable_name_is_unmeasured_never_a_recorded_failure():
    assert st.shadow_floor("JUICE_ENGINE", None)["pass"] is None


def test_dividend_compounder_uses_the_combined_bar_plus_a_slippage_subfloor():
    out = st.shadow_floor("DIVIDEND_COMPOUNDER", 0.10, 30.0,
                          weekly_extrinsic_per_share=0.001)
    assert out["basis"] == "combined"
    assert out["floor_pct"] == config.COMBINED_YIELD_FLOOR_WK
    # Clears the combined bar on its dividend alone, but its juice cannot pay for
    # its own crossing — flagged separately rather than hidden by the blend.
    assert "JUICE_BELOW_SLIPPAGE" in out["reasons"]
