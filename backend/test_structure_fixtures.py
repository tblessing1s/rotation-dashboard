"""Regression pins for the structure-classifier fixtures (backend/fixtures/
structure/). These are the Fixture A / Fixture B artifacts the audit called for —
>=250 bars and volume-varied, so the classifier returns real stages instead of
INSUFFICIENT_DATA (unlike the constant-volume regime fixtures).

Fixture A (topping_distribution): the crux case. The Genius four-light read is
NOT red — in fact the per-name Symbol Genius (which swaps the regime's EMA21>SMA50
fourth light for SMA50>SMA200) reads 4/4 GREEN — yet the STRUCTURE classifier
reads BaseStage=TOPPING and the cell is BLOCKED. This proves trend/momentum
lights alone are insufficient; the fixture is deliberately NOT "fixed" to red.

Fixture B (early_advance_accum): SYM green AND an entrable structure cell
(EARLY_ADVANCE x ACCUMULATING -> READY). Under a RED market regime the composed
VERDICT must still be BLOCKED — the invisible-regime-input pin. The composition
assertion lands with the shared VERDICT function (a later step); here we pin the
two inputs it will consume (SYM green, structure READY).
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

import genius_lights as gl
import indicators
import rs_state as rss
import scan_verdict as sv
import structure_classifier as sc
import symbol_genius
from structure_classifier import BaseStage, InstFlow, Entrability

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "structure")


def _load(name):
    return pd.read_parquet(os.path.join(FIX, f"{name}.parquet"))


def _symbol_genius_greens(df):
    """The per-name Symbol Genius green count WITHOUT importing the (not-yet-built)
    light-set: the three lights it shares with the regime engine (close>SMA50,
    SAR<close, ROC10>0) plus its divergent fourth light SMA50>SMA200. Lets these
    fixtures pin the 'Symbol Genius is green' property today."""
    lights = gl.compute(df)["lights"]
    shared = sum(lights[k]["signal"] == "green" for k in ("close_vs_ma", "sar", "momentum"))
    sma50, sma200 = indicators.sma(df, 50), indicators.sma(df, 200)
    fourth = 1 if (sma50 is not None and sma200 is not None and sma50 > sma200) else 0
    return shared + fourth


# ---------------------------------------------------------------------------
# Fixture A — topping while the lights still say go
# ---------------------------------------------------------------------------
def test_fixture_a_has_enough_history():
    assert len(_load("topping_distribution")) >= sc.MIN_BARS_BASE


def test_fixture_a_classifier_is_topping_and_blocked():
    df = _load("topping_distribution")
    base, inst = sc.classify_symbol(df)
    assert base == BaseStage.TOPPING
    assert inst == InstFlow.DISTRIBUTING          # report: the flow it yields
    assert sc.structure_entrability(base, inst) == Entrability.BLOCKED


def test_fixture_a_symbol_genius_is_not_red():
    # The whole point: lights alone would NOT block this. Symbol Genius is GREEN
    # (4/4) — its SMA50>SMA200 fourth light is green even though the regime engine's
    # EMA21>SMA50 light is not, which is exactly the deliberate divergence.
    df = _load("topping_distribution")
    assert gl.compute(df)["color"] != "red"       # regime-style vote isn't red either
    assert _symbol_genius_greens(df) == 4          # Symbol Genius would verdict GREEN


# ---------------------------------------------------------------------------
# Fixture B — entrable structure + green SYM, to be BLOCKED by a red regime
# ---------------------------------------------------------------------------
def test_fixture_b_has_enough_history():
    assert len(_load("early_advance_accum")) >= sc.MIN_BARS_BASE


def test_fixture_b_classifier_is_early_advance_accumulating_ready():
    df = _load("early_advance_accum")
    base, inst = sc.classify_symbol(df)
    assert base == BaseStage.EARLY_ADVANCE
    assert inst in (InstFlow.ACCUMULATING, InstFlow.EARLY_INTEREST)
    assert sc.structure_entrability(base, inst) == Entrability.READY


def test_fixture_b_symbol_genius_is_green():
    assert _symbol_genius_greens(_load("early_advance_accum")) == 4


def test_fixture_b_red_regime_composes_to_blocked():
    # The invisible-regime pin, now via the real shared VERDICT: both stock-level
    # inputs are "go" (structure READY, SYM green), so ONLY the regime can block —
    # and a RED regime does, even though regime is never displayed per-row.
    import scan_verdict as sv
    df = _load("early_advance_accum")
    base, inst = sc.classify_symbol(df)
    assert sc.structure_entrability(base, inst) == Entrability.READY
    assert _symbol_genius_greens(df) == 4
    assert sv.compose_verdict("green", "green", base, inst)["verdict"] == "READY"
    assert sv.compose_verdict("red", "green", base, inst)["verdict"] == "BLOCKED"


# ---------------------------------------------------------------------------
# Fixture A RS-state extension — the two-speed RS shadow reads FADING
# (distribution-into-strength: led its sector on 3M, but rolling over now).
# ---------------------------------------------------------------------------
def test_fixture_a_rs_state_is_fading():
    stock = _load("topping_distribution")
    sector = _load("topping_distribution_sector")
    st = rss.rs_state(stock, sector)
    assert st["level"] is not None and st["level"] >= 0      # led its sector over 3M
    assert st["slope"] is not None and st["slope"] < 0        # but rolling over now
    assert st["state"] == rss.FADING


# ---------------------------------------------------------------------------
# Fixture C — the NVDA shape: EARLY_ADVANCE x EARLY_INTEREST, RS TURNING, and a
# non-READY verdict whose binding constraint is the SYM (Level-3) input.
# ---------------------------------------------------------------------------
def test_fixture_c_has_enough_history():
    assert len(_load("turning_recovery")) >= sc.MIN_BARS_BASE


def test_fixture_c_classifier_is_early_advance_early_interest():
    df = _load("turning_recovery")
    base, inst = sc.classify_symbol(df)
    assert base == BaseStage.EARLY_ADVANCE
    assert inst == InstFlow.EARLY_INTEREST
    # The structure cell itself is entrable — the block comes from SYM, not structure.
    assert sc.structure_entrability(base, inst) == Entrability.READY


def test_fixture_c_rs_state_is_turning():
    stock = _load("turning_recovery")
    sector = _load("turning_recovery_sector")
    st = rss.rs_state(stock, sector)
    assert st["level"] is not None and st["level"] < 0        # lags its sector on 3M
    assert st["slope"] is not None and st["slope"] > 0        # but recovering now
    assert st["state"] == rss.TURNING


def test_fixture_c_symbol_genius_is_yellow_not_green():
    # SMA50 is still below SMA200 (golden cross not yet), so the divergent fourth
    # light is red -> 3/4 -> YELLOW. This is the input that blocks the verdict.
    df = _load("turning_recovery")
    assert symbol_genius.compute(df)["color"] == "yellow"


def test_fixture_c_verdict_non_ready_binding_is_symbol_level3():
    # Structure READY + SYM yellow under a green regime -> WATCH (non-READY). The
    # binding constraint (the worst input) is the SYM / stock-lights read = Level 3.
    stock = _load("turning_recovery")
    base, inst = sc.classify_symbol(stock)
    sym = symbol_genius.compute(stock)["color"]
    composed = sv.compose_verdict("green", sym, base, inst)
    assert composed["verdict"] != sv.READY
    assert composed["reasons"] == ["symbol:WATCH"]            # SYM is the binding input


def test_fixture_c_turning_annotation_present_on_non_ready():
    # The gated Phase-0 exception: a TURNING vs-Sector RS annotates an already-
    # non-READY row's reasons — informational only, never a verdict change.
    stock = _load("turning_recovery")
    sector = _load("turning_recovery_sector")
    base, inst = sc.classify_symbol(stock)
    sym = symbol_genius.compute(stock)["color"]
    verdict = sv.compose_verdict("green", sym, base, inst)["verdict"]
    state = rss.rs_state(stock, sector)["state"]
    assert rss.turning_watch_reason(verdict, state) == rss.WATCH_ANNOTATION
    # ...but a READY row is never annotated, even if TURNING.
    assert rss.turning_watch_reason(sv.READY, rss.TURNING) is None


# ---------------------------------------------------------------------------
# Determinism (the fixtures are committed; classification must be stable)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["topping_distribution", "early_advance_accum",
                                  "turning_recovery"])
def test_classification_is_deterministic(name):
    df = _load(name)
    assert sc.classify_symbol(df) == sc.classify_symbol(df.copy())
