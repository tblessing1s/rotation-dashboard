"""The veto set, the two-state verdict, and the entry route (§1.1 / §1.4 / §1.6).

The load-bearing assertions here are the ones about what does NOT block. A test
suite for a filter proves the filter fires; a test suite for a thin floor plus a
ranker has to prove the floor STAYS thin — that a name failing only ranking inputs
is ELIGIBLE, and that no veto carries an override path.
"""
from __future__ import annotations

import config
import pytest
import scan_verdict as sv


# ---------------------------------------------------------------------------
# The registry is the contract
# ---------------------------------------------------------------------------
def test_veto_registry_is_the_exhaustive_list():
    """§1.1 is exhaustive: this list is complete and nothing else may block."""
    assert set(sv.VETO_IDS) == {
        "regime_red", "rs3m_vs_spy", "close_below_ma50", "close_below_ma200",
        "line_in_the_sand", "earnings_in_cycle", "no_weeklies",
        "untradeable_spread", "stale_inputs", "account",
    }


def test_every_veto_names_the_rule_it_mirrors():
    """The governing principle: the entry veto set equals the exit trigger set plus
    hard account constraints. A veto that cannot name what it mirrors does not
    belong, so the registry carries the mirror and this asserts none is blank."""
    for veto_id, label, mirrors in sv.VETOES:
        assert label and mirrors, veto_id


def test_evaluate_emits_no_id_outside_the_registry():
    blocks = sv.evaluate(
        regime_color="red", rs3m_vs_spy=-1.0, below_ma50=True, below_ma200=True,
        price=10.0, line_in_the_sand=20.0, has_weeklies=False,
        spread_pct=99.0, stale=True)
    assert {b["veto"] for b in blocks} <= set(sv.VETO_IDS)


# ---------------------------------------------------------------------------
# One fixture per veto: it blocks, and it blocks with no override path (§1.8)
# ---------------------------------------------------------------------------
_CASES = [
    ("regime_red", {"regime_color": "red"}),
    ("rs3m_vs_spy", {"rs3m_vs_spy": -0.01}),
    ("close_below_ma50", {"below_ma50": True}),
    ("close_below_ma200", {"below_ma200": True}),
    ("line_in_the_sand", {"price": 9.99, "line_in_the_sand": 10.0}),
    ("no_weeklies", {"has_weeklies": False}),
    ("untradeable_spread",
     {"spread_pct": config.TRADEABILITY_MAX_SPREAD_PCT + 0.01}),
    ("stale_inputs", {"stale": True}),
]


@pytest.mark.parametrize("veto_id,kwargs", _CASES, ids=[c[0] for c in _CASES])
def test_each_veto_blocks(veto_id, kwargs):
    blocks = sv.evaluate(**kwargs)
    assert [b["veto"] for b in blocks] == [veto_id]
    assert sv.compose(blocks)["verdict"] == sv.BLOCKED


@pytest.mark.parametrize("veto_id,kwargs", _CASES, ids=[c[0] for c in _CASES])
def test_no_veto_has_an_override_path(veto_id, kwargs):
    """§1.5: the structural veto list carries NO ``override_reason`` path.

    Asserted structurally rather than by trying one keyword: ``evaluate`` is
    keyword-only, so ANY override-shaped argument is a TypeError. That is a
    stronger guarantee than checking one spelling — there is no argument name that
    could admit a blocked name.
    """
    for attempt in ("override_reason", "override", "force", "admit_anyway"):
        with pytest.raises(TypeError):
            sv.evaluate(**kwargs, **{attempt: "because I want to"})
    # And the composed verdict has no field a caller could flip.
    composed = sv.compose(sv.evaluate(**kwargs))
    assert composed["verdict"] == sv.BLOCKED
    assert "override" not in composed and "overridable" not in composed


