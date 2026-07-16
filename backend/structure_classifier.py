"""Structure classifier — a PURE per-symbol read of *where a name sits in its
base→advance→decline cycle* (``BaseStage``) and *whether volume shows
institutional accumulation or distribution* (``InstFlow``).

This is the (C) classifier of the scan restructure. One call per symbol returns
the two enums; the scan splits them into the BASE and INST columns (display-only
split — never two calls). It is deliberately independent of the Genius lights:
the lights judge trend/momentum, this judges *structure*, and the two disagree
on exactly the cases that matter (a name whose trend lights are still green while
its base is topping — the July-6 XLK case).

Contract & invariants:
  * PURE and deterministic — takes an ascending OHLCV frame, does NO I/O, reads
    NO clock. Every metric is causal on the series (bar i depends only on bars
    <= i), so ``classify_symbol(df.iloc[:k])`` on a prefix equals what a
    full-history run reported for that same as-of bar. Never mutates the frame.
  * Runs **IDENTICALLY for stocks and ETFs** — it reads price/volume structure
    only. There is no ``is_etf`` argument and no RS-vs-sector path, so it cannot
    collide with the ETF alternate gate path.
  * Returns an explicit ``INSUFFICIENT_DATA`` outcome per enum, **never a guessed
    stage** — ``BaseStage`` needs the deep history (SMA200 + 150-day slope + base
    counting); ``InstFlow`` needs far less (the 50-day up/down-volume window), so
    they degrade independently.

Base counting is done by **full replay over the bar history** inside this pure
function (audit Q4 option a) — no state.json, no ``recompute_derived`` entry;
mirrors the Parabolic-SAR prefix-causal precedent in ``indicators``.

**Every threshold in this module is ``PROPOSED_DEFAULT``**, not a HARD_CFM_RULE.
They live here (next to the logic) for now, clearly banner-tagged; a later step
can promote the ones calibration needs into ``config`` the way the Genius params
were. Reuses the already-pure primitives in ``indicators`` (SMA, ATR posture);
the volume/slope/base primitives are implemented here as new pure functions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators

# ---------------------------------------------------------------------------
# Enums (string-valued, matching the GREEN/YELLOW/RED convention in the engine).
# INSUFFICIENT_DATA is a member of EACH enum: the two are computed independently
# and each returns it when its own inputs are too short.
# ---------------------------------------------------------------------------
class BaseStage:
    BASING = "BASING"
    EARLY_ADVANCE = "EARLY_ADVANCE"
    LATE_ADVANCE = "LATE_ADVANCE"
    TOPPING = "TOPPING"
    DECLINING = "DECLINING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class InstFlow:
    ACCUMULATING = "ACCUMULATING"
    EARLY_INTEREST = "EARLY_INTEREST"
    NO_INTEREST = "NO_INTEREST"
    DISTRIBUTING = "DISTRIBUTING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Entrability:
    """The structure-cell entrability grid's output (an input to the eventual
    worst-signal-wins VERDICT — composed elsewhere, never here)."""
    READY = "READY"
    CAUTION = "CAUTION"
    WATCH = "WATCH"        # watchlist: valid setup, not yet entrable
    BLOCKED = "BLOCKED"


# ---------------------------------------------------------------------------
# PROPOSED_DEFAULT thresholds — none of these is a HARD_CFM_RULE.
# ---------------------------------------------------------------------------
# Data sufficiency (bars). BaseStage needs the SMA200 series + a 150-day slope +
# room to count bases; the depth precondition (config.HISTORY_DAYS ~= 400 cal ~=
# ~276 trading bars) is sized to clear this floor.
MIN_BARS_BASE = 250            # PROPOSED_DEFAULT — below this, BaseStage = INSUFFICIENT_DATA
MIN_BARS_FLOW = 50             # PROPOSED_DEFAULT — below this, InstFlow = INSUFFICIENT_DATA

# Long-term trend (150-day least-squares slope, expressed as the total % change
# of the fit line across the window).
SLOPE_WINDOW = 150             # PROPOSED_DEFAULT — the "150-day slope" window
SLOPE_RISING_PCT = 8.0         # PROPOSED_DEFAULT — fit rises > this % across the window = rising
SLOPE_FALLING_PCT = -8.0       # PROPOSED_DEFAULT — fit falls below this = falling

# Maturity / posture for the advance sub-classification.
EXT_LATE_PCT = 15.0            # PROPOSED_DEFAULT — close > this % above SMA50 = extended (late)
ATR_EXPANDING_MAX = 1.10       # PROPOSED_DEFAULT — ATR/ATR_5EMA above this = volatility expanding
LATE_ADVANCE_MIN_BASES = 3     # PROPOSED_DEFAULT — this many prior bases = a mature (late) advance

# Base counting.
BASE_MIN_LEN = 25              # PROPOSED_DEFAULT — a consolidation must last >= this many bars
BASE_MAX_DEPTH = 0.30          # PROPOSED_DEFAULT — and be shallower than this (30%) to count

# InstFlow.
UDVR_WINDOW = 50               # PROPOSED_DEFAULT — up/down volume ratio window (trading days)
UDVR_ACCUM = 1.25              # PROPOSED_DEFAULT — up-day volume >= this x down-day volume = accumulation
UDVR_INTEREST = 1.00           # PROPOSED_DEFAULT — merely > this = some interest
UDVR_DISTRIB = 0.80            # PROPOSED_DEFAULT — below this = distribution
DIVERGENCE_WINDOW = 20         # PROPOSED_DEFAULT — price-vs-OBV divergence lookback
ACC_DIST_WINDOW = 25           # PROPOSED_DEFAULT — accumulation/distribution day-count window
ACC_DIST_MOVE_PCT = 0.20       # PROPOSED_DEFAULT — |close move| >= this % qualifies a day
DIST_DAYS_MAX = 5              # PROPOSED_DEFAULT — this many distribution days in the window = under distribution


# ---------------------------------------------------------------------------
# Pure metric primitives (new — over bars only).
# ---------------------------------------------------------------------------
def trend_slope_pct(df: pd.DataFrame | None, window: int = SLOPE_WINDOW) -> float | None:
    """Least-squares slope of close over the last ``window`` bars, returned as the
    TOTAL percent change of the fit line across the window (end − start, as a
    percent of the window's mean price). Positive = rising trend, ~0 = flat,
    negative = falling. None with insufficient history or a zero mean."""
    if df is None or len(df) < window:
        return None
    y = df["Close"].astype(float).to_numpy()[-window:]
    x = np.arange(window, dtype=float)
    slope = float(np.polyfit(x, y, 1)[0])          # price per bar
    mean = float(y.mean())
    if mean == 0:
        return None
    return slope * (window - 1) / mean * 100.0     # total % change of the fit line


def _obv_series(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume (same construction as the scorecard's, kept local so the
    classifier core has no dependency on the metrics/display package)."""
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * vol).cumsum()


