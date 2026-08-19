"""Entry-gate ruleset recalibration — the SHADOW-FIRST dual compute.

Covers the four moving parts of the change:

  1. the symbol vote rule (legacy 4-of-4 vs proposed mandatory-core 3-of-4),
  2. the Level-4 ATR ceiling (legacy contracting vs proposed not-expanding),
  3. the full per-level calibration record (every level, past the first fail),
  4. the ``config.GATE_RULESET`` authority flag (default "legacy").

Every test is offline and fixture/scalar driven — no provider calls, no clock.
"""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-gate-ruleset-"))

import config  # noqa: E402
import genius_lights  # noqa: E402
import indicators  # noqa: E402
import scan_triggers  # noqa: E402
import stock_lights  # noqa: E402
import symbol_genius  # noqa: E402

FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regime")

LEGACY, PROPOSED = config.RULESET_LEGACY, config.RULESET_PROPOSED
GREEN, YELLOW, RED = stock_lights.GREEN, stock_lights.YELLOW, stock_lights.RED


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setattr(config, "GATE_RULESET", LEGACY)


@pytest.fixture
def proposed(monkeypatch):
    monkeypatch.setattr(config, "GATE_RULESET", PROPOSED)


def _xlk():
    return pd.read_parquet(os.path.join(FIX_DIR, "xlk_july6_rollover.parquet"))


# ===========================================================================
# 1. XLK July 6th regression — unchanged under the legacy ruleset.
#    This is the canonical assertion that verdict logic is untouched.
# ===========================================================================
def test_1_xlk_july6_identical_under_legacy_ruleset(legacy):
    df = _xlk()

    # Layer 1 — the four-light vote still denies GREEN.
    eng = genius_lights.compute(df)
    assert eng["lights"]["sar"]["signal"] == "red" or eng["lights"]["momentum"]["signal"] == "red"
    assert eng["greens"] < 4

    # Layer 2 — the ATR/IVR veto still fires independently, through the ETF path.
    assert indicators.atr_expanding(df) is True
    res = stock_lights.compute(df, sector_df=None, ivr_percentile=95.0, is_etf=True)
    assert res["verdict"] == RED
    assert "veto:atr_expanding_high_ivr" in res["veto_reasons"]
    assert stock_lights.compute(df, sector_df=None, ivr_percentile=10.0,
                                is_etf=True)["verdict"] != GREEN

    # And the authoritative fields ARE the legacy rule's output, exactly — the
    # dual compute added a shadow record, it did not reroute the decision.
    assert res["verdict"] == stock_lights.verdict(res["greens"], res["insufficient"],
                                                  res["vetoed"])
    assert res["right_spot"] == stock_lights.right_spot(df, LEGACY)
    assert res["by_ruleset"][LEGACY]["verdict"] == res["verdict"]
    assert res["by_ruleset"][LEGACY]["right_spot"] == res["right_spot"]


def test_1b_xlk_july6_cannot_flip_under_the_proposed_ruleset(proposed):
    """Measured, not assumed: the fixture's last bar is 0/4 green with the
    MANDATORY CORE light red, so the mandatory-core rule is strictly unable to
    admit it — with the ATR/IVR veto armed or disarmed."""
    df = _xlk()
    eng = genius_lights.compute(df)
    assert eng["greens"] == 0
    assert stock_lights.core_is_green(eng["lights"]) is False

    for ivr in (95.0, 10.0):
        res = stock_lights.compute(df, sector_df=None, ivr_percentile=ivr, is_etf=True)
        assert res["verdict"] == RED
        assert res["by_ruleset"][LEGACY]["verdict"] == RED
        assert res["by_ruleset"][PROPOSED]["verdict"] == RED


# ===========================================================================
# 2. SYM vote matrix.
#
# NOTE ON THE SPEC: the prompt's summary list says "(c) 3/4 green with core
# FAILING -> RED both" and "(d) 2/4 -> RED both". Neither holds against the
# prompt's own normative formulas, and the legacy rule is pinned by regression:
#   * legacy has NO core concept, so a core-failing 3/4 is still exactly 3 green
#     -> YELLOW. Only the PROPOSED rule can read it RED.
#   * the proposed formula makes SYM_MIN_GREEN_LIGHTS - 1 (= 2) YELLOW, so a
#     core-green 2/4 is YELLOW under proposed and RED under legacy.
# Both readings of (d) are asserted below. The formulas are implemented as
# written; these tests record what the rules actually produce.
# ===========================================================================
def _votes(greens, core, *, insufficient=False, veto=False):
    return stock_lights.verdicts_by_ruleset(greens, insufficient, veto, core)


