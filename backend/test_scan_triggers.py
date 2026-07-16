"""Tests for the gate-complete scan verdict + forward-looking triggers.

The crux is Fixture D (early_advance_extended, the AAPL 7/16 shape): an entrable
structure with green SYM whose FULL gate fails at Level 4 (extended past the right
spot). The signal composition alone says READY; the gate-complete verdict must say
WATCH with the binding constraint at Level 4 — the verdict-completeness fix.
"""
from __future__ import annotations

import os

import pandas as pd

import scan_triggers as st
import scan_verdict as sv
import stock_lights
from structure_classifier import BaseStage, InstFlow

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "structure")


def _load(name):
    return pd.read_parquet(os.path.join(FIX, f"{name}.parquet"))


# ---------------------------------------------------------------------------
# Trigger classification.
# ---------------------------------------------------------------------------
def test_classify_kinds():
    assert st.classify({"level": 4, "id": "extension", "observed": {}})["kind"] == st.ESTIMATED
    assert st.classify({"level": 4, "id": "atr_pct", "observed": {}})["kind"] == st.CONDITIONAL
    assert st.classify({"level": 5, "id": "sector_concentration", "observed": {}})["kind"] == st.CONDITIONAL
    assert st.classify({"level": 5, "id": "earnings_in_cycle", "observed": {}})["kind"] == st.CALENDAR
    assert st.classify({"level": 3, "id": "veto:close_below_ma200", "observed": {}})["kind"] == st.SAFETY
    assert st.classify({"level": 2, "id": "under_distribution", "observed": {}})["kind"] == st.SAFETY


def test_earnings_calendar_eligible_date_and_days():
    block = {"level": 5, "id": "earnings_in_cycle",
             "observed": {"earnings": {"date": "2026-07-30", "days_until": 14}}}
    trig = st.classify(block)["trigger"]
    assert trig["kind"] == st.CALENDAR
    # Eligible the day after the report (buffer PROPOSED_DEFAULT).
    assert trig["eligible_date"] == "2026-07-31"
    assert trig["days_estimate"] == 14 + st.EARNINGS_TRIGGER_BUFFER_DAYS


def test_extension_estimated_days_from_ma21_catchup():
    # 2 ATR of excess, MA21 rising $0.5/day, ATR $2 => 2*2/0.5 = 8 days (EST).
    block = {"level": 4, "id": "extension",
             "observed": {"excess_atr": 2.0, "ma21_rise_per_day": 0.5, "atr": 2.0}}
    trig = st.classify(block)["trigger"]
    assert trig["kind"] == st.ESTIMATED and trig["estimated"] is True
    assert trig["days_estimate"] == 8


def test_estimated_days_none_when_uncomputable():
    trig = st.classify({"level": 4, "id": "extension", "observed": {}})["trigger"]
    assert trig["days_estimate"] is None       # still EST, just no concrete number


# ---------------------------------------------------------------------------
# Block extraction (a READ of the gate dicts — no re-eval).
# ---------------------------------------------------------------------------
def _gate_with_l4(df):
    """A minimal gate dict carrying only the failing Level-4 right spot, exactly as
    screening.entry_gate lays it out (detail.right_spot.checks)."""
    return {"levels": [
        {"level": 1, "pass": True}, {"level": 2, "pass": True},
        {"level": 3, "pass": True, "detail": {"vetoes": []}},
        {"level": 3.5, "pass": True},
        {"level": 4, "pass": False, "detail": {"right_spot": stock_lights.right_spot(df)}},
    ]}


def test_gate_blocks_reads_l4_right_spot():
    df = _load("early_advance_extended")
    blocks = st.gate_blocks(_gate_with_l4(df))
    ids = {b["id"] for b in blocks}
    assert "extension" in ids and "atr_5d_ema" in ids
    assert all(b["level"] == 4 for b in blocks)