def up_down_volume_ratio(df: pd.DataFrame | None, window: int = UDVR_WINDOW) -> float | None:
    """Sum of volume on up-close days / sum on down-close days over the last
    ``window`` bars. > 1 = more volume changing hands on up days (accumulation);
    < 1 = heavier on down days (distribution). None with insufficient history or
    no down-day volume (avoids a divide-by-zero infinity)."""
    if df is None or len(df) < window + 1:
        return None
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    delta = close.diff()
    # Window the LAST ``window`` bars, then split that slice into up-day and
    # down-day volume (both legs drawn from the same trailing region).
    tail_vol = vol.tail(window)
    tail_delta = delta.tail(window)
    up_vol = float(tail_vol[tail_delta > 0].sum())
    down_vol = float(tail_vol[tail_delta < 0].sum())
    if down_vol <= 0:
        return None
    return up_vol / down_vol


def obv_price_divergence(df: pd.DataFrame | None, window: int = DIVERGENCE_WINDOW) -> int | None:
    """Sign of a price-vs-OBV divergence over the last ``window`` bars:
      -1 bearish  — price rose but OBV fell (buyers not confirming the highs),
      +1 bullish  — price fell but OBV rose (accumulation into weakness),
       0 aligned  — they agree (or neither moved).
    None with insufficient history."""
    if df is None or len(df) < window + 1:
        return None
    close = df["Close"].astype(float)
    obv = _obv_series(df)
    price_chg = float(close.iloc[-1] - close.iloc[-1 - window])
    obv_chg = float(obv.iloc[-1] - obv.iloc[-1 - window])
    if price_chg > 0 and obv_chg < 0:
        return -1
    if price_chg < 0 and obv_chg > 0:
        return 1
    return 0