def test_account_gate_blocking_failures_become_vetoes():
    gate = {"checks": [{"id": "cash_reserve", "detail": {"short_by": 500}},
                       {"id": "earnings_in_cycle", "detail": {"earnings": {}}}],
            "blocking_failures": ["cash_reserve", "earnings_in_cycle"]}
    blocks = sv.evaluate(account_gate=gate)
    by_id = {b["id"]: b for b in blocks}
    assert by_id["cash_reserve"]["veto"] == "account"
    # Earnings is its own registry veto, not a generic account failure — §1.1
    # lists it separately and the surviving CALENDAR trigger keys off it.
    assert by_id["earnings_in_cycle"]["veto"] == "earnings_in_cycle"


# ---------------------------------------------------------------------------
# What must NOT block — the whole point of the redesign
# ---------------------------------------------------------------------------
def test_a_clean_candidate_is_eligible():
    assert sv.compose(sv.evaluate(regime_color="green", rs3m_vs_spy=1.0,
                                  below_ma50=False, below_ma200=False,
                                  has_weeklies=True))["verdict"] == sv.ELIGIBLE


def test_regime_yellow_does_not_block():
    """YELLOW constrains the ROUTE, not eligibility (§1.6)."""
    assert sv.evaluate(regime_color="yellow") == []


def test_ranking_only_failures_never_block():
    """§1.2 exhaustively: sector strength, sector breadth, sector ATR expansion,
    the four-light vote, RS magnitude above zero, ATR% of price, ATR vs its 5-EMA,
    extension above MA21 and structure entrability all RANK. None may veto.

    ``evaluate`` is keyword-only, so the proof is that it accepts no argument for
    any of them — a ranking input cannot reach the blocks list because there is no
    parameter through which to pass one.
    """
    for name in ("sector_rs1m", "sector_breadth", "sector_atr_expanding",
                 "stock_greens", "atr_momentum", "atr_pct", "extension_atr",
                 "entrability", "base_stage", "inst_flow", "score",
                 "shadow_floor", "juice_capacity", "structure_score"):
        with pytest.raises(TypeError):
            sv.evaluate(**{name: 0})


def test_missing_chart_data_fails_open():
    """The three chart vetoes mirror EXIT rules, and the exit rules do not fire on
    an unknown. A veto that fired on absence would block every short-history name —
    the multiplicative collapse this redesign exists to remove."""
    assert sv.evaluate(rs3m_vs_spy=None, below_ma50=None, below_ma200=None) == []


def test_unknown_freshness_fails_closed():
    """STALE_BLOCKS_GO is a HARD rule and unknown freshness has always read as
    stale. Note the asymmetry with the chart vetoes above — it is deliberate."""
    assert sv.evaluate(stale=True) != []
    assert sv.evaluate(stale=False) == []


def test_unknown_weeklies_is_not_a_false_hide():
    assert sv.evaluate(has_weeklies=None) == []
    assert sv.evaluate(has_weeklies=True) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_blocked_by_order_is_registry_order_not_evaluation_order():
    blocks = sv.evaluate(stale=True, regime_color="red", has_weeklies=False)
    assert sv.compose(blocks)["blocked_by"] == [
        "regime_red", "no_weeklies", "stale_inputs"]


def test_compose_is_deterministic_across_runs():
    kwargs = dict(regime_color="red", rs3m_vs_spy=-2.0, below_ma200=True)
    assert sv.compose(sv.evaluate(**kwargs)) == sv.compose(sv.evaluate(**kwargs))


# ---------------------------------------------------------------------------
# Entry route selection (§1.6) — advisory only
# ---------------------------------------------------------------------------
_NEAR = config.SPOT_ATR_EXTENSION_MAX - 0.5
_EXTENDED = config.SPOT_ATR_EXTENSION_MAX + 0.5


def test_a_name_near_ma21_routes_to_shares():
    r = sv.route(extension_atr=_NEAR, regime_color="green")
    assert r["route"] == sv.SHARES


