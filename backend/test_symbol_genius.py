"""Tests for Symbol Genius — the per-name four-light instance whose fourth light
deliberately diverges from the market regime (SMA50>SMA200 vs EMA21>SMA50).
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

import config
import genius_lights as gl
import symbol_genius as sg


def _frame(closes, start="2022-06-01"):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.bdate_range(start=start, periods=n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"Open": opens, "High": closes + 0.3, "Low": closes - 0.3,
         "Close": closes, "Volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Parameter isolation — the whole point of the divergence guardrail.
# ---------------------------------------------------------------------------
def test_params_have_no_fast_ma_and_carry_slower_ma():
    p = sg.default_params()
    assert "fast_ma" not in p                       # cannot inherit EMA21
    assert p["slower_ma"] == config.SYMBOL_GENIUS_SLOWER_MA == 200
    assert p["slow_ma"] == config.GENIUS_SLOW_MA == 50


def test_params_are_not_the_regime_param_set():
    assert sg.default_params() != gl.default_params()


def test_fourth_light_key_is_structure_not_fast_vs_slow():
    assert sg.LIGHT_KEYS == ("close_vs_ma", "structure", "sar", "momentum")


# ---------------------------------------------------------------------------
# The deliberate divergence: EMA21>SMA50 (regime) can disagree with SMA50>SMA200.
# ---------------------------------------------------------------------------
def test_fourth_light_uses_sma50_vs_sma200_not_ema21_vs_sma50():
    # A long downtrend that just turned up: EMA21 pops above SMA50 (regime light 2
    # GREEN) while SMA50 is still well below SMA200 (Symbol Genius light 4 RED).
    closes = np.concatenate([np.linspace(200, 120, 210), np.linspace(120, 150, 40)])
    df = _frame(closes)
    regime = gl.compute_lights(df)
    sym = sg.compute_lights(df)
    assert regime["fast_vs_slow"]["signal"] == "green"   # EMA21 > SMA50 (short clock)
    assert sym["structure"]["signal"] == "red"           # SMA50 < SMA200 (long clock)
    # And the structure light carries the SMA values, not EMA values.
    assert set(sym["structure"]) == {"signal", "slow_ma", "slower_ma"}


# ---------------------------------------------------------------------------
# Verdict mapping (the stock mapping: 4=GREEN, 3=YELLOW, <=2 or insufficient=RED).
# ---------------------------------------------------------------------------
def test_all_four_green_is_green():
    closes = np.concatenate([np.full(60, 100.0), np.linspace(100, 180, 200)])
    out = sg.compute(_frame(closes))
    assert out["greens"] == 4
    assert out["verdict"] == sg.GREEN


def test_exactly_three_green_is_yellow():
    # A clean advance that just stalls: close>SMA50, SMA50>SMA200 and ROC(10) all
    # stay green, but the Parabolic SAR (which accelerates right up under price in a
    # strong trend) flips the moment the advance flattens -> exactly 3 green ->
    # YELLOW (the watchlist "first warning" state, never enterable).
    closes = np.concatenate([np.full(60, 100.0), np.linspace(100, 180, 200),
                             np.linspace(180, 179.2, 6)])
    out = sg.compute(_frame(closes))
    lights = out["lights"]
    assert lights["sar"]["signal"] == "red"
    assert lights["close_vs_ma"]["signal"] == "green"
    assert lights["structure"]["signal"] == "green"
    assert lights["momentum"]["signal"] == "green"
    assert out["greens"] == 3
    assert out["verdict"] == sg.YELLOW


def test_downtrend_is_red():
    out = sg.compute(_frame(np.linspace(200, 110, 260)))
    assert out["greens"] <= 2
    assert out["verdict"] == sg.RED


# ---------------------------------------------------------------------------
# Warm-up: inside the SMA200 warm-up a name is insufficient -> RED, never GREEN.
# ---------------------------------------------------------------------------
def test_inside_warmup_is_red_even_if_lights_look_green():
    # A clean uptrend but only ~150 bars — SMA200 can't be formed, so structure is
    # None -> insufficient -> RED (never GREEN inside the warm-up).
    out = sg.compute(_frame(np.linspace(100, 160, 150)))
    assert out["insufficient"] is True
    assert out["verdict"] == sg.RED


def test_warmup_boundary_matches_config():
    assert sg.default_params()["warmup_bars"] == config.SYMBOL_LIGHTS_WARMUP_BARS == 200


# ---------------------------------------------------------------------------
# Fixtures: the divergence proven on the committed structure fixtures.
# ---------------------------------------------------------------------------
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "structure")


def test_symbol_genius_green_on_topping_fixture():
    # The crux Fixture-A property, now via the REAL Symbol Genius module: it reads
    # GREEN on the topping fixture even though the structure classifier says TOPPING.
    df = pd.read_parquet(os.path.join(FIX, "topping_distribution.parquet"))
    out = sg.compute(df)
    assert out["verdict"] == sg.GREEN
    # ...and the regime engine's fourth light is the one that would have blocked it.
    assert gl.compute_lights(df)["fast_vs_slow"]["signal"] == "red"
    assert out["lights"]["structure"]["signal"] == "green"


def test_symbol_genius_green_on_early_advance_fixture():
    df = pd.read_parquet(os.path.join(FIX, "early_advance_accum.parquet"))
    assert sg.compute(df)["verdict"] == sg.GREEN


# ---------------------------------------------------------------------------
# Purity / determinism, and the regime engine is untouched.
# ---------------------------------------------------------------------------
def test_compute_does_not_mutate_frame():
    df = _frame(np.linspace(100, 180, 260))
    before = df.copy()
    sg.compute(df)
    pd.testing.assert_frame_equal(df, before)


def test_regime_light_set_unchanged_by_new_light():
    # The new light function is additive: compute_lights still returns exactly the
    # regime's four keys (the byte-identical guarantee is not disturbed).
    df = _frame(np.linspace(100, 180, 260))
    assert set(gl.compute_lights(df)) == {"close_vs_ma", "fast_vs_slow", "sar", "momentum"}