def acc_dist_day_counts(df: pd.DataFrame | None, window: int = ACC_DIST_WINDOW,
                        move_pct: float = ACC_DIST_MOVE_PCT) -> tuple[int, int] | None:
    """(accumulation_days, distribution_days) over the last ``window`` bars.

    A distribution day = close down at least ``move_pct`` percent on volume
    GREATER than the prior bar's (institutions selling into the tape); an
    accumulation day = close up at least ``move_pct`` percent on higher volume.
    None with insufficient history."""
    if df is None or len(df) < window + 1:
        return None
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    ret = close.pct_change() * 100.0
    higher_vol = vol > vol.shift(1)
    acc = int(((ret >= move_pct) & higher_vol).tail(window).sum())
    dist = int(((ret <= -move_pct) & higher_vol).tail(window).sum())
    return acc, dist


def base_count(df: pd.DataFrame | None, min_len: int = BASE_MIN_LEN,
               max_depth: float = BASE_MAX_DEPTH) -> int | None:
    """A PROPOSED_DEFAULT heuristic count of completed bases in the frame.

    Full replay over the bars: a base is a ``min_len``-bar consolidation whose
    range is shallower than ``max_depth`` (high-to-low); it *completes* (count
    += 1) when a bar closes above that consolidation's high (a breakout to new
    highs). The low of the just-broken-out base is remembered, and a later close
    back below THAT prior base low resets the count (a failed base / decline
    phase). A cooldown prevents re-counting the same breakout. This is a maturity
    proxy for EARLY vs LATE advance, not a chart-perfect base detector. None when
    the frame is shorter than one base."""
    if df is None or len(df) <= min_len:
        return None
    highs = df["High"].astype(float).to_numpy()
    lows = df["Low"].astype(float).to_numpy()
    closes = df["Close"].astype(float).to_numpy()
    n = len(closes)
    count = 0
    cooldown = 0
    prior_base_low: float | None = None
    for i in range(min_len, n):
        c = float(closes[i])
        # Undercut of the prior base's low -> reset (failed base / decline phase).
        if prior_base_low is not None and c < prior_base_low:
            count = 0
            prior_base_low = None
        window_high = float(highs[i - min_len:i].max())
        window_low = float(lows[i - min_len:i].min())
        depth = (window_high - window_low) / window_high if window_high else 1.0
        if cooldown == 0 and depth <= max_depth and c > window_high:
            count += 1
            prior_base_low = window_low   # the base we just broke out of
            cooldown = min_len            # don't re-count until a fresh base forms
        elif cooldown:
            cooldown -= 1
    return count


# ---------------------------------------------------------------------------
# The two classifications (pure decision trees over the primitives).
# ---------------------------------------------------------------------------
def _signals(df: pd.DataFrame | None) -> dict:
    """Every underlying signal, computed once, for both the decision trees and the
    per-row detail drawer / provenance. Values are None when unavailable."""
    price = indicators.last(df)
    sma50 = indicators.sma(df, 50) if df is not None else None
    sma200 = indicators.sma(df, 200) if df is not None else None
    atr = indicators.atr(df) if df is not None else None
    ext_atr = (price - sma50) / atr if (price is not None and sma50 is not None
                                        and atr not in (None, 0)) else None
    pct_above_sma50 = ((price / sma50 - 1) * 100 if (price is not None and sma50)
                       else None)
    acc_dist = acc_dist_day_counts(df)
    return {
        "bars": 0 if df is None else len(df),
        "price": price,
        "sma50": sma50,
        "sma200": sma200,
        "above_sma50": None if (price is None or sma50 is None) else price > sma50,
        "above_sma200": None if (price is None or sma200 is None) else price > sma200,
        "slope_pct": trend_slope_pct(df),
        "ext_atr": ext_atr,
        "pct_above_sma50": pct_above_sma50,
        "atr_posture": indicators.atr_momentum(df) if df is not None else None,
        "base_count": base_count(df),
        "udvr": up_down_volume_ratio(df),
        "obv_above_ema": _obv_above_ema(df),
        "divergence": obv_price_divergence(df),
        "acc_days": None if acc_dist is None else acc_dist[0],
        "dist_days": None if acc_dist is None else acc_dist[1],
    }


def _obv_above_ema(df: pd.DataFrame | None) -> bool | None:
    if df is None or df.empty:
        return None
    obv = _obv_series(df)
    if obv.empty:
        return None
    ema = obv.ewm(span=20, adjust=False).mean()
    return bool(obv.iloc[-1] > ema.iloc[-1])


