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
import structure_classifier as sc
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


def test_fixture_b_red_regime_would_block_is_pending_shared_verdict():
    # The composed VERDICT (worst-signal-wins of Market Genius + Symbol Genius +
    # structure entrability) lands in a later step. Here we pin the two stock-level
    # inputs it consumes; the regime is the invisible third input that must force
    # BLOCKED when red. Both inputs are individually "go", so ONLY the regime can
    # block it — which is exactly what makes this the invisible-regime pin.
    df = _load("early_advance_accum")
    base, inst = sc.classify_symbol(df)
    assert sc.structure_entrability(base, inst) == Entrability.READY
    assert _symbol_genius_greens(df) == 4


# ---------------------------------------------------------------------------
# Determinism (the fixtures are committed; classification must be stable)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["topping_distribution", "early_advance_accum"])
def test_classification_is_deterministic(name):
    df = _load(name)
    assert sc.classify_symbol(df) == sc.classify_symbol(df.copy())