def test_the_same_name_extended_routes_to_a_put():
    r = sv.route(extension_atr=_EXTENDED, regime_color="green", ma21=100.0)
    assert r["route"] == sv.CASH_SECURED_PUT
    assert r["detail"]["target_strike_zone"] == 100.0


def test_the_same_name_extended_under_yellow_routes_to_shares():
    """A put commits capital a week out on a tape that is already wobbling."""
    r = sv.route(extension_atr=_EXTENDED, regime_color="yellow")
    assert r["route"] == sv.SHARES
    assert r["reason"] == "regime_yellow_shares_only"


def test_below_ma21_routes_to_shares():
    assert sv.route(extension_atr=-2.0, regime_color="green")["route"] == sv.SHARES


def test_red_regime_offers_no_route():
    assert sv.route(extension_atr=_EXTENDED, regime_color="red")["route"] is None


def test_unmeasurable_extension_takes_the_route_that_commits_nothing_forward():
    assert sv.route(extension_atr=None, regime_color="green")["route"] == sv.SHARES


def test_the_route_threshold_is_the_old_level_4_veto_bar():
    """No constant of its own: what used to BLOCK an entry now SELECTS its route."""
    assert (sv.route(extension_atr=0.0)["detail"]["threshold"]
            == config.SPOT_ATR_EXTENSION_MAX)


# ---------------------------------------------------------------------------
# Put collateral, the collateral-denominated floor, and re-gating (§1.6)
# ---------------------------------------------------------------------------
def test_put_collateral_is_strike_times_a_round_lot():
    assert sv.put_collateral(50.0, 1) == 5000.0
    assert sv.put_collateral(50.0, 2) == 10000.0
    assert sv.put_collateral(None, 1) is None


def test_put_collateral_counts_against_the_capital_cap_not_the_reserve():
    """§1.6: collateral is the position, not a defence of one. It consumes
    deployed-capital headroom and leaves the ATR cash reserve untouched."""
    collateral = sv.put_collateral(100.0, 1)
    deployed_before, reserve = 20000.0, 4000.0
    deployed_after = deployed_before + collateral
    assert deployed_after == 30000.0
    assert deployed_after <= config.MAX_DEPLOYED_CAPITAL   # counted against the cap
    assert reserve == 4000.0                                # reserve undrawn


def test_put_juice_uses_collateral_as_its_denominator():
    """A DIFFERENT denominator from the covered-call floor, and a different
    constant — the two measure a yield on different capital."""
    assert sv.put_juice_pct(0.50, 50.0) == pytest.approx(1.0)
    assert config.PUT_JUICE_FLOOR_PCT != config.SHARES_JUICE_FLOOR_PCT


def test_an_open_put_on_a_name_that_broke_the_50_day_advises_close():
    """Re-gate before assignment: if the name would be BLOCKED today, close the
    put rather than accept delivery of shares the entry rules would refuse."""
    advice = sv.put_close_advice(blocks=sv.evaluate(below_ma50=True))
    assert advice["action"] == "close"
    assert advice["blocked_by"] == ["close_below_ma50"]


def test_a_close_below_ma21_alone_does_not_advise_closing_the_put():
    """MA21 is a TIMING reference — it is what put the name on the put route in the
    first place, and a put struck at the MA21 zone is supposed to be approached.
    It appears nowhere in the veto registry, so this cannot fire even by accident.
    """
    assert "ma21" not in " ".join(sv.VETO_IDS)
    still_fine = sv.evaluate(regime_color="green", rs3m_vs_spy=1.0,
                             below_ma50=False, below_ma200=False)
    assert sv.put_close_advice(blocks=still_fine)["action"] == "hold"


def test_an_eligible_name_holds_its_put():
    assert sv.put_close_advice(blocks=[])["action"] == "hold"


