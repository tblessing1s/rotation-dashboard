"""Deterministic builder for the labeled regime regression fixtures.

These are SYNTHESIZED daily OHLCV series that stand in for known SPY market
shapes — they are NOT fetched from any provider (the regime tests must run fully
offline). Each shape is hand-designed to exercise one regime behaviour, written
to a clearly-named parquet file next to this script, and asserted against in
``test_regime_regression.py``:

  * sustained_green.parquet      — a confirmed uptrend the regime must hold GREEN
                                    through, with no flaps.
  * distribution_rollover.parquet — a topping/rollover into a correction the
                                    regime must degrade GREEN -> YELLOW -> RED
                                    through, never jumping GREEN <-> RED
                                    day-over-day.
  * v_bottom_whipsaw.parquet     — a fresh-yellow tape with a 1-day up-spike; the
                                    yellow dwell must suppress a 1-day
                                    yellow -> green -> yellow round trip.

Regenerate with:  python -m fixtures.regime.build_fixtures   (from backend/)
The parquet outputs are committed so tests never rebuild or fetch.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))


def _ohlcv(closes: list[float], highs=None, lows=None, start="2022-01-03") -> pd.DataFrame:
    """Assemble an ascending business-day OHLCV frame from a close path. High/Low
    default to a tight band around the close (High/Low drive the Parabolic SAR)."""
    n = len(closes)
    idx = pd.bdate_range(start=start, periods=n)
    closes = np.asarray(closes, dtype=float)
    if highs is None:
        highs = closes + 0.25
    if lows is None:
        lows = closes - 0.25
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"Open": opens, "High": np.asarray(highs, float),
         "Low": np.asarray(lows, float), "Close": closes,
         "Volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


def sustained_green() -> pd.DataFrame:
    """A steady, low-noise uptrend — every light stays green through the tail."""
    rng = np.random.default_rng(1)
    steps = rng.normal(0.35, 0.25, 220)      # positive drift, small vol
    closes = 300 + np.cumsum(np.clip(steps, -0.4, 1.2))
    return _ohlcv(closes)


def distribution_rollover() -> pd.DataFrame:
    """Uptrend -> rounded top -> steady decline into a correction. Designed so the
    four lights flip ONE AT A TIME (close, then momentum, then fast/slow, then
    SAR), so the raw vote steps green(4) -> green(3) -> yellow(2) -> red(1) -> red,
    i.e. it degrades through yellow rather than gapping green->red."""
    up = 300 + np.linspace(0, 70, 140)                         # long advance
    top = up[-1] + (-0.0025) * (np.arange(30) - 15) ** 2       # gentle rounded top
    down = top[-1] - np.linspace(0, 90, 90)                    # slow, orderly decline
    closes = np.concatenate([up, top, down])
    return _ohlcv(closes)


def v_bottom_whipsaw() -> pd.DataFrame:
    """A tape chopping right at the green/yellow regime boundary, with a single
    sharp up-spike (a one-day bear-market rally) that pops the RAW four-light vote
    to green for exactly one bar. Without the dwell the published regime would
    round-trip yellow -> green -> yellow in a day; the 3-day yellow dwell must hold
    it yellow through the blip. The spike index and RNG seed are fixed so the
    fixture is byte-for-byte reproducible."""
    rng = np.random.default_rng(0)
    base = 300 + np.linspace(0, 40, 100)                 # establish the moving averages
    chop = base[-1] + np.cumsum(rng.normal(0.0, 0.9, 80))  # boundary chop -> yellow
    closes = np.concatenate([base, chop]).astype(float)
    highs = closes + 0.20
    lows = closes - 0.20
    spike_i = 113                                        # one-day up-spike inside the chop
    closes[spike_i] += 6.0
    highs[spike_i] = closes[spike_i] + 1.0
    return _ohlcv(list(closes), highs=highs, lows=lows)


FIXTURES = {
    "sustained_green": sustained_green,
    "distribution_rollover": distribution_rollover,
    "v_bottom_whipsaw": v_bottom_whipsaw,
}


def build() -> None:
    for name, fn in FIXTURES.items():
        df = fn()
        path = os.path.join(HERE, f"{name}.parquet")
        df.to_parquet(path)
        print(f"wrote {path}  ({len(df)} bars, "
              f"{df.index[0].date()} -> {df.index[-1].date()})")


if __name__ == "__main__":
    build()
