"""Deterministic builder for the structure-classifier regression fixtures.

SYNTHESIZED daily OHLCV series with VARIED volume (the regime fixtures use
constant volume, which cannot exercise InstFlow — see AUDIT_SCAN_RESTRUCTURE_
PHASE0.md Q7). Each is >= the classifier's 250-bar BaseStage floor so it returns
a real stage rather than INSUFFICIENT_DATA. Written next to this script and
asserted in ``test_structure_fixtures.py``.

  * topping_distribution.parquet — a long healthy advance that rolls over into a
    high-volatility, distribution-volume top while still above its 200-day MA.
    The classifier MUST read BaseStage=TOPPING even though the trend/momentum
    lights can still be >=3 green — this is the "trend lights alone are
    insufficient" case (the July-6 XLK analog, rebuilt >=250 bars; the original
    xlk_july6_rollover fixture is left untouched because the regime regression
    pins its lights to <=2 green, which contradicts "lights may be green" here).

  * early_advance_accum.parquet — a clean early advance on up-day-heavy volume:
    BaseStage=EARLY_ADVANCE, InstFlow=ACCUMULATING, so the structure cell is
    entrable (READY). Under a RED market regime the composed VERDICT must still
    be BLOCKED — the invisible-regime-input pin (asserted once the shared VERDICT
    lands; the fixture + structure read are ready now).

Regenerate with:  python -m fixtures.structure.build_fixtures   (from backend/)
The parquet outputs are committed so tests never rebuild.
"""
from __future__ import annotations

import math
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))


def _ohlcv(closes, highs, lows, volumes, start="2023-01-02") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.bdate_range(start=start, periods=n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"Open": opens, "High": np.asarray(highs, float), "Low": np.asarray(lows, float),
         "Close": closes, "Volume": np.asarray(volumes, float)},
        index=idx,
    )


def _updown_volume(closes, up_vol, down_vol, base_vol=1_000_000.0):
    """Volume heavier on up days (up_vol) or down days (down_vol) than baseline,
    keyed off the sign of the day-over-day close change."""
    delta = np.diff(np.concatenate([[closes[0]], closes]))
    vol = np.full(len(closes), base_vol)
    vol[delta > 0] = up_vol
    vol[delta < 0] = down_vol
    return vol


def topping_distribution() -> pd.DataFrame:
    """A 150-bar advance, then a 120-bar high-volatility top that stays ELEVATED
    (above its 50- and 200-day MAs) and ends on an up-move — so the trend/momentum
    lights can still read green — while the 150-day slope has flattened, ATR is
    expanding, and volume is under distribution. This is the crux Fixture-A case:
    the four-light engine says go, the STRUCTURE classifier says TOPPING."""
    rng = np.random.default_rng(7)
    advance = 100 + np.linspace(0, 100, 150) + rng.normal(0, 0.6, 150)     # 100 -> ~200
    # Rolling top: gently rising center (200 -> ~204) with wide oscillation, so
    # price holds above MA50 (and well above the 200-day mean) while the long-run
    # slope flattens.
    t = np.arange(120)
    center = 200 + np.linspace(0, 4, 120)                                 # gentle rise keeps slope flat
    osc = 4.0 * np.sin(t / 120 * 3 * 2 * np.pi)                            # 3 choppy cycles
    top = center + osc + rng.normal(0, 1.5, 120)
    # End on a clean up-swing so ROC(10) > 0 and the close prints above MA50/SAR.
    top[-6:] = top[-7] + np.linspace(1.0, 5.0, 6)
    closes = np.concatenate([advance, top]).astype(float)
    # Intraday ranges: calm in the advance, widening through the top, with a wide
    # spike in the final bars so ATR is expanding (atr/atr_5ema > 1) at the as-of bar.
    band = np.concatenate([np.full(150, 0.5), np.linspace(1.0, 4.5, 112), np.full(8, 9.0)])
    highs = closes + band
    lows = closes - band
    vols = _updown_volume(closes, up_vol=900_000.0, down_vol=3_400_000.0)
    return _ohlcv(closes, highs, lows, vols, start="2023-01-02")


