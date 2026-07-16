"""Per-name Symbol Genius — a four-light instance of the Genius engine tuned for a
single symbol's STRUCTURAL health, deliberately diverging from the market regime
on its fourth light.

Three lights are shared byte-for-byte with the market regime / stock engine (they
ARE the same ``genius_lights`` functions):

    1. close vs SMA50         — close above the slow SMA        = GREEN
    3. Parabolic SAR vs close  — SAR dots under price            = GREEN
    4. momentum (ROC10) vs 0   — ROC above zero                  = GREEN

The remaining light is DIFFERENT ON PURPOSE:

    regime :  EMA21 > SMA50    (fast vs slow — a short-clock trend read)
    symbol :  SMA50 > SMA200   (a long-clock structural / golden-cross read)

The two engines must never silently share that fourth-light constant, so Symbol
Genius carries its OWN parameter set — ``slow_ma`` (50) + ``slower_ma`` (200), and
**no ``fast_ma`` at all** — and does NOT read ``genius_lights.default_params``. It
assembles its own light-set here rather than editing ``genius_lights.compute_lights``
(which the market regime and stock lights use and whose byte-for-byte output is
pinned by ``test_regime_regression``).

VERDICT is the STOCK mapping — 4/4 green = GREEN, exactly 3 = YELLOW (watchlist,
never enterable), <=2 or insufficient = RED — reused from ``stock_lights.verdict``
so the two can never diverge. There are NO vetoes and NO right-spot gate here
(those belong to ``stock_lights`` / the entry gate); Symbol Genius is purely the
four lights + the verdict mapping. There is NO yellow dwell in v1 (an asymmetric
green->yellow dwell is a PROPOSED future; shadow-log flip frequency first).

Warm-up: the SMA50>SMA200 light needs >=200 bars, so a name needs at least
``config.SYMBOL_LIGHTS_WARMUP_BARS`` bars before its verdict is trusted; inside the
warm-up the verdict is RED (insufficient), never GREEN — which keeps fixtures /
backfill reproducible, exactly as the stock lights do.

Pure and deterministic: takes an ascending OHLCV frame, does NO I/O, reads NO
clock, never mutates the frame. Runs IDENTICALLY for stocks and ETFs.
"""
from __future__ import annotations

import config
import genius_lights
import indicators
import stock_lights

GREEN = genius_lights.GREEN
YELLOW = genius_lights.YELLOW
RED = genius_lights.RED

# Symbol Genius' own four light keys, in vote order. Note ``structure`` in slot 2
# where the regime/stock engine has ``fast_vs_slow`` — the deliberate divergence.
LIGHT_KEYS = ("close_vs_ma", "structure", "sar", "momentum")


# ---------------------------------------------------------------------------
# Parameters — a DISTINCT set (no fast_ma; a slower_ma the regime never uses).
# ---------------------------------------------------------------------------
def default_params() -> dict:
    """Symbol Genius' provenance-tagged params. Deliberately NOT a superset of
    ``genius_lights.default_params`` — it has no ``fast_ma`` key at all, so the
    SMA50>SMA200 fourth light can never accidentally fall back to the regime's
    EMA21 constant. The three shared lights reuse the same GENIUS_* windows."""
    return {
        "slow_ma": config.GENIUS_SLOW_MA,             # 50  — close>SMA50 + the slow leg of light 4
        "slower_ma": config.SYMBOL_GENIUS_SLOWER_MA,  # 200 — the slower leg of light 4 (divergence)
        "sar_af_step": config.GENIUS_SAR_AF_STEP,
        "sar_af_max": config.GENIUS_SAR_AF_MAX,
        "roc_window": config.GENIUS_MOMENTUM_ROC,
        "warmup_bars": config.SYMBOL_LIGHTS_WARMUP_BARS,
    }


def _params(overrides: dict | None) -> dict:
    p = default_params()
    if overrides:
        p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# The four lights — three shared, one divergent — assembled here (NOT via
# genius_lights.compute_lights, which is the regime/stock light-set).
# ---------------------------------------------------------------------------
def compute_lights(df, params: dict | None = None) -> dict:
    """The four Symbol Genius lights for the latest bar of ``df``. Every light is a
    shared ``genius_lights`` function; only the fourth (``structure``) differs from
    the regime engine (SMA50>SMA200 instead of EMA21>SMA50)."""
    p = _params(params)
    close = indicators.last(df)
    sma_slow = indicators.sma(df, p["slow_ma"]) if df is not None else None
    sma_slower = indicators.sma(df, p["slower_ma"]) if df is not None else None
    sar = indicators.parabolic_sar_last(df, p["sar_af_step"], p["sar_af_max"])
    roc = indicators.roc(df, p["roc_window"])
    return {
        "close_vs_ma": genius_lights.light_close_vs_ma(close, sma_slow),
        "structure": genius_lights.light_sma_slow_vs_slower(sma_slow, sma_slower),
        "sar": genius_lights.light_sar(close, sar),
        "momentum": genius_lights.light_momentum(roc),
    }


def _green_count(lights: dict) -> int:
    return sum(1 for k in LIGHT_KEYS if lights[k]["signal"] == GREEN)


def _insufficient(lights: dict) -> bool:
    return any(lights[k]["signal"] is None for k in LIGHT_KEYS)


# ---------------------------------------------------------------------------
# The single entry point — lights + green count + stock verdict.
# ---------------------------------------------------------------------------
def compute(df, params: dict | None = None) -> dict:
    """Symbol Genius for the latest bar of ``df``: the four lights, the green count,
    and the STOCK verdict (4/4 = GREEN, exactly 3 = YELLOW, <=2 or insufficient =
    RED — reused from ``stock_lights.verdict``). PURE. A name inside the SMA200
    warm-up is insufficient (verdict RED), never GREEN."""
    p = _params(params)
    lights = compute_lights(df, p)
    bars = 0 if df is None else len(df)
    insufficient = _insufficient(lights) or bars < p["warmup_bars"]
    greens = _green_count(lights)
    # Symbol Genius has no vetoes of its own (those live in stock_lights / the gate).
    verdict = stock_lights.verdict(greens, insufficient, any_veto=False)
    return {
        "lights": lights,
        "greens": greens,
        "insufficient": insufficient,
        "verdict": verdict,
        "color": verdict,        # the per-row SYM color
    }