def test_2a_four_of_four_green_is_green_under_both():
    v = _votes(4, True)
    assert v[LEGACY] == GREEN and v[PROPOSED] == GREEN


def test_2b_three_of_four_core_passing_diverges_yellow_to_green():
    """The headline case: SAR (or any single non-core light) red, core green.
    Legacy YELLOW (never enterable) -> proposed GREEN. This is the divergence
    the whole shadow period exists to measure."""
    v = _votes(3, True)
    assert v[LEGACY] == YELLOW
    assert v[PROPOSED] == GREEN
    assert v[LEGACY] != v[PROPOSED]


def test_2c_three_of_four_core_failing_is_red_under_proposed():
    """Core failure alone forces RED regardless of the other lights — the
    proposed rule is STRICTER than legacy here, not looser."""
    v = _votes(3, False)
    assert v[PROPOSED] == RED
    assert v[LEGACY] == YELLOW          # legacy has no core concept (pinned)


def test_2d_two_of_four_core_failing_is_red_under_both():
    v = _votes(2, False)
    assert v[LEGACY] == RED and v[PROPOSED] == RED


def test_2d2_two_of_four_core_passing_is_the_proposed_watchlist():
    """SYM_MIN_GREEN_LIGHTS - 1 green with the core intact is the proposed
    YELLOW band. YELLOW is never enterable under either ruleset, so this changes
    the displayed tier, never entry eligibility."""
    v = _votes(2, True)
    assert v[LEGACY] == RED
    assert v[PROPOSED] == YELLOW


def test_2e_vetoes_and_insufficient_history_still_dominate_both_rulesets():
    for greens, core in ((4, True), (3, True), (0, False)):
        assert _votes(greens, core, veto=True) == {LEGACY: RED, PROPOSED: RED}
        assert _votes(greens, core, insufficient=True) == {LEGACY: RED, PROPOSED: RED}


def test_2f_symbol_genius_reports_both_rulesets_and_follows_the_flag(monkeypatch):
    """The SYM engine uses the same shared rule, on its own (SMA50>SMA200) lights."""
    df = _xlk()
    monkeypatch.setattr(config, "GATE_RULESET", LEGACY)
    sym = symbol_genius.compute(df)
    assert sym["ruleset"] == LEGACY
    assert set(sym["by_ruleset"]) == set(config.GATE_RULESETS)
    assert sym["color"] == sym["by_ruleset"][LEGACY]

    monkeypatch.setattr(config, "GATE_RULESET", PROPOSED)
    sym2 = symbol_genius.compute(df)
    assert sym2["color"] == sym2["by_ruleset"][PROPOSED]
    assert sym2["lights"] == sym["lights"]        # the lights themselves never move


# ===========================================================================
# 3. Level-4 ATR ceiling — contracting (legacy) vs not-expanding (proposed).
# ===========================================================================
def _spot(momentum, ruleset):
    """Right spot at an exact ATR/ATR_5EMA ratio, with the other two checks
    passing, so the ratio is the only thing under test."""
    return stock_lights._right_spot_from(3.0, momentum, 0.5, ruleset)


@pytest.mark.parametrize("momentum,legacy_pass,proposed_pass", [
    (0.95, True, True),     # contracting — passes both
    (1.02, False, True),    # flat-to-slightly-firm — the divergence band
    (1.10, False, False),   # genuinely expanding — fails both
])
def test_3_level4_atr_bands(momentum, legacy_pass, proposed_pass):
    assert _spot(momentum, LEGACY)["pass"] is legacy_pass
    assert _spot(momentum, PROPOSED)["pass"] is proposed_pass


def test_3b_only_the_atr_check_moves():
    """ATR% and extension are identical under both rulesets — only the veto's
    ATR ceiling relaxed, and the SCORE's own ATR band is untouched."""
    import scan_score
    over_ext = stock_lights._right_spot_from(3.0, 0.9, 99.0, PROPOSED)
    assert over_ext["blocked_by"] == ["spot:extension"]
    over_atrp = stock_lights._right_spot_from(99.0, 0.9, 0.5, PROPOSED)
    assert over_atrp["blocked_by"] == ["spot:atr_pct"]
    assert stock_lights.atr_momentum_max(LEGACY) == config.SPOT_ATR_MOMENTUM_MAX
    assert stock_lights.atr_momentum_max(PROPOSED) == config.L4_ATR_EXPANSION_MAX
    # The rank still rewards contraction — the score band is a separate constant.
    assert scan_score.ATR_CONTRACTING == 1.0 and scan_score.ATR_EXPANDING == 1.2


