"""Tests for the pure structure classifier.

Synthetic OHLCV frames are built inline with VARIED volume (the regime fixtures
use constant volume, which cannot exercise InstFlow — see audit Q7). Each frame
is hand-shaped to land one stage/flow; thresholds are PROPOSED_DEFAULT so these
tests also pin the current calibration.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import structure_classifier as sc
from structure_classifier import BaseStage, InstFlow, Entrability


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------
def _frame(closes, volumes=None, high_pad=0.3, low_pad=0.3, start="2023-01-02"):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)
    volumes = np.asarray(volumes, dtype=float)
    idx = pd.bdate_range(start=start, periods=n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"Open": opens, "High": closes + high_pad, "Low": closes - low_pad,
         "Close": closes, "Volume": volumes},
        index=idx,
    )


def _rising(n=280, base=100.0, step=0.6, seed=1):
    rng = np.random.default_rng(seed)
    return base + np.cumsum(np.clip(rng.normal(step, 0.3, n), -0.4, step * 3))


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def test_trend_slope_sign():
    up = _frame(np.linspace(100, 170, 200))
    down = _frame(np.linspace(170, 100, 200))
    flat = _frame(100 + np.zeros(200))
    assert sc.trend_slope_pct(up) > sc.SLOPE_RISING_PCT
    assert sc.trend_slope_pct(down) < sc.SLOPE_FALLING_PCT
    assert abs(sc.trend_slope_pct(flat)) < 1.0


def test_trend_slope_insufficient():
    assert sc.trend_slope_pct(_frame(np.linspace(1, 2, 10))) is None


def test_up_down_volume_ratio_accumulation_vs_distribution():
    closes = 100 + np.cumsum(np.where(np.arange(60) % 2 == 0, 1.0, -1.0))
    # Heavy volume on up days -> ratio > 1.
    vol_up = np.where(np.diff(np.concatenate([[closes[0]], closes])) > 0, 3_000_000.0, 1_000_000.0)
    assert sc.up_down_volume_ratio(_frame(closes, vol_up)) > 1.5
    # Heavy volume on down days -> ratio < 1.
    vol_dn = np.where(np.diff(np.concatenate([[closes[0]], closes])) > 0, 1_000_000.0, 3_000_000.0)
    assert sc.up_down_volume_ratio(_frame(closes, vol_dn)) < 0.7


def test_up_down_volume_ratio_no_down_days_returns_none():
    closes = np.linspace(100, 160, 60)          # strictly rising, no down days
    assert sc.up_down_volume_ratio(_frame(closes)) is None


def test_acc_dist_day_counts_detects_distribution():
    # Alternating moves; big volume only on the down days -> distribution days.
    closes = 100 + np.cumsum(np.where(np.arange(40) % 2 == 0, 0.8, -0.8))
    vols = np.where(np.diff(np.concatenate([[closes[0]], closes])) < 0, 4_000_000.0, 1_000_000.0)
    acc, dist = sc.acc_dist_day_counts(_frame(closes, vols))
    assert dist > acc


def test_base_count_counts_breakouts_and_resets():
    # Two flat bases each followed by a breakout leg to new highs.
    def base(level, k=30):
        return level + np.zeros(k)
    seg = np.concatenate([
        base(100), np.linspace(100, 130, 20),      # base 1 -> breakout
        base(130), np.linspace(130, 165, 20),      # base 2 -> breakout
    ])
    assert sc.base_count(_frame(seg)) >= 2
    # A deep undercut after a base resets the count to a small number.
    crash = np.concatenate([base(100), np.linspace(100, 130, 20), base(130),
                            np.linspace(130, 60, 30)])   # undercut far below the base low
    assert sc.base_count(_frame(crash)) == 0


def test_base_count_insufficient():
    assert sc.base_count(_frame(np.linspace(1, 2, 10))) is None


# ---------------------------------------------------------------------------
# BaseStage
# ---------------------------------------------------------------------------
def test_base_stage_insufficient_below_min_bars():
    df = _frame(_rising(n=sc.MIN_BARS_BASE - 20))
    assert sc.classify_symbol(df)[0] == BaseStage.INSUFFICIENT_DATA


def test_base_stage_early_advance():
    # A clean, not-yet-extended advance: rising trend, above both MAs, few bases.
    closes = np.concatenate([100 + np.zeros(120), np.linspace(100, 145, 160)])
    base, _ = sc.classify_symbol(_frame(closes))
    assert base == BaseStage.EARLY_ADVANCE


def test_base_stage_late_advance_when_extended():
    # Long base then a steep, extended run -> stretched far above MA50 in ATR units.
    closes = np.concatenate([100 + np.zeros(200), np.linspace(100, 260, 80)])
    base, _ = sc.classify_symbol(_frame(closes, high_pad=0.2, low_pad=0.2))
    assert base == BaseStage.LATE_ADVANCE


def test_base_stage_topping():
    # Long advance that rolls over at the tail while still above the 200-MA:
    # slope over the last 150 flattens, price slips below MA50, ATR expands.
    up = np.linspace(100, 200, 230)
    roll = 200 - np.linspace(0, 18, 50)
    closes = np.concatenate([up, roll])
    df = _frame(closes, high_pad=2.5, low_pad=3.0)
    base, _ = sc.classify_symbol(df)
    assert base == BaseStage.TOPPING


def test_base_stage_declining():
    closes = np.linspace(200, 110, 280)          # sustained downtrend below its MAs
    base, _ = sc.classify_symbol(_frame(closes))
    assert base == BaseStage.DECLINING


# ---------------------------------------------------------------------------
# InstFlow
# ---------------------------------------------------------------------------
def test_inst_flow_insufficient_below_min_bars():
    df = _frame(_rising(n=sc.MIN_BARS_FLOW - 5))
    assert sc.classify_symbol(df)[1] == InstFlow.INSUFFICIENT_DATA


def test_inst_flow_accumulating():
    closes = _rising(n=120)
    delta = np.diff(np.concatenate([[closes[0]], closes]))
    vols = np.where(delta > 0, 3_000_000.0, 900_000.0)   # heavy on up days
    _, flow = sc.classify_symbol(_frame(closes, vols))
    assert flow == InstFlow.ACCUMULATING


def test_inst_flow_distributing():
    # Price grinds up but the volume (and OBV) is heavy on down days.
    closes = 100 + np.cumsum(np.where(np.arange(120) % 3 == 0, -1.4, 0.9))
    delta = np.diff(np.concatenate([[closes[0]], closes]))
    vols = np.where(delta < 0, 5_000_000.0, 800_000.0)
    _, flow = sc.classify_symbol(_frame(closes, vols))
    assert flow == InstFlow.DISTRIBUTING


# ---------------------------------------------------------------------------
# Entrability grid (pure over the two enums)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("base,flow,expected", [
    (BaseStage.EARLY_ADVANCE, InstFlow.ACCUMULATING, Entrability.READY),
    (BaseStage.EARLY_ADVANCE, InstFlow.EARLY_INTEREST, Entrability.READY),
    (BaseStage.EARLY_ADVANCE, InstFlow.NO_INTEREST, Entrability.WATCH),
    (BaseStage.LATE_ADVANCE, InstFlow.ACCUMULATING, Entrability.CAUTION),
    (BaseStage.LATE_ADVANCE, InstFlow.EARLY_INTEREST, Entrability.WATCH),
    (BaseStage.BASING, InstFlow.EARLY_INTEREST, Entrability.WATCH),
    (BaseStage.BASING, InstFlow.ACCUMULATING, Entrability.WATCH),
    (BaseStage.TOPPING, InstFlow.ACCUMULATING, Entrability.BLOCKED),
    (BaseStage.DECLINING, InstFlow.ACCUMULATING, Entrability.BLOCKED),
    (BaseStage.EARLY_ADVANCE, InstFlow.DISTRIBUTING, Entrability.BLOCKED),
    (BaseStage.LATE_ADVANCE, InstFlow.DISTRIBUTING, Entrability.BLOCKED),
    (BaseStage.INSUFFICIENT_DATA, InstFlow.ACCUMULATING, Entrability.BLOCKED),
    (BaseStage.EARLY_ADVANCE, InstFlow.INSUFFICIENT_DATA, Entrability.BLOCKED),
])
def test_structure_entrability_grid(base, flow, expected):
    assert sc.structure_entrability(base, flow) == expected


# ---------------------------------------------------------------------------
# Purity / determinism / ETF-identical
# ---------------------------------------------------------------------------
def test_classify_does_not_mutate_frame():
    df = _frame(_rising(n=280))
    before = df.copy()
    sc.classify_symbol(df)
    pd.testing.assert_frame_equal(df, before)


def test_prefix_causal_determinism():
    # A run on a prefix equals a run on that same prefix later (no lookahead).
    full = _frame(_rising(n=300))
    k = 270
    prefix = full.iloc[:k]
    a = sc.classify_symbol(prefix)
    b = sc.classify_symbol(full.iloc[:k].copy())
    assert a == b


def test_no_is_etf_argument():
    # The classifier runs identically for ETFs: there is simply no is_etf path.
    import inspect
    params = inspect.signature(sc.classify_symbol).parameters
    assert "is_etf" not in params and "sector" not in params