def test_gate_blocks_l2_and_l3_veto():
    gate = {"levels": [
        {"level": 2, "pass": False, "detail": {"deteriorating_reasons": ["rs1m_negative"],
                                               "rs1m": -1.2}},
        {"level": 3, "pass": False, "detail": {"vetoes": [
            {"id": "close_below_ma200", "tripped": True, "value": {}},
            {"id": "rs3m_vs_sector", "tripped": False}]}},
    ]}
    blocks = st.gate_blocks(gate)
    got = {(b["level"], b["id"]) for b in blocks}
    assert (2, "rs1m_negative") in got
    assert (3, "veto:close_below_ma200") in got
    assert not any(b["id"] == "veto:rs3m_vs_sector" for b in blocks)   # not tripped


def test_gate_blocks_none_is_empty():
    assert st.gate_blocks(None) == []


# ---------------------------------------------------------------------------
# The gate-complete verdict — Fixture D is the guard.
# ---------------------------------------------------------------------------
def test_fixture_d_ready_structure_but_gate_watch_binding_l4():
    df = _load("early_advance_extended")
    from structure_classifier import classify_symbol
    base, inst = classify_symbol(df)
    # The signal composition alone says READY (green regime + green SYM + entrable).
    composed = sv.compose_verdict("green", "green", base, inst)
    assert composed["verdict"] == sv.READY
    # The full gate fails at Level 4 -> the gate-complete verdict is WATCH, binding L4.
    blocks = st.gate_blocks(_gate_with_l4(df))
    rv = st.compose_row_verdict(composed, blocks)
    assert rv["verdict"] == sv.WATCH
    assert rv["binding"]["level"] == 4
    assert rv["binding"]["kind"] == st.ESTIMATED
    assert rv["reasons"][0].startswith("L4:")


def test_fixture_d_with_l5_still_binds_l4_and_shows_slot_trigger():
    df = _load("early_advance_extended")
    from structure_classifier import classify_symbol
    base, inst = classify_symbol(df)
    composed = sv.compose_verdict("green", "green", base, inst)
    account = {"blocking_failures": ["earnings_in_cycle", "sector_concentration"],
               "checks": [
                   {"id": "earnings_in_cycle", "blocking": True, "pass": False,
                    "detail": {"earnings": {"date": "2026-07-30", "days_until": 14}}},
                   {"id": "sector_concentration", "blocking": True, "pass": False,
                    "detail": {"sector": "XLK", "already_held": ["MSFT"]}},
               ]}
    blocks = st.gate_blocks(_gate_with_l4(df), account_gate=account)
    rv = st.compose_row_verdict(composed, blocks)
    assert rv["verdict"] == sv.WATCH
    assert rv["binding"]["level"] == 4                 # L4 < L5, so L4 still binds
    kinds = {t["id"]: t["kind"] for t in rv["triggers"]}
    assert kinds["earnings_in_cycle"] == st.CALENDAR
    assert kinds["sector_concentration"] == st.CONDITIONAL
    # Not bench — the L4 estimated + L5 blocks are all WAITs, no safety block.
    assert st.is_bench(rv["verdict"], rv["triggers"]) is True


# ---------------------------------------------------------------------------
# Net juice-floor SAFETY block — Fixture E is the guard (the PNC shape).
# ---------------------------------------------------------------------------
def test_juice_floor_block_two_tiers():
    import config
    floor = config.JUICE_FLOOR_WK
    # Hard tier: NET <= 0 (burn exceeds income), regardless of gross.
    assert st.juice_floor_block(-0.5, floor + 2)["observed"]["tier"] == "hard"
    assert st.juice_floor_block(0.0, floor + 2)["observed"]["tier"] == "hard"
    # Adequacy tier: NET positive but GROSS below the floor (thin premium).
    assert st.juice_floor_block(0.4, floor - 0.1)["observed"]["tier"] == "adequacy"
    # Clears: net positive AND gross at/above the floor.
    assert st.juice_floor_block(0.4, floor) is None
    assert st.juice_floor_block(1.0, floor + 1.0) is None
    # A None figure isn't blocked here (can't price -> other gates handle it).
    assert st.juice_floor_block(None, None) is None
    assert st.juice_floor_block(0.4, None) is None            # gross unknown -> no adequacy block


