"""Validation rules: garbage data must be rejected with a reason, and one bad
bar must not poison the reference close for the bars after it."""
import numpy as np
import pandas as pd

import validation


def frame(rows):
    idx = pd.bdate_range("2026-06-01", periods=len(rows))
    return pd.DataFrame(rows, index=idx)


def base_row(close, **kw):
    row = {"Open": close * 0.99, "High": close * 1.01, "Low": close * 0.98,
           "Close": close, "Volume": 1_000_000.0}
    row.update(kw)
    return row


def test_null_close_rejected():
    bars = frame([base_row(100), base_row(np.nan)])
    accepted, rejected = validation.validate_bars("SPY", bars)
    assert len(accepted) == 1
    assert rejected[0]["reason"] == "close is null"


def test_zero_and_negative_close_rejected():
    bars = frame([base_row(100), base_row(0.0), base_row(-5.0)])
    accepted, rejected = validation.validate_bars("SPY", bars)
    assert len(accepted) == 1
    assert "<= 0" in rejected[0]["reason"]
    assert "<= 0" in rejected[1]["reason"]


def test_high_below_low_rejected():
    bad = base_row(100)
    bad["High"], bad["Low"] = 90.0, 110.0
    accepted, rejected = validation.validate_bars("SPY", frame([bad]))
    assert accepted.empty
    assert "high" in rejected[0]["reason"] and "low" in rejected[0]["reason"]


def test_negative_volume_rejected():
    accepted, rejected = validation.validate_bars(
        "SPY", frame([base_row(100, Volume=-1.0)]))
    assert accepted.empty
    assert "volume" in rejected[0]["reason"]


def test_spike_beyond_band_rejected_with_band_in_reason():
    bars = frame([base_row(100), base_row(140)])  # +40% > ±25% default
    accepted, rejected = validation.validate_bars("SPY", bars)
    assert len(accepted) == 1
    assert "±25%" in rejected[0]["reason"]


def test_vix_band_is_wider_per_symbol():
    bars = frame([base_row(20), base_row(32)])  # +60%
    accepted_vix, rejected_vix = validation.validate_bars("^VIX", bars)
    accepted_spy, rejected_spy = validation.validate_bars("SPY", bars)
    assert len(accepted_vix) == 2 and not rejected_vix   # within ±100%
    assert len(accepted_spy) == 1 and rejected_spy       # beyond ±25%


def test_rejected_bar_does_not_poison_reference_close():
    # 100 -> 500 (rejected) -> 102: the 102 bar must be judged against 100,
    # not against the rejected 500, so it is accepted.
    bars = frame([base_row(100), base_row(500), base_row(102)])
    accepted, rejected = validation.validate_bars("SPY", bars)
    assert list(accepted["Close"]) == [100, 102]
    assert len(rejected) == 1


def test_prior_close_seeds_the_chain():
    bars = frame([base_row(200)])  # +100% vs stored history close of 100
    accepted, rejected = validation.validate_bars("SPY", bars, prior_close=100.0)
    assert accepted.empty and len(rejected) == 1
    # without a seed, a lone first bar is accepted
    accepted, rejected = validation.validate_bars("SPY", bars)
    assert len(accepted) == 1 and not rejected
