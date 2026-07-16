"""The shared Genius four-light engine — ONE indicator system, fractal across the
market regime and per-name stock lights.

The Cash Flow Machine "Genius System" reads four binary indicator "lights" off a
daily-bar series and votes them:

    1. close vs slow MA        — close above the slow average  = GREEN light
    2. fast MA vs slow MA      — fast above slow (trend up)     = GREEN light
    3. Parabolic SAR vs close  — SAR dots under price           = GREEN light
    4. momentum vs zero        — ROC above zero                 = GREEN light

This module is the single home of that light math and the raw vote. It is PURE
and deterministic: it takes an ascending OHLCV frame plus provenance-tagged
params, does NO I/O and reads NO clock, and every indicator is causal on the
series (bar i depends only on bars <= i). ``regime_genius`` layers the market's
yellow dwell + secondary indicators on top; ``stock_lights`` layers the
per-name verdict mapping + right-spot gate + vetoes on top. Neither duplicates a
single line of light logic — both call ``compute`` here.

SAR seeding is canonical-start: ``indicators.parabolic_sar`` seeds from the first
two bars of the frame it is given, so a fixed first bar makes the whole light
series reproducible (the regime backfill and the stock backfill both always slice
from each name's EARLIEST cached bar, never a rolling sub-window — see
``config.STOCK_LIGHTS_WARMUP_BARS``).
"""
from __future__ import annotations

import config
import indicators

GREEN = "green"
YELLOW = "yellow"
RED = "red"

# The four light keys, in vote order. ``close_vs_ma`` is the close-vs-slow-MA
# light (light 1); the key name is retained for backward-compat with the market
# regime trace, the entry gate, the snapshot, and the frontend.
LIGHT_KEYS = ("close_vs_ma", "fast_vs_slow", "sar", "momentum")


# ---------------------------------------------------------------------------
# Parameters (config defaults, per-call overridable for calibration)
# ---------------------------------------------------------------------------
def default_params() -> dict:
    """The provenance-tagged light + vote parameter set from config. A calibration
    sweep passes a modified copy into ``compute`` — nothing past this point reads
    config directly, so an alternative parameter set fully determines the output.
    These are the ONLY parameters the lights need; the market dwell / breadth /
    VIX params live in ``regime_genius.default_params`` (a superset)."""
    return {
        "slow_ma": config.GENIUS_SLOW_MA,
        "fast_ma": config.GENIUS_FAST_MA,
        "sar_af_step": config.GENIUS_SAR_AF_STEP,
        "sar_af_max": config.GENIUS_SAR_AF_MAX,
        "roc_window": config.GENIUS_MOMENTUM_ROC,
        "vote_green_min": config.GENIUS_VOTE_GREEN_MIN,
    }


def _params(overrides: dict | None) -> dict:
    p = default_params()
    if overrides:
        p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# The four lights — each returns its signal (GREEN/RED, or None on missing data)
# plus the underlying values, for UI + snapshot provenance.
# ---------------------------------------------------------------------------
def _signal(is_green: bool | None) -> str | None:
    if is_green is None:
        return None
    return GREEN if is_green else RED


def light_close_vs_ma(close: float | None, slow_ma: float | None) -> dict:
    """Light 1 — close above the slow MA is a GREEN light."""
    green = None if close is None or slow_ma is None else close > slow_ma
    return {"signal": _signal(green), "close": close, "slow_ma": slow_ma}


def light_fast_vs_slow(fast_ma: float | None, slow_ma: float | None) -> dict:
    """Light 2 — fast MA above slow MA (primary trend up) is a GREEN light."""
    green = None if fast_ma is None or slow_ma is None else fast_ma > slow_ma
    return {"signal": _signal(green), "fast_ma": fast_ma, "slow_ma": slow_ma}


def light_sar(close: float | None, sar: float | None) -> dict:
    """Light 3 — Parabolic SAR under price (dots below the bar) is a GREEN light."""
    green = None if close is None or sar is None else sar < close
    return {"signal": _signal(green), "sar": sar, "close": close}


def light_momentum(roc: float | None) -> dict:
    """Light 4 — momentum (ROC) above zero is a GREEN light."""
    green = None if roc is None else roc > 0
    return {"signal": _signal(green), "roc": roc}