def early_advance_accum() -> pd.DataFrame:
    """A 90-bar base, then a 180-bar orderly advance on up-day-heavy volume."""
    rng = np.random.default_rng(3)
    base = 100 + rng.normal(0, 0.6, 90)                                    # base ~100
    advance = 100 + np.linspace(0, 48, 180) + rng.normal(0, 0.8, 180)      # 100 -> ~148
    closes = np.concatenate([base, advance]).astype(float)
    highs = closes + 0.7
    lows = closes - 0.7
    vols = _updown_volume(closes, up_vol=3_200_000.0, down_vol=900_000.0)
    return _ohlcv(closes, highs, lows, vols, start="2023-01-02")


# ---------------------------------------------------------------------------
# Two-speed RS shadow fixtures (Fixture C + the Fixture A sector companion).
# The RS state needs a SECOND frame (the sector benchmark). We build each sector by
# dividing the stock closes by a DESIGNED RS line r = stock/sector, so the RS
# pairing lands on an exact target state regardless of the stock's own shape:
#   * level  = rs3m  = (r[-1]/r[-64] - 1) * 100   (63-bar relative strength)
#   * slope  = sign of the RS-line EMA over the last 21 bars
# ---------------------------------------------------------------------------
def _sector_from_rs_line(stock_closes: np.ndarray, r: np.ndarray, band: float = 0.8) -> pd.DataFrame:
    """A benchmark frame such that stock/sector == r (the designed RS line)."""
    sector = (np.asarray(stock_closes, float) / np.asarray(r, float))
    highs = sector + band
    lows = sector - band
    vols = np.full(len(sector), 1_000_000.0)
    return _ohlcv(sector, highs, lows, vols, start="2023-01-02")


def _rs_line(n: int, seg_start: float, seg_mid: float, seg_end: float) -> np.ndarray:
    """A three-segment RS line: flat 1.0 until 63 bars out, a linear leg to the
    -21-bar point (``seg_start`` -> ``seg_mid``), then a linear leg over the last 21
    bars (``seg_mid`` -> ``seg_end``). The 63-bar level is ``seg_end/seg_start - 1``;
    the last-21 direction is ``sign(seg_end - seg_mid)``."""
    r = np.full(n, seg_start)
    i0, i1 = n - 64, n - 21
    r[:i0] = seg_start
    r[i0:i1] = np.linspace(seg_start, seg_mid, i1 - i0)
    r[i1:] = np.linspace(seg_mid, seg_end, n - i1)
    return r


def turning_recovery() -> pd.DataFrame:
    """Fixture C stock (the NVDA shape): a high plateau, a deep dip, then a 120-bar
    recovery that holds above SMA50 while SMA50 is still BELOW SMA200 (golden cross
    not yet). The classifier reads EARLY_ADVANCE x EARLY_INTEREST (entrable), but the
    per-name Symbol Genius is YELLOW (3/4 — the SMA50>SMA200 light is red), so the
    composed VERDICT is non-READY with the SYM (Level-3) input binding. Paired with
    ``turning_recovery_sector`` its RS state is TURNING (lagging on 3M, recovering)."""
    rng = np.random.default_rng(11)
    plateau = 200 + rng.normal(0, 0.6, 60)                                 # tall, keeps SMA200 elevated
    dip = np.linspace(200, 95, 90) + rng.normal(0, 0.8, 90)                # 200 -> 95
    advance = np.linspace(95, 128, 120) + rng.normal(0, 0.8, 120)          # orderly recovery, below SMA200
    closes = np.concatenate([plateau, dip, advance]).astype(float)
    highs = closes + 1.0
    lows = closes - 1.0
    vols = _updown_volume(closes, up_vol=1_150_000.0, down_vol=1_000_000.0)  # mild EARLY_INTEREST
    return _ohlcv(closes, highs, lows, vols, start="2023-01-02")