def test_3c_unmeasurable_atr_still_fails_conservatively_under_both():
    for rs in config.GATE_RULESETS:
        assert stock_lights._right_spot_from(3.0, None, 0.5, rs)["pass"] is False


def test_3d_gate_blocks_reads_the_requested_rulesets_right_spot():
    """The shadow fold reads the alternate right spot out of the level detail —
    a READ, never a re-evaluation."""
    legacy_spot = _spot(1.02, LEGACY)
    proposed_spot = _spot(1.02, PROPOSED)
    gate = {"levels": [{"level": 4, "pass": legacy_spot["pass"],
                        "detail": {"right_spot": legacy_spot,
                                   "right_spot_by_ruleset": {LEGACY: legacy_spot,
                                                             PROPOSED: proposed_spot}}}]}
    assert [b["id"] for b in scan_triggers.gate_blocks(gate)] == ["atr_5d_ema"]
    assert [b["id"] for b in scan_triggers.gate_blocks(gate, ruleset=LEGACY)] == ["atr_5d_ema"]
    assert scan_triggers.gate_blocks(gate, ruleset=PROPOSED) == []


# ===========================================================================
# 4. Stop-on-first-fail governs the verdict; the log records every level.
# ===========================================================================
def _gate_for(monkeypatch, *, sector_rs1m, verdict, right_spot_pass):
    import screening
    screening._results.clear()
    monkeypatch.setattr(screening, "regime",
                        lambda: {"status": "green", "published_regime": "green",
                                 "lights": {}, "breadth": 70, "vix": 15})
    monkeypatch.setattr(screening, "sectors",
                        lambda: {"XLK": {"name": "Technology", "rs1m": sector_rs1m,
                                         "breadth": 70, "inst_flow": "ACCUMULATING"}})
    spot = {"pass": right_spot_pass, "checks": [], "blocked_by": []}
    alt = {"pass": True, "checks": [], "blocked_by": []}
    monkeypatch.setattr(screening, "_stock_row", lambda *a, **k: {
        "ticker": "NVDA", "sector": "XLK", "lights": {}, "greens": 4,
        "verdict": verdict, "atr_pct": 3.0, "consolidating": right_spot_pass,
        "right_spot": spot, "status": "wait",
        "by_ruleset": {LEGACY: {"verdict": verdict, "right_spot": spot},
                       PROPOSED: {"verdict": stock_lights.GREEN, "right_spot": alt}},
    })
    monkeypatch.setattr(screening, "resolve_profile", lambda t, **k: "JUICE_ENGINE")
    # Level 3.5 reads real bars; pin it entrable so these tests isolate the two
    # levels the ruleset actually moves (L3 vote, L4 right spot).
    import structure_classifier
    monkeypatch.setattr(structure_classifier, "classify_symbol",
                        lambda df: (structure_classifier.BaseStage.EARLY_ADVANCE,
                                    structure_classifier.InstFlow.ACCUMULATING))
    return screening.entry_gate("NVDA")


def test_4_all_levels_recorded_even_past_the_first_failure(monkeypatch):
    # Level 2 fails (sector RS1M negative); the verdict stops there...
    gate = _gate_for(monkeypatch, sector_rs1m=-3.0, verdict=GREEN, right_spot_pass=True)
    assert gate["cleared_level"] == 1
    assert gate["first_failing_level"] == 2
    assert gate["verdict"] == "WAIT"

    # ...but every level's result is recorded, including the ones AFTER it.
    results = gate["all_level_results"]
    assert set(results) == {"L1", "L2", "L3", "L3.5", "L4", "L5"}
    assert results["L2"]["pass"] is False
    assert results["L3"]["pass"] is True          # evaluated and recorded regardless
    assert results["L4"]["pass"] is True
    # Level 5 is a separate per-request account overlay — recorded as unevaluated,
    # never guessed (evaluating it per swept name would load state per name).
    assert results["L5"]["pass"] is None and results["L5"]["note"] == "not_evaluated"


def test_4b_per_ruleset_replay_rides_along_with_the_gate(monkeypatch):
    """A name whose Level-3 verdict is YELLOW under legacy but GREEN under
    proposed clears a different number of levels in each replay."""
    gate = _gate_for(monkeypatch, sector_rs1m=5.0, verdict=YELLOW, right_spot_pass=False)
    assert gate["cleared_level"] == 2                        # legacy stops at L3
    assert gate["rulesets"][LEGACY]["cleared_level"] == 2
    assert gate["rulesets"][PROPOSED]["cleared_level"] == 4  # L3 GREEN + L4 relaxed
    assert gate["rulesets"][PROPOSED]["verdict"] == "READY TO ENTER"
    assert gate["rulesets"][LEGACY]["verdict"] == "WAIT"