def _base_stage(sig: dict) -> str:
    if sig["bars"] < MIN_BARS_BASE:
        return BaseStage.INSUFFICIENT_DATA
    slope = sig["slope_pct"]
    above200 = sig["above_sma200"]
    above50 = sig["above_sma50"]
    if slope is None or above200 is None:
        return BaseStage.INSUFFICIENT_DATA

    rising = slope > SLOPE_RISING_PCT
    falling = slope < SLOPE_FALLING_PCT
    extended = sig["pct_above_sma50"] is not None and sig["pct_above_sma50"] > EXT_LATE_PCT
    expanding = sig["atr_posture"] is not None and sig["atr_posture"] > ATR_EXPANDING_MAX
    mature = (sig["base_count"] or 0) >= LATE_ADVANCE_MIN_BASES

    # A confirmed downtrend below the long-term average dominates.
    if falling and not above200:
        return BaseStage.DECLINING
    # An advance in force: rising trend AND holding above the fast MA.
    if rising and above50:
        if above200 and (extended or mature):
            return BaseStage.LATE_ADVANCE
        return BaseStage.EARLY_ADVANCE
    # Perched above the long-term average but momentum has stalled.
    if above200:
        if falling or expanding or not above50:
            return BaseStage.TOPPING
        return BaseStage.BASING          # quiet consolidation above the LT trend (re-base)
    # Below the long-term average: falling = declining, flat = building a base.
    if falling:
        return BaseStage.DECLINING
    return BaseStage.BASING


def _inst_flow(sig: dict) -> str:
    if sig["bars"] < MIN_BARS_FLOW:
        return InstFlow.INSUFFICIENT_DATA
    udvr = sig["udvr"]
    obv_above = sig["obv_above_ema"]
    acc, dist = sig["acc_days"], sig["dist_days"]
    if udvr is None or obv_above is None or acc is None or dist is None:
        return InstFlow.INSUFFICIENT_DATA
    bearish_div = sig["divergence"] == -1
    bullish_div = sig["divergence"] == 1

    # Distribution dominates (worst-signal-wins within the flow read).
    if dist >= DIST_DAYS_MAX or udvr < UDVR_DISTRIB or (bearish_div and not obv_above):
        return InstFlow.DISTRIBUTING
    if udvr >= UDVR_ACCUM and obv_above and not bearish_div:
        return InstFlow.ACCUMULATING
    if obv_above or udvr >= UDVR_INTEREST or acc > dist or bullish_div:
        return InstFlow.EARLY_INTEREST
    return InstFlow.NO_INTEREST


def classify(df: pd.DataFrame | None) -> dict:
    """The full structure read for one name: the two enums PLUS every underlying
    signal (for the per-row detail drawer / snapshot provenance). ONE pass over
    the bars. Pure — no I/O, no clock, never mutates ``df``."""
    sig = _signals(df)
    return {
        "base_stage": _base_stage(sig),
        "inst_flow": _inst_flow(sig),
        "signals": sig,
    }


def classify_symbol(df: pd.DataFrame | None) -> tuple[str, str]:
    """The spec contract: ``(BaseStage, InstFlow)`` for one symbol's bars. A thin
    tuple over ``classify`` — the scan splits these into the BASE and INST columns
    (display-only split, one call per row)."""
    out = classify(df)
    return out["base_stage"], out["inst_flow"]


# ---------------------------------------------------------------------------
# Structure-cell entrability grid (pure over the two enums). This is an INPUT to
# the worst-signal-wins VERDICT composed elsewhere — it never sees the regime or
# the Symbol Genius color here.
# ---------------------------------------------------------------------------
def structure_entrability(base_stage: str, inst_flow: str) -> str:
    """Map the (BaseStage, InstFlow) cell to entrability:

      * EARLY_ADVANCE × (ACCUMULATING | EARLY_INTEREST) -> READY
      * LATE_ADVANCE  × ACCUMULATING                    -> CAUTION
      * TOPPING / DECLINING (any flow)                  -> BLOCKED
      * any DISTRIBUTING                                -> BLOCKED
      * INSUFFICIENT_DATA (either axis)                 -> BLOCKED (never guess entrable)
      * everything else (e.g. BASING × EARLY_INTEREST)  -> WATCH (valid, not entrable)
    """
    if base_stage in (BaseStage.INSUFFICIENT_DATA, BaseStage.TOPPING, BaseStage.DECLINING):
        return Entrability.BLOCKED
    if inst_flow in (InstFlow.INSUFFICIENT_DATA, InstFlow.DISTRIBUTING):
        return Entrability.BLOCKED
    if base_stage == BaseStage.EARLY_ADVANCE and inst_flow in (
            InstFlow.ACCUMULATING, InstFlow.EARLY_INTEREST):
        return Entrability.READY
    if base_stage == BaseStage.LATE_ADVANCE and inst_flow == InstFlow.ACCUMULATING:
        return Entrability.CAUTION
    return Entrability.WATCH