def light_sma_slow_vs_slower(slow_ma: float | None, slower_ma: float | None) -> dict:
    """A STRUCTURAL light — the slow SMA above the slower SMA (e.g. SMA50 > SMA200,
    a golden-cross / long-clock trend posture) is a GREEN light.

    This is NOT used by the market regime or the stock lights (whose fourth light
    is the short-clock ``light_fast_vs_slow`` = EMA21 > SMA50). It is the Symbol
    Genius fourth light — the deliberate divergence: the per-symbol instance judges
    structural health on a longer clock. Kept here beside the other light functions
    so every light in the system is defined in one place, but ``compute_lights``
    (the regime/stock light-set) does NOT call it — Symbol Genius assembles its own
    light-set in ``symbol_genius`` from this plus the three shared lights."""
    green = None if slow_ma is None or slower_ma is None else slow_ma > slower_ma
    return {"signal": _signal(green), "slow_ma": slow_ma, "slower_ma": slower_ma}


def compute_lights(df, params: dict | None = None) -> dict:
    """The four lights for the latest bar of `df` (an ascending OHLCV frame).
    Every indicator is computed here from `df` alone — no clock, no I/O."""
    p = _params(params)
    close = indicators.last(df)
    slow = indicators.sma(df, p["slow_ma"]) if df is not None else None
    fast = indicators.ema(df, p["fast_ma"])
    sar = indicators.parabolic_sar_last(df, p["sar_af_step"], p["sar_af_max"])
    roc = indicators.roc(df, p["roc_window"])
    return {
        "close_vs_ma": light_close_vs_ma(close, slow),
        "fast_vs_slow": light_fast_vs_slow(fast, slow),
        "sar": light_sar(close, sar),
        "momentum": light_momentum(roc),
    }


# ---------------------------------------------------------------------------
# Vote (HARD_CFM_RULE) — the market-style raw condition
# ---------------------------------------------------------------------------
def vote(lights: dict, params: dict | None = None) -> dict:
    """Vote the four lights to a raw condition. >=vote_green_min GREEN -> GREEN;
    a 2/2 split -> YELLOW; >=vote_green_min RED -> RED. If any light is missing
    (insufficient history) the vote can't be trusted, so it degrades to YELLOW
    and flags ``insufficient`` — real SPY always has all four.

    NOTE: this is the MARKET vote (used by the regime). Per-name stock lights use
    their OWN verdict mapping (4/4 = GREEN, exactly 3 = YELLOW, <=2 or any veto =
    RED) over the same ``green_count`` — see ``stock_lights.verdict``."""
    p = _params(params)
    signals = [lights[k]["signal"] for k in LIGHT_KEYS]
    green = sum(1 for s in signals if s == GREEN)
    red = sum(1 for s in signals if s == RED)
    insufficient = any(s is None for s in signals)
    green_min = p["vote_green_min"]
    # Canon: >=green_min GREEN -> GREEN; >=green_min RED -> RED; else YELLOW.
    if insufficient:
        condition = YELLOW
    elif green >= green_min:
        condition = GREEN
    elif red >= green_min:
        condition = RED
    else:
        condition = YELLOW
    return {"raw_condition": condition, "green_count": green, "red_count": red,
            "insufficient": insufficient}


# ---------------------------------------------------------------------------
# The single shared entry point
# ---------------------------------------------------------------------------
def compute(series, clock=None, params: dict | None = None) -> dict:
    """The four lights + raw vote for the latest bar of `series` (an ascending
    OHLCV frame). PURE — the single light computation shared by the market regime
    and per-name stock lights.

    ``clock`` is accepted for call-site symmetry / provenance only; the lights are
    causal on the series (the "as-of" instant is the last bar), so it does NOT
    enter the math. Returns the four ``lights``, the ``greens`` count, the ``reds``
    count, an ``insufficient`` flag (any light lacked history), and ``color`` — the
    MARKET-style vote color (>=vote_green_min green). Consumers that need a
    different mapping (stock verdict) read ``greens`` and apply their own rule; the
    market regime layers dwell on ``color`` (see ``regime_genius``)."""
    p = _params(params)
    lights = compute_lights(series, p)
    v = vote(lights, p)
    return {
        "lights": lights,
        "greens": v["green_count"],
        "reds": v["red_count"],
        "insufficient": v["insufficient"],
        "color": v["raw_condition"],
    }