# ===========================================================================
# 5. The authority flag.
# ===========================================================================
@pytest.mark.skipif(os.environ.get("CFM_GATE_RULESET") is not None,
                    reason="the shipped default is overridden by the environment")
def test_5a_default_ruleset_is_legacy():
    """The proposed rules never carry authority by default — flipping is a human
    decision, made once the divergence record justifies it."""
    assert config.GATE_RULESET == LEGACY
    assert config.GATE_RULESETS == (LEGACY, PROPOSED)


def test_5b_flipping_the_flag_moves_authority_and_nothing_else(monkeypatch):
    """Same inputs, both rulesets — only which one is authoritative changes."""
    lights = {"close_vs_ma": {"signal": "green"}}
    core = stock_lights.core_is_green(lights)

    monkeypatch.setattr(config, "GATE_RULESET", LEGACY)
    assert stock_lights.verdict_for(3, False, False, core_green=core) == YELLOW
    monkeypatch.setattr(config, "GATE_RULESET", PROPOSED)
    assert stock_lights.verdict_for(3, False, False, core_green=core) == GREEN

    # An explicit ruleset always wins over the flag, in both directions.
    assert stock_lights.verdict_for(3, False, False, core_green=core,
                                    ruleset=LEGACY) == YELLOW
    monkeypatch.setattr(config, "GATE_RULESET", LEGACY)
    assert stock_lights.verdict_for(3, False, False, core_green=core,
                                    ruleset=PROPOSED) == GREEN


def test_5c_an_unknown_ruleset_never_becomes_authority(monkeypatch):
    """A typo'd env value must degrade to legacy, never to "not legacy"."""
    monkeypatch.setenv("CFM_GATE_RULESET", "PROPOSED-ISH")
    import importlib
    reloaded = importlib.reload(config)
    try:
        assert reloaded.GATE_RULESET == LEGACY
    finally:
        monkeypatch.delenv("CFM_GATE_RULESET", raising=False)
        importlib.reload(config)


# ===========================================================================
# 6. The record carries both verdicts and their divergence.
# ===========================================================================
def test_6_rejection_record_carries_the_divergence(tmp_path, monkeypatch):
    import scan_rejection_log as srl
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    row = {"ticker": "NVDA", "verdict": "WATCH", "verdict_reasons": ["symbol:WATCH"],
           "gate_ruleset": LEGACY, "legacy_verdict": "WATCH", "proposed_verdict": "READY",
           "ruleset_divergence": True, "first_failing_level": 3,
           "all_level_results": {"L3": {"pass": False}},
           "sym_lights": {"close_vs_ma": "green", "sar": "red"},
           "sym_core_green": True, "sym_greens": 3, "atr_momentum": 1.02}
    srl.record_scan([row], day="2026-07-16", scan_id="run-1")
    rec = srl.series("NVDA")[-1]
    assert rec["legacy_verdict"] == "WATCH" and rec["proposed_verdict"] == "READY"
    assert rec["ruleset_divergence"] is True
    assert rec["first_failing_level"] == 3
    assert rec["sym_lights"]["sar"] == "red" and rec["sym_core_green"] is True
    assert rec["atr_momentum"] == 1.02

    s = srl.summary()
    assert s["ruleset_divergences"] == 1
    assert s["ruleset_divergence_pairs"]["WATCH->READY"] == 1


# ===========================================================================
# 7. Kill-switch isolation — it never reads SAR or the symbol vote.
# ===========================================================================
def test_7_kill_switch_output_is_identical_under_both_rulesets(monkeypatch):
    import data_handler
    import earnings
    import kill_switch
    import sector_data

    def _frame(values):
        idx = pd.bdate_range("2024-01-01", periods=len(values))
        c = pd.Series(values, index=idx, dtype=float)
        return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1,
                             "Close": c, "Volume": 1e6}, index=idx)

    spy = _frame([100.0] * 70)
    weak = _frame([100 - i * 0.5 for i in range(70)])
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: spy if s.upper() in ("SPY", "XLK") else weak)
    monkeypatch.setattr(sector_data, "sector_for", lambda t: "XLK")
    monkeypatch.setattr(earnings, "next_earnings",
                        lambda t: {"date": None, "days_until": None, "warning": False})

    monkeypatch.setattr(config, "GATE_RULESET", LEGACY)
    under_legacy = kill_switch.evaluate("NVDA")
    monkeypatch.setattr(config, "GATE_RULESET", PROPOSED)
    under_proposed = kill_switch.evaluate("NVDA")

    assert under_legacy == under_proposed
    assert under_legacy["status"] == "red"