def test_juice_block_is_safety_blocked_and_never_bench():
    composed = sv.compose_verdict("green", "green", BaseStage.EARLY_ADVANCE,
                                  InstFlow.ACCUMULATING)  # pristine -> READY signals
    rv = st.compose_row_verdict(composed, [st.juice_floor_block(0.5, 1.0)])  # gross 1.0 < 1.5
    assert rv["verdict"] == sv.BLOCKED                    # safety, not a WATCH wait
    assert rv["binding"]["id"] == "juice_floor" and rv["binding"]["level"] == 5
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False
    # The binding phrases the economics: "gross juice 1.00% < floor 1.5%".
    assert "gross juice" in rv["binding"]["trigger"]["clears_when"]


def test_juice_hard_tier_names_the_negative_net():
    rv = st.compose_row_verdict(
        sv.compose_verdict("green", "green", BaseStage.EARLY_ADVANCE, InstFlow.ACCUMULATING),
        [st.juice_floor_block(-0.3, 2.0)])               # gross fine, but net negative
    assert rv["verdict"] == sv.BLOCKED
    assert "burn exceeds income" in rv["binding"]["trigger"]["clears_when"]


def test_juice_safety_binds_over_a_lower_level_extension_wait():
    # A sub-floor name that is ALSO slightly extended (L4) must bind on L5 juice
    # (the decisive safety block), not the L4 WATCH wait.
    df = _load("early_advance_extended")
    composed = sv.compose_verdict("green", "green", *__import__("structure_classifier").classify_symbol(df))
    blocks = st.gate_blocks(_gate_with_l4(df)) + [st.juice_floor_block(0.4, 1.0)]
    rv = st.compose_row_verdict(composed, blocks)
    assert rv["verdict"] == sv.BLOCKED
    assert rv["binding"]["id"] == "juice_floor"           # safety worst -> binds over L4
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False


def test_fixture_e_pnc_shape_blocks_on_juice(monkeypatch):
    # End-to-end over the PNC fixture: pristine structure + SYM green, but juice
    # below the viability floor => VERDICT BLOCKED, binding L5 juice, NOT on bench.
    import account_gate, structure_classifier
    df = _load("early_advance_low_juice")
    base, inst = structure_classifier.classify_symbol(df)
    assert base == BaseStage.EARLY_ADVANCE and inst == InstFlow.ACCUMULATING
    est = account_gate.juice_estimate("PNC", df)
    net, gross = est["net_weekly_yield_pct"], est["weekly_yield_pct"]
    block = st.juice_floor_block(net, gross)
    assert block is not None                                    # trips a tier
    composed = sv.compose_verdict("green", "green", base, inst)
    assert composed["verdict"] == sv.READY                      # signals alone say READY
    rv = st.compose_row_verdict(composed, [block])
    assert rv["verdict"] == sv.BLOCKED and rv["binding"]["level"] == 5
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False


# ---------------------------------------------------------------------------
# ~1D guard — a degenerate estimate renders as a condition word, never a count.
# ---------------------------------------------------------------------------
def test_extension_sub_day_estimate_is_condition_word_not_fabricated_1d():
    # excess just over the line, MA21 rising fast => days rounds to 0 => None (word),
    # never the old "or 1" fabricated ~1D.
    obs = {"excess_atr": 0.03, "ma21_rise_per_day": 1.5, "atr": 2.0}
    trig = st.classify({"level": 4, "id": "extension", "observed": obs})["trigger"]
    assert trig["days_estimate"] is None
    assert trig.get("estimated") is False                # renders as the condition word
    # A genuine multi-day estimate still comes through.
    obs2 = {"excess_atr": 2.0, "ma21_rise_per_day": 0.5, "atr": 2.0}
    assert st.classify({"level": 4, "id": "extension", "observed": obs2})["trigger"]["days_estimate"] == 8