def turning_recovery_sector() -> pd.DataFrame:
    """Fixture C sector: the benchmark that makes the stock's RS state TURNING —
    3-month level NEGATIVE (the sector out-ran the stock over 63 bars) while the
    last-21-bar RS-EMA slope is UP (the stock is now catching up). r declines
    1.00 -> 0.90 over [-63,-21] then rises 0.90 -> 0.95 over the last 21 bars, so
    level = 0.95/1.00 - 1 = -5% and the recent slope is positive."""
    r = _rs_line(len(turning_recovery()), seg_start=1.00, seg_mid=0.90, seg_end=0.95)
    return _sector_from_rs_line(turning_recovery()["Close"].to_numpy(), r)


def topping_distribution_sector() -> pd.DataFrame:
    """Fixture A sector companion: makes the topping stock's RS state FADING —
    3-month level NON-NEGATIVE (the stock led over 63 bars from its earlier surge)
    while the last-21-bar RS-EMA slope is DOWN (rolling over into the top —
    distribution-into-strength). r rises 1.00 -> 1.08 over [-63,-21] then falls
    1.08 -> 1.03 over the last 21 bars, so level = +3% and the recent slope is
    negative."""
    r = _rs_line(len(topping_distribution()), seg_start=1.00, seg_mid=1.08, seg_end=1.03)
    return _sector_from_rs_line(topping_distribution()["Close"].to_numpy(), r)


def early_advance_extended() -> pd.DataFrame:
    """Fixture D — the AAPL 7/16 shape. A clean early advance on up-day-heavy
    volume (BaseStage=EARLY_ADVANCE, InstFlow=ACCUMULATING, structure entrable),
    that ends on a sharp multi-bar POP with a widening intraday range: price is
    driven >1.5 ATR above its MA21 with ATR expanding, so the Level-4 right-spot
    gate FAILS (extension + atr_5d_ema) even though the structure is READY and the
    Symbol-Genius lights are green.

    The pop is sized to stay UNDER +15% of the (laggier) SMA50 so the classifier
    still reads EARLY_ADVANCE (not LATE_ADVANCE/TOPPING) — the whole point is a
    READY-structure name that the full gate must NOT call READY. This is the guard
    for the verdict-completeness fix: VERDICT != READY, binding constraint = L4.
    """
    rng = np.random.default_rng(19)
    base = 100 + rng.normal(0, 0.5, 90)                                    # base ~100
    advance = 100 + np.linspace(0, 40, 176) + rng.normal(0, 0.6, 176)      # 100 -> ~140, orderly
    pop = advance[-1] + np.linspace(2.0, 11.0, 4)                          # sharp 4-bar pop to ~151
    closes = np.concatenate([base, advance, pop]).astype(float)
    # Calm ranges through the advance, then a wide spike over the pop so ATR
    # expands (atr/atr_5ema > 1) and the extension in ATR units blows past 1.5.
    band = np.concatenate([np.full(90, 0.5), np.full(176, 0.6),
                           np.linspace(2.5, 5.0, 4)])
    highs = closes + band
    lows = closes - band
    vols = _updown_volume(closes, up_vol=3_200_000.0, down_vol=900_000.0)
    return _ohlcv(closes, highs, lows, vols, start="2023-01-02")


def early_advance_low_juice() -> pd.DataFrame:
    """Fixture E — the PNC shape. A pristine early advance (EARLY_ADVANCE × ACCUM,
    SYM green) on a VERY LOW-volatility name: tight daily ranges and a gentle drift,
    so realized vol is low, the BSM-implied weekly extrinsic is thin, and NET juice/
    wk lands BELOW the viability floor. The structure/RS/SYM are all green — the only
    thing wrong is the economics, which is exactly the case the juice safety block
    must catch (VERDICT BLOCKED, binding = L5 juice, NOT on bench)."""
    rng = np.random.default_rng(23)
    base = 100 + rng.normal(0, 0.10, 90)                                   # calm base
    advance = 100 + np.linspace(0, 30, 180) + rng.normal(0, 0.12, 180)     # orderly, low-vol
    closes = np.concatenate([base, advance]).astype(float)
    highs = closes + 0.15                                                   # tight ranges -> low HV
    lows = closes - 0.15
    vols = _updown_volume(closes, up_vol=3_200_000.0, down_vol=900_000.0)   # still ACCUM
    return _ohlcv(closes, highs, lows, vols, start="2023-01-02")


