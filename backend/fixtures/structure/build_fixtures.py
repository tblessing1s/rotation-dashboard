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


FIXTURES = {
    "topping_distribution": topping_distribution,
    "early_advance_accum": early_advance_accum,
}


def build() -> None:
    for name, fn in FIXTURES.items():
        df = fn()
        path = os.path.join(HERE, f"{name}.parquet")
        df.to_parquet(path)
        print(f"wrote {path}  ({len(df)} bars, {df.index[0].date()} -> {df.index[-1].date()})")


if __name__ == "__main__":
    build()
