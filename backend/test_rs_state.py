"""Two-speed RS shadow — the RS-line primitives (indicators) + the four-state
collapse (rs_state). Pure, offline, no provider calls."""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators
import rs_state as rss


def _frame(closes, start="2022-01-03"):
    closes = np.asarray(closes, float)
    n = len(closes)
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame({"Open": closes, "High": closes + 0.5, "Low": closes - 0.5,
                         "Close": closes, "Volume": np.full(n, 1_000_000.0)}, index=idx)


# ---------------------------------------------------------------------------
# RS-line primitives
# ---------------------------------------------------------------------------
def test_rs_line_is_the_close_ratio():
    stock = _frame(np.linspace(100, 120, 80))
    bench = _frame(np.linspace(100, 110, 80))
    line = indicators.rs_line(stock, bench)
    assert line is not None
    # Last point equals close/close.
    assert abs(line.iloc[-1] - stock["Close"].iloc[-1] / bench["Close"].iloc[-1]) < 1e-9


def test_rs_line_none_on_missing_or_empty():
    good = _frame(np.linspace(100, 120, 80))
    assert indicators.rs_line(None, good) is None
    assert indicators.rs_line(good, None) is None
    assert indicators.rs_line(good, _frame([], )) is None


def test_rs_ema_slope_sign_tracks_recent_outperformance():
    # Stock flat vs a bench that first outran it then flattened -> the RS line
    # (stock/bench) falls then rises: recent slope positive.
    n = 90
    stock = _frame(np.full(n, 100.0))
    bench = np.concatenate([np.linspace(100, 130, n - 21), np.linspace(130, 120, 21)])
    slope = indicators.rs_ema_slope(stock, _frame(bench))
    assert slope is not None and slope > 0


def test_rs_ema_slope_none_with_insufficient_history():
    short = _frame(np.linspace(100, 110, 10))
    assert indicators.rs_ema_slope(short, short) is None


# ---------------------------------------------------------------------------
# Four-state collapse — the truth table
# ---------------------------------------------------------------------------
def test_collapse_truth_table():
    assert rss.collapse(5.0, 1.0) == rss.RISING     # level>=0, slope up
    assert rss.collapse(5.0, -1.0) == rss.FADING    # level>=0, slope down
    assert rss.collapse(-5.0, 1.0) == rss.TURNING   # level<0,  slope up
    assert rss.collapse(-5.0, -1.0) == rss.FALLING  # level<0,  slope down


def test_collapse_boundaries_are_inclusive_up():
    # Exactly-zero level counts as "not underperforming"; flat slope counts as up.
    assert rss.collapse(0.0, 0.0) == rss.RISING
    assert rss.collapse(-0.01, 0.0) == rss.TURNING


def test_collapse_none_on_missing_input():
    assert rss.collapse(None, 1.0) is None
    assert rss.collapse(1.0, None) is None


def test_rs_state_bundles_level_and_slope():
    stock = _frame(np.linspace(100, 140, 90))
    bench = _frame(np.linspace(100, 110, 90))       # stock strongly outperforms
    st = rss.rs_state(stock, bench)
    assert st["state"] == rss.RISING
    assert st["level"] is not None and st["slope"] is not None


def test_rs_state_none_state_when_data_short():
    short = _frame(np.linspace(100, 110, 10))
    st = rss.rs_state(short, short)
    assert st["state"] is None


# ---------------------------------------------------------------------------
# The gated WATCH annotation (never changes a verdict)
# ---------------------------------------------------------------------------
def test_turning_watch_reason_only_annotates_non_ready_turning():
    assert rss.turning_watch_reason("WATCH", rss.TURNING) == rss.WATCH_ANNOTATION
    assert rss.turning_watch_reason("BLOCKED", rss.TURNING) == rss.WATCH_ANNOTATION
    assert rss.turning_watch_reason("READY", rss.TURNING) is None      # never on READY
    assert rss.turning_watch_reason("WATCH", rss.RISING) is None       # only TURNING
    assert rss.turning_watch_reason("WATCH", None) is None