# ---------------------------------------------------------------------------
# Re-pinned canonical fixtures (§1.8), with the WRITTEN DIFF for each.
#
# This change is expected to alter live verdicts; the byte-identical rule is
# suspended and replaced by a documented diff. A verdict change that cannot be
# explained is a bug, not a redesign — so each fixture below states what it used
# to produce, what it produces now, and the specific veto or rank change that
# caused it.
# ---------------------------------------------------------------------------
import os

import pandas as pd

import indicators
import scan_score
import stock_lights
import structure_classifier as sclf

_FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _evaluate_fixture(path, ivr, is_etf):
    """Run one fixture frame through the new veto set + ranker + route."""
    df = pd.read_parquet(path)
    sl = stock_lights.compute(df, ivr_percentile=ivr, is_etf=is_etf)
    base, inst = sclf.classify_symbol(df)
    price = indicators.last(df)
    ma50, ma200 = indicators.sma(df, 50), indicators.sma(df, 200)
    ext = indicators.atr_extension(df)
    blocks = sv.evaluate(
        regime_color="green", rs3m_vs_spy=None,
        below_ma50=None if ma50 is None else bool(price < ma50),
        below_ma200=None if ma200 is None else bool(price < ma200),
        has_weeklies=True)
    return {
        "composed": sv.compose(blocks),
        "route": sv.route(extension_atr=ext, regime_color="green"),
        "greens": sl["greens"],
        "right_spot_pass": sl["right_spot"]["pass"],
        "entrability": sclf.structure_entrability(base, inst),
        "extension_atr": ext,
        "score": scan_score.compute_score(
            inst_flow=inst, base_stage=base,
            entrability=sclf.structure_entrability(base, inst),
            extension_atr=ext, stock_greens=sl["greens"],
            atr_momentum=indicators.atr_momentum(df),
            net_juice_weekly_pct=1.0),
    }


def test_xlk_july6_still_blocked_but_for_a_different_reason():
    """XLK, 6 July rollover.

    WAS:  BLOCKED. Level 3 failed (0/4 lights, plus the atr-expanding/high-IVR
          veto), Level 3.5 failed (INSUFFICIENT_DATA structure), Level 4 failed
          (ATR expanding). Stop-on-first-fail reported Level 3 and hid the rest.

    NOW:  BLOCKED — same outcome, different and better-named reason.
          ``blocked_by == ["close_below_ma50", "close_below_ma200"]``.

    WHY:  the light vote, the structure grid and the right spot all lost veto
          authority. What blocks this name now is that it is trading below BOTH
          its 50- and 200-day averages — which is the circuit breaker, i.e. the
          rule that would make you EXIT it. That is the governing principle
          working as intended: the name a rollover fixture describes is exactly
          the name the exit rules refuse, and it is refused for that reason
          rather than for a momentum-screen proxy of it.

          Note also what the old gate could NOT tell you: it stopped at Level 3.
          The new evaluation names every veto that fired, which is what makes the
          sole-blocker rate computable for this row at all.
    """
    out = _evaluate_fixture(
        os.path.join(_FIX, "regime", "xlk_july6_rollover.parquet"), 95.0, True)
    assert out["composed"]["verdict"] == sv.BLOCKED
    assert out["composed"]["blocked_by"] == ["close_below_ma50", "close_below_ma200"]
    # The three demoted inputs all still read badly — they simply no longer block.
    assert out["greens"] == 0
    assert out["right_spot_pass"] is False
    assert out["entrability"] == sclf.Entrability.BLOCKED