def recovery_v_shape() -> pd.DataFrame:
    """A V-shaped recovery still under its 200-day — the shape that read BASING
    before 2026-08-21 (BaseStage.RECOVERING; AUDIT_BASING_RECOVERY_PHASE0.md).

    SYNTHETIC. This is a hand-built reproduction of the GDDY 2026-08-21
    observation, NOT captured market data — no provider is configured in the test
    environment and none is ever called, so real bars for that date cannot be
    obtained here. It is named for the SHAPE rather than the ticker so nobody
    mistakes it for a real quote series. The GDDY measurements it reproduces:

        close 97.08, ~0.7% BELOW SMA200 (97.77), ~29% above the June low,
        above a rising SMA50, downtrend line broken, an early-August
        earnings gap carrying a range + volume expansion.

    The geometry is what makes the case: the plateau sits OUTSIDE the 150-bar
    slope window but INSIDE the 200-bar SMA, so the long fit reads flat (the
    decline and the rally net out) while the SMA200 still sits above price. That
    is exactly the configuration one 150-bar fit cannot resolve."""
    plateau, top, low, end = 132.0, 118.0, 75.0, 97.08
    pre = [plateau * (1 + 0.012 * math.cos(2 * math.pi * i / 23)) for i in range(95)]
    ramp = np.linspace(plateau, top, 15).tolist()
    down = [(top + (low - top) * (i / 59)) * (1 + 0.012 * math.cos(2 * math.pi * i / 13))
            for i in range(60)]
    up = [(low + (end - low) * (i / 99)) * (1 + 0.010 * math.cos(2 * math.pi * i / 11))
          for i in range(100)]
    closes = np.array(pre + ramp + down + up, dtype=float)
    closes[-1] = end
    highs = closes * 1.008
    lows = closes * 0.992
    vols = _updown_volume(closes, up_vol=1_600_000.0, down_vol=1_100_000.0)
    # The early-August earnings gap: one wide-range bar on ~4x volume.
    highs[-12] = closes[-12] * 1.06
    lows[-12] = closes[-12] * 0.985
    vols[-13:-9] *= 4.0
    return _ohlcv(closes, highs, lows, vols, start="2025-08-01")


def flat_base_below_200() -> pd.DataFrame:
    """A genuine base under the 200-day: flat over BOTH horizons. Proves
    RECOVERING did not swallow legitimate bases — same below-200 position as
    recovery_v_shape, but with no rally in the short window."""
    # The decline sits OUTSIDE the 150-bar slope window (the flat stretch is
    # longer than the window) but INSIDE the 200-bar SMA — the same geometry as
    # recovery_v_shape, so the two differ ONLY in what the short window sees.
    plateau = 132.0
    pre = [plateau * (1 + 0.012 * math.cos(2 * math.pi * i / 23)) for i in range(58)]
    decline = np.linspace(plateau, 97.0, 60).tolist()
    flat = [97.0 * (1 + 0.011 * math.cos(2 * math.pi * i / 17)) for i in range(152)]
    closes = np.array(pre + decline + flat, dtype=float)
    closes[-1] = 97.0
    highs = closes * 1.007
    lows = closes * 0.993
    vols = _updown_volume(closes, up_vol=1_300_000.0, down_vol=1_150_000.0)
    return _ohlcv(closes, highs, lows, vols, start="2025-08-01")


FIXTURES = {
    "recovery_v_shape": recovery_v_shape,
    "flat_base_below_200": flat_base_below_200,
    "topping_distribution": topping_distribution,
    "topping_distribution_sector": topping_distribution_sector,
    "early_advance_accum": early_advance_accum,
    "early_advance_extended": early_advance_extended,
    "early_advance_low_juice": early_advance_low_juice,
    "turning_recovery": turning_recovery,
    "turning_recovery_sector": turning_recovery_sector,
}


def build() -> None:
    for name, fn in FIXTURES.items():
        df = fn()
        path = os.path.join(HERE, f"{name}.parquet")
        df.to_parquet(path)
        print(f"wrote {path}  ({len(df)} bars, {df.index[0].date()} -> {df.index[-1].date()})")


if __name__ == "__main__":
    build()
