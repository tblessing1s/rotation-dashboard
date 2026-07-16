"""Tests for the shared scan VERDICT composition (worst-signal-wins)."""
from __future__ import annotations

import os

import pandas as pd
import pytest

import genius_lights as gl
import scan_verdict as sv
import structure_classifier as sclf
import symbol_genius as sg
from structure_classifier import BaseStage, InstFlow

GREEN, YELLOW, RED = gl.GREEN, gl.YELLOW, gl.RED
# A structure cell that is entrable (READY) and one that blocks, for isolation.
READY_CELL = (BaseStage.EARLY_ADVANCE, InstFlow.ACCUMULATING)      # -> READY
CAUTION_CELL = (BaseStage.LATE_ADVANCE, InstFlow.ACCUMULATING)     # -> CAUTION
WATCH_CELL = (BaseStage.BASING, InstFlow.EARLY_INTEREST)           # -> WATCH
BLOCK_CELL = (BaseStage.TOPPING, InstFlow.ACCUMULATING)            # -> BLOCKED


def _v(regime, symbol, cell):
    return sv.compose_verdict(regime, symbol, cell[0], cell[1])["verdict"]


# ---------------------------------------------------------------------------
# All-clear and the single-blocker dominance (worst-signal-wins).
# ---------------------------------------------------------------------------
def test_all_clear_is_ready():
    assert _v(GREEN, GREEN, READY_CELL) == sv.READY


def test_red_regime_blocks_everything():
    # The invisible-regime rule: a RED regime BLOCKS even a green SYM + entrable cell.
    assert _v(RED, GREEN, READY_CELL) == sv.BLOCKED


def test_red_symbol_blocks():
    assert _v(GREEN, RED, READY_CELL) == sv.BLOCKED


def test_blocking_structure_blocks_even_with_green_lights():
    # The Fixture-A shape in the abstract: green regime + green SYM, but TOPPING.
    assert _v(GREEN, GREEN, BLOCK_CELL) == sv.BLOCKED


def test_caution_structure_carries_when_others_clear():
    assert _v(GREEN, GREEN, CAUTION_CELL) == sv.CAUTION


def test_yellow_symbol_is_watch_not_ready():
    # SYM YELLOW = watchlist, never enterable -> WATCH even on an entrable cell.
    assert _v(GREEN, YELLOW, READY_CELL) == sv.WATCH


def test_yellow_regime_is_watch():
    assert _v(YELLOW, GREEN, READY_CELL) == sv.WATCH


def test_worst_of_watch_and_caution_is_watch():
    # SYM yellow (WATCH) beats a CAUTION structure cell -> WATCH (more restrictive).
    assert _v(GREEN, YELLOW, CAUTION_CELL) == sv.WATCH


def test_unknown_regime_never_ready():
    assert _v(None, GREEN, READY_CELL) != sv.READY


@pytest.mark.parametrize("regime,symbol,cell,expected", [
    (GREEN, GREEN, READY_CELL, sv.READY),
    (GREEN, GREEN, CAUTION_CELL, sv.CAUTION),
    (GREEN, GREEN, WATCH_CELL, sv.WATCH),
    (GREEN, GREEN, BLOCK_CELL, sv.BLOCKED),
    (YELLOW, GREEN, READY_CELL, sv.WATCH),
    (RED, GREEN, READY_CELL, sv.BLOCKED),
    (GREEN, YELLOW, READY_CELL, sv.WATCH),
    (GREEN, RED, READY_CELL, sv.BLOCKED),
])
def test_truth_table(regime, symbol, cell, expected):
    assert _v(regime, symbol, cell) == expected


def test_reasons_name_the_worst_inputs():
    out = sv.compose_verdict(RED, GREEN, *BLOCK_CELL)
    assert out["verdict"] == sv.BLOCKED
    # Both the regime and the structure are at BLOCKED; both are named.
    assert "regime:BLOCKED" in out["reasons"]
    assert "structure:BLOCKED" in out["reasons"]
    assert not any(r.startswith("symbol") for r in out["reasons"])  # symbol was READY


# ---------------------------------------------------------------------------
# End-to-end on the committed fixtures (real modules, not hand-rolled signals).
# ---------------------------------------------------------------------------
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "structure")


def _signals(name):
    df = pd.read_parquet(os.path.join(FIX, f"{name}.parquet"))
    base, inst = sclf.classify_symbol(df)
    return sg.compute(df)["color"], base, inst


def test_fixture_a_composes_to_blocked_under_any_regime():
    # topping_distribution: SYM GREEN + TOPPING. BLOCKED regardless of regime — the
    # structure input alone blocks it, which is the whole Fixture-A point.
    sym, base, inst = _signals("topping_distribution")
    assert sym == GREEN and base == BaseStage.TOPPING
    for regime in (GREEN, YELLOW, RED):
        assert sv.compose_verdict(regime, sym, base, inst)["verdict"] == sv.BLOCKED


def test_fixture_b_ready_in_green_regime_blocked_in_red():
    # early_advance_accum: SYM GREEN + EARLY_ADVANCE x ACCUMULATING (entrable).
    # READY in a green regime; a RED regime must flip it to BLOCKED (the invisible
    # regime pin, now a real composition — previously deferred to this step).
    sym, base, inst = _signals("early_advance_accum")
    assert sym == GREEN
    assert sv.compose_verdict(GREEN, sym, base, inst)["verdict"] == sv.READY
    assert sv.compose_verdict(RED, sym, base, inst)["verdict"] == sv.BLOCKED
    assert sv.compose_verdict(YELLOW, sym, base, inst)["verdict"] == sv.WATCH
