"""Unit tests for the Genius four-light regime (regime_genius.py) — pure, offline.

Covers: each light; a HAND-COMPUTED Parabolic SAR fixture (not a library); the
full 16-combination vote table; the yellow-dwell hysteresis edge cases (hold
through day 3, day-4 release, re-yellow inside the window, a raw crash held,
cold start); and the downgrade-only vetoes (breadth + VIX never upgrade).
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-regime-"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import indicators  # noqa: E402
import regime_genius as rg  # noqa: E402


def _df(closes, highs=None, lows=None):
    n = len(closes)
    idx = pd.bdate_range("2023-01-02", periods=n)
    closes = np.asarray(closes, float)
    highs = closes + 0.25 if highs is None else np.asarray(highs, float)
    lows = closes - 0.25 if lows is None else np.asarray(lows, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                         "Close": closes, "Volume": np.full(n, 1e6)}, index=idx)


# ---------------------------------------------------------------------------
# The four lights
# ---------------------------------------------------------------------------
def test_light_close_vs_ma():
    assert rg.light_close_vs_ma(101, 100)["signal"] == "green"
    assert rg.light_close_vs_ma(99, 100)["signal"] == "red"
    assert rg.light_close_vs_ma(100, 100)["signal"] == "red"   # "above" is strict
    assert rg.light_close_vs_ma(None, 100)["signal"] is None


def test_light_fast_vs_slow():
    assert rg.light_fast_vs_slow(105, 100)["signal"] == "green"
    assert rg.light_fast_vs_slow(95, 100)["signal"] == "red"
    assert rg.light_fast_vs_slow(100, None)["signal"] is None


def test_light_sar():
    # SAR dots UNDER price -> green (uptrend); overhead -> red.
    assert rg.light_sar(close=100, sar=95)["signal"] == "green"
    assert rg.light_sar(close=100, sar=105)["signal"] == "red"
    assert rg.light_sar(close=None, sar=105)["signal"] is None


def test_light_momentum():
    assert rg.light_momentum(1.5)["signal"] == "green"
    assert rg.light_momentum(-1.5)["signal"] == "red"
    assert rg.light_momentum(0.0)["signal"] == "red"          # "above zero" is strict
    assert rg.light_momentum(None)["signal"] is None


# ---------------------------------------------------------------------------
# Parabolic SAR — hand-computed fixture (standard Wilder, af 0.02 / 0.20)
# ---------------------------------------------------------------------------
def test_parabolic_sar_hand_fixture():
    """Seven bars worked through Wilder's recursion by hand:

        bar  high   low
         0   10.0    9.0
         1   11.0    9.5   init long: EP=11.0, SAR(1)=low[0]=9.0, AF=0.02
         2   12.0   10.0   SAR=9.04 -> clamp to 9.0 ; new high -> EP=12.0, AF=0.04
         3   12.5   10.5   SAR=9.12                 ; new high -> EP=12.5, AF=0.06
         4   13.0   11.0   SAR=9.3228               ; new high -> EP=13.0, AF=0.08
         5   11.5    9.0   SAR=9.616976 ; low 9.0 < SAR -> FLIP short, SAR=EP=13.0
         6   11.0    8.5   short: SAR=12.92 -> clamp up to 13.0
    """
    highs = [10.0, 11.0, 12.0, 12.5, 13.0, 11.5, 11.0]
    lows = [9.0, 9.5, 10.0, 10.5, 11.0, 9.0, 8.5]
    df = _df([h - 0.4 for h in highs], highs=highs, lows=lows)
    sar = indicators.parabolic_sar(df, af_step=0.02, af_max=0.20)
    expected = [None, 9.0, 9.0, 9.12, 9.3228, 13.0, 13.0]
    assert sar[0] is None
    for got, want in zip(sar[1:], expected[1:]):
        assert got == pytest.approx(want, abs=1e-6)
    # And the convenience latest-value helper agrees.
    assert indicators.parabolic_sar_last(df) == pytest.approx(13.0, abs=1e-6)


def test_parabolic_sar_too_short():
    assert indicators.parabolic_sar(_df([100.0])) is None


def test_ema_and_roc_basics():
    df = _df(list(np.linspace(100, 120, 60)))
    assert indicators.ema(df, 21) is not None
    # rising series -> positive ROC
    assert indicators.roc(df, 10) > 0
    assert indicators.roc(_df([100.0] * 5), 10) is None       # insufficient history


# ---------------------------------------------------------------------------
# Vote — all 16 light combinations
# ---------------------------------------------------------------------------
def _lights(signals):
    keys = ("close_vs_ma", "fast_vs_slow", "sar", "momentum")
    return {k: {"signal": s} for k, s in zip(keys, signals)}


def test_vote_all_16_combinations():
    from itertools import product
    for combo in product(["green", "red"], repeat=4):
        greens = combo.count("green")
        res = rg.vote(_lights(combo))
        if greens >= 3:
            expected = "green"
        elif greens <= 1:
            expected = "red"
        else:
            expected = "yellow"
        assert res["raw_condition"] == expected, (combo, res)
        assert res["green_count"] == greens


def test_vote_insufficient_is_yellow():
    res = rg.vote(_lights(["green", "green", "green", None]))
    assert res["raw_condition"] == "yellow"
    assert res["insufficient"] is True


# ---------------------------------------------------------------------------
# Yellow dwell (3 trading days) — the exact edge behaviour
# ---------------------------------------------------------------------------
def test_dwell_cold_start_publishes_raw():
    assert rg.apply_dwell("green", [])["regime"] == "green"
    assert rg.apply_dwell("red", [])["regime"] == "red"
    d = rg.apply_dwell("yellow", [])           # cold-start yellow starts the clock
    assert d["regime"] == "yellow" and d["dwell_day"] == 1 and d["cold_start"] is True


def test_dwell_holds_yellow_through_day3_then_releases_day4():
    # Day 1 enter yellow; days 2-3 raw green but published holds yellow; day 4 releases.
    assert rg.apply_dwell("yellow", [])["regime"] == "yellow"                      # day 1
    d2 = rg.apply_dwell("green", ["yellow"])                                       # day 2
    assert d2["regime"] == "yellow" and d2["held_by_dwell"] is True
    d3 = rg.apply_dwell("green", ["yellow", "yellow"])                             # day 3
    assert d3["regime"] == "yellow" and d3["held_by_dwell"] is True
    d4 = rg.apply_dwell("green", ["yellow", "yellow", "yellow"])                   # day 4
    assert d4["regime"] == "green" and d4["held_by_dwell"] is False


def test_dwell_release_is_off_by_one_safe():
    # Exactly 3 yellow days published -> the 4th evaluation releases, not the 3rd.
    assert rg.apply_dwell("green", ["yellow", "yellow"])["regime"] == "yellow"     # only 2 so far
    assert rg.apply_dwell("green", ["yellow", "yellow", "yellow"])["regime"] == "green"


def test_dwell_reyellow_inside_window_does_not_reset():
    # yellow, (raw green held), yellow again -> still releases on day 4 (measured
    # from the original entry, not reset by the re-yellow).
    d3 = rg.apply_dwell("yellow", ["yellow", "yellow"])          # day 3 raw re-yellow
    assert d3["regime"] == "yellow" and d3["dwell_day"] == 3
    d4 = rg.apply_dwell("green", ["yellow", "yellow", "yellow"])
    assert d4["regime"] == "green"


def test_dwell_holds_yellow_against_a_raw_crash():
    # The course rule: a yellow condition cannot change for N days regardless of the
    # raw vote — a raw RED inside the window is also held to yellow.
    d = rg.apply_dwell("red", ["yellow"])
    assert d["regime"] == "yellow" and d["held_by_dwell"] is True


def test_dwell_does_not_apply_to_green_or_red_episodes():
    # A green->red raw move (no yellow episode active) follows the raw vote.
    assert rg.apply_dwell("red", ["green", "green"])["regime"] == "red"
    assert rg.apply_dwell("green", ["red", "red"])["regime"] == "green"


# ---------------------------------------------------------------------------
# Vetoes — downgrade-only, never upgrade
# ---------------------------------------------------------------------------
def test_breadth_veto_downgrades_green_to_yellow():
    v = rg.apply_vetoes("green", breadth=40.0, vix=15.0)       # weak breadth
    assert v["regime"] == "yellow" and v["breadth"]["fired"] is True


def test_vix_veto_downgrades_green_to_yellow():
    v = rg.apply_vetoes("green", breadth=80.0, vix=30.0)       # VIX spike
    assert v["regime"] == "yellow" and v["vix"]["fired"] is True


def test_vetoes_never_upgrade():
    # A red regime with pristine breadth + calm VIX must stay red.
    v = rg.apply_vetoes("red", breadth=95.0, vix=10.0)
    assert v["regime"] == "red"
    assert v["breadth"]["fired"] is False and v["vix"]["fired"] is False
    # A yellow regime is never upgraded to green either.
    assert rg.apply_vetoes("yellow", breadth=95.0, vix=10.0)["regime"] == "yellow"


def test_green_survives_confirming_signals():
    v = rg.apply_vetoes("green", breadth=80.0, vix=15.0)
    assert v["regime"] == "green"


def test_missing_veto_inputs_do_not_fire():
    v = rg.apply_vetoes("green", breadth=None, vix=None)
    assert v["regime"] == "green"
    assert v["breadth"]["fired"] is False and v["vix"]["fired"] is False


# ---------------------------------------------------------------------------
# compose — the full trace end to end
# ---------------------------------------------------------------------------
def test_compute_trace_rising_is_green():
    df = _df(list(np.linspace(100, 160, 120)))
    tr = rg.compute_trace(df, breadth=75.0, vix=15.0, prior_published=[])
    assert tr["raw_condition"] == "green"
    assert tr["published_regime"] == "green"
    assert tr["status"] == tr["published_regime"]              # backward-compat mirror
    assert set(tr["lights"]) == {"close_vs_ma", "fast_vs_slow", "sar", "momentum"}


def test_compute_trace_green_vote_but_weak_breadth_publishes_yellow():
    df = _df(list(np.linspace(100, 160, 120)))
    tr = rg.compute_trace(df, breadth=35.0, vix=15.0, prior_published=[])
    assert tr["dwell_regime"] == "green"                       # vote + dwell said green
    assert tr["published_regime"] == "yellow"                  # breadth veto downgraded it
    assert tr["vetoes"]["breadth"]["fired"] is True


def test_compute_trace_falling_is_red():
    df = _df(list(np.linspace(160, 100, 120)))
    tr = rg.compute_trace(df, breadth=30.0, vix=30.0, prior_published=[])
    assert tr["raw_condition"] == "red"
    assert tr["published_regime"] == "red"