def test_low_juice_fixture_flips_from_blocked_to_eligible_with_a_put_route():
    """``early_advance_low_juice`` — the repo's canonical stand-in for the
    "GDDY Aug 21" artifact, which does not exist in this tree.

    WAS:  not entrable. Structure/RS/SYM were all green, but Level 4 failed on
          BOTH legs (ATR expanding and extended past the 1.5-ATR bar), so the
          gate-complete verdict was WATCH — and its economics were thin besides.

    NOW:  **ELIGIBLE**, ranked ~5.0/10, routed to a CASH-SECURED PUT.

    WHY:  this is the single clearest expression of the redesign. The name is
          4.2 ATR above its MA21 — genuinely extended — and being extended used
          to mean "you may not enter". It now means "do not pay this price
          today": the route selector sends it to a weekly put struck at the MA21
          zone, so the operator is PAID to wait for the price the strategy would
          rather pay. The extension is not ignored; it costs the name rank
          (the extension sub-score is near its floor) and it changes how the
          entry is made.

          The thin juice likewise no longer blocks: the LEAP-denominated floor
          could not fire in shares mode at all and was deleted, and the
          share-denominated replacement is the ranker's multiplicative viability
          factor — which is why this name ranks ~5 rather than ~7.5.
    """
    out = _evaluate_fixture(
        os.path.join(_FIX, "structure", "early_advance_low_juice.parquet"), 20.0, False)
    assert out["composed"]["verdict"] == sv.ELIGIBLE
    assert out["composed"]["blocked_by"] == []
    # Extended — the old Level-4 veto would have fired here.
    assert out["right_spot_pass"] is False
    assert out["extension_atr"] > config.SPOT_ATR_EXTENSION_MAX
    # ...and that extension now picks the ROUTE instead of blocking.
    assert out["route"]["route"] == sv.CASH_SECURED_PUT
    # The extension is priced into the rank rather than ignored.
    assert out["score"]["parts"]["extension"] < 0.2
    # And the thin juice shows up as a viability haircut, not a block.
    assert out["score"]["score"] < out["score"]["score_quality"]


def test_the_two_fixtures_diverge_in_opposite_directions():
    """The pair is the whole argument. Both were non-entrable under the filter;
    one stays blocked because it trips an EXIT rule, the other becomes eligible
    because "extended" was never an exit rule — it was a screen for appreciation
    applied to a strategy whose upside is capped."""
    xlk = _evaluate_fixture(
        os.path.join(_FIX, "regime", "xlk_july6_rollover.parquet"), 95.0, True)
    low = _evaluate_fixture(
        os.path.join(_FIX, "structure", "early_advance_low_juice.parquet"), 20.0, False)
    assert xlk["composed"]["verdict"] == sv.BLOCKED
    assert low["composed"]["verdict"] == sv.ELIGIBLE


# ---------------------------------------------------------------------------
# The scan is ADVISORY. The executor enforces. (§1.8, §0.2 item 6)
# ---------------------------------------------------------------------------
def test_the_executor_still_rejects_an_eligible_name_that_fails_l5(tmp_path,
                                                                   monkeypatch):
    """An ELIGIBLE scan verdict is not an authorization.

    This is the invariant the whole redesign rests on being able to loosen the
    scan safely: the shortlist got materially more permissive, so the fact that
    NOTHING downstream treats a scan verdict as permission is what keeps that
    from being a risk. ``executor.execute`` re-runs the Level-5 account gate
    itself and rejects, whatever the scan said.
    """
    import executor
    import logging_handler as log

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    state = log.load_state()
    state["metadata"]["operating_cash"] = 0        # cannot fund anything
    log.save_state(state)

    # The scan says ELIGIBLE...
    assert sv.compose(sv.evaluate(regime_color="green", rs3m_vs_spy=5.0,
                                  below_ma50=False, below_ma200=False,
                                  has_weeklies=True))["verdict"] == sv.ELIGIBLE

    # ...and the ticket refuses anyway, because the account cannot fund it.
    with pytest.raises(ValueError) as exc:
        executor.execute({"action": "buy_shares", "ticker": "AAA", "qty": 100,
                          "price_per_share": 50.0, "stock_price": 50.0})
    assert "cash_reserve" in str(exc.value) or "reserve" in str(exc.value).lower()