def test_no_blocks_leaves_composed_verdict_untouched():
    # gate=None path (the many score_ticker(...) callers with a synthetic/None gate).
    composed = sv.compose_verdict("green", "green", BaseStage.EARLY_ADVANCE,
                                  InstFlow.ACCUMULATING)
    rv = st.compose_row_verdict(composed, [])
    assert rv["verdict"] == sv.READY and rv["reasons"] == [] and rv["binding"] is None
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False


def test_basing_intake_is_watch_only_never_bench():
    # A BASING x EARLY_INTEREST intake maps to structure entrability WATCH -> a
    # structure SIGNAL block. That is "interesting, not waiting": WATCH-only, never
    # bench. This is the WATCH/BENCH de-collapse.
    composed = sv.compose_verdict("green", "green", BaseStage.BASING, InstFlow.EARLY_INTEREST)
    rv = st.compose_row_verdict(composed, [])
    assert rv["verdict"] == sv.WATCH
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False   # signal-WATCH != bench


def test_yellow_sym_signal_is_watch_only_never_bench():
    composed = sv.compose_verdict("green", "yellow", BaseStage.EARLY_ADVANCE,
                                  InstFlow.ACCUMULATING)          # SYM yellow watchlist
    rv = st.compose_row_verdict(composed, [])
    assert rv["verdict"] == sv.WATCH
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False


def test_gate_block_with_clear_signals_is_bench():
    # Entrable structure + green SYM/regime, blocked only by an L4 estimate =>
    # BENCH ("waiting, with a schedule").
    df = _load("early_advance_extended")
    import structure_classifier
    composed = sv.compose_verdict("green", "green", *structure_classifier.classify_symbol(df))
    rv = st.compose_row_verdict(composed, st.gate_blocks(_gate_with_l4(df)))
    assert rv["verdict"] == sv.WATCH
    assert st.is_bench(rv["verdict"], rv["triggers"]) is True


def test_safety_block_excludes_from_bench():
    composed = sv.compose_verdict("green", "green", BaseStage.EARLY_ADVANCE,
                                  InstFlow.ACCUMULATING)
    gate = {"levels": [{"level": 3, "pass": False, "detail": {"vetoes": [
        {"id": "close_below_ma200", "tripped": True, "value": {}}]}}]}
    rv = st.compose_row_verdict(composed, st.gate_blocks(gate))
    assert rv["verdict"] == sv.BLOCKED               # safety block -> BLOCKED
    assert st.is_bench(rv["verdict"], rv["triggers"]) is False


def test_red_regime_signal_binds_before_gate_blocks():
    df = _load("early_advance_extended")
    from structure_classifier import classify_symbol
    base, inst = classify_symbol(df)
    composed = sv.compose_verdict("red", "green", base, inst)   # regime RED -> BLOCKED
    rv = st.compose_row_verdict(composed, st.gate_blocks(_gate_with_l4(df)))
    assert rv["verdict"] == sv.BLOCKED
    assert rv["binding"]["id"] == "regime" and rv["binding"]["level"] == 1


def test_path_to_ready_renders_legs():
    triggers = [
        st.classify({"level": 4, "id": "extension",
                     "observed": {"excess_atr": 2.0, "ma21_rise_per_day": 0.5, "atr": 2.0}}),
        st.classify({"level": 5, "id": "earnings_in_cycle",
                     "observed": {"earnings": {"date": "2026-07-30", "days_until": 14}}}),
        st.classify({"level": 5, "id": "sector_concentration", "observed": {}}),
    ]
    line = st.path_to_ready(triggers)
    # Rendering discipline: estimate tilded (~8d EST), calendar date plain (by …),
    # conditional as a word (slot).
    assert "~8d EST" in line and "by 2026-07-31" in line and "slot" in line
    assert st.earliest_eligible_days(triggers) == 8    # min(8 EST, 15 earnings)
