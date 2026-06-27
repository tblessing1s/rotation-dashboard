"""CFM indicator math on daily OHLCV frames.

All functions take a pandas DataFrame with columns Open/High/Low/Close/Volume
indexed by date (ascending), as returned by data_handler. They return plain
floats (or None when there is insufficient history) so they serialize straight
to JSON for the API.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


def _close(df: pd.DataFrame) -> pd.Series:
    return df["Close"].astype(float)


def last(df: pd.DataFrame | None) -> float | None:
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def sma(df: pd.DataFrame, window: int = config.MA_WINDOW) -> float | None:
    c = _close(df)
    if len(c) < window:
        return None
    return float(c.rolling(window).mean().iloc[-1])


def rsi(df: pd.DataFrame, window: int = config.RSI_WINDOW) -> float | None:
    c = _close(df)
    if len(c) < window + 1:
        return None
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing (RMA).
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    val = out.iloc[-1]
    return None if pd.isna(val) else float(val)


def atr(df: pd.DataFrame, window: int = config.ATR_WINDOW) -> float | None:
    """Wilder ATR over `window` bars (CFM uses 9)."""
    if len(df) < window + 1:
        return None
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    rma = tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    val = rma.iloc[-1]
    return None if pd.isna(val) else float(val)


def atr_pct(df: pd.DataFrame, window: int = config.ATR_WINDOW) -> float | None:
    """ATR as a percent of the latest close — the consolidation gauge."""
    a = atr(df, window)
    px = last(df)
    if a is None or not px:
        return None
    return round(a / px * 100, 2)


def atr_expanding(df: pd.DataFrame, window: int = config.ATR_WINDOW, lookback: int = 10) -> bool | None:
    """True when current ATR exceeds the ATR `lookback` bars ago (volatility
    expanding, the sector-strength condition)."""
    if len(df) < window + lookback + 1:
        return None
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    rma = tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    now, then = rma.iloc[-1], rma.iloc[-1 - lookback]
    if pd.isna(now) or pd.isna(then):
        return None
    return bool(now > then)


def pct_from_ma(df: pd.DataFrame, window: int = config.MA_WINDOW) -> float | None:
    """Percent distance of close above (+) / below (-) its `window`-day MA."""
    ma = sma(df, window)
    px = last(df)
    if ma is None or not px or ma == 0:
        return None
    return round((px / ma - 1) * 100, 2)


def rs3m(df: pd.DataFrame, bench: pd.DataFrame, lookback: int = config.RS3M_LOOKBACK) -> float | None:
    """Relative strength vs a benchmark over `lookback` bars, as a percent.

    ratio = symbol_close / bench_close, aligned on date. RS3M is the percent
    change of that ratio over the lookback window: (ratio_now/ratio_then - 1)*100.
    Positive = the symbol outran the benchmark over the period.
    """
    if df is None or bench is None or df.empty or bench.empty:
        return None
    ratio = (_close(df) / _close(bench).reindex(df.index)).dropna()
    if len(ratio) < lookback + 1:
        return None
    now = ratio.iloc[-1]
    then = ratio.iloc[-1 - lookback]
    if not then:
        return None
    # Cast to native float: round() on a numpy scalar returns a numpy scalar,
    # and downstream comparisons would then yield numpy.bool_ (not JSON
    # serializable). np.float64 serializes fine, but the booleans it spawns do not.
    return float(round((now / then - 1) * 100, 2))


def above_ma(df: pd.DataFrame, window: int = config.BREADTH_MA_WINDOW) -> bool | None:
    ma = sma(df, window)
    px = last(df)
    if ma is None or not px:
        return None
    return bool(px > ma)


def breadth(frames: dict[str, pd.DataFrame], window: int = config.BREADTH_MA_WINDOW) -> float | None:
    """Percent of the supplied frames whose latest close is above their MA."""
    flags = [above_ma(df, window) for df in frames.values() if df is not None and not df.empty]
    flags = [f for f in flags if f is not None]
    if not flags:
        return None
    return round(sum(flags) / len(flags) * 100, 1)


def consolidating(df: pd.DataFrame) -> bool | None:
    """Low ATR% and price near MA21 = consolidating (not breaking out)."""
    a = atr_pct(df)
    dist = pct_from_ma(df)
    if a is None or dist is None:
        return None
    return bool(a <= config.CONSOLIDATION_ATR_PCT_MAX
               and abs(dist) <= config.CONSOLIDATION_MA21_DIST_MAX)


def short_strike(price: float, atr_value: float, mult: float = config.SHORT_ATR_MULT) -> float:
    """Suggested weekly short-call strike = price - mult*ATR, rounded to 0.5."""
    raw = price - mult * atr_value
    return round(raw * 2) / 2


# ---------------------------------------------------------------------------
# Option-chain helpers
# ---------------------------------------------------------------------------
# These operate on the normalized call-contract dicts produced by
# schwab_api.parse_call_chain (strike/expiration/dte/bid/ask/mark/delta/...), so
# the math stays provider-agnostic and JSON-serializable.

def calculate_extrinsic(bid: float | None, ask: float | None, strike: float,
                        underlying_price: float | None) -> float | None:
    """Extrinsic (time) value per share = option midpoint − intrinsic.

    Midpoint is (bid+ask)/2; intrinsic for a call is max(underlying − strike, 0).
    Returns None when the quote is missing; clamps to ≥ 0 (a stale ITM mark can
    print just under intrinsic)."""
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2
    intrinsic = max((underlying_price or 0) - strike, 0)
    return round(max(mid - intrinsic, 0), 4)


def _augment(contract: dict, underlying_price: float | None) -> dict:
    """Add mark (midpoint fallback), intrinsic, and extrinsic to a contract."""
    bid, ask = contract.get("bid"), contract.get("ask")
    mark = contract.get("mark")
    if mark is None and bid is not None and ask is not None:
        mark = round((bid + ask) / 2, 4)
    intrinsic = round(max((underlying_price or 0) - contract["strike"], 0), 4)
    return {**contract, "mark": mark, "intrinsic": intrinsic,
            "extrinsic": calculate_extrinsic(bid, ask, contract["strike"], underlying_price)}


def find_leap_strike(contracts: list[dict], underlying_price: float | None,
                     target_delta: float = config.LEAP_TARGET_DELTA,
                     target_dte: int = config.LEAP_TARGET_DTE) -> dict | None:
    """Auto-pick the deep-ITM LEAP: the expiration whose DTE is closest to
    target_dte, then within it the strike whose |delta| is closest to
    target_delta. Falls back to a strike heuristic when deltas are absent (Schwab
    omits greeks when the market is closed). Returns the chosen contract augmented
    with mark/intrinsic/extrinsic, or None when there are no contracts."""
    pool = [c for c in contracts if c.get("dte") is not None]
    if not pool:
        return None
    best_dte = min({c["dte"] for c in pool}, key=lambda d: abs(d - target_dte))
    pool = [c for c in pool if c["dte"] == best_dte]

    have_delta = [c for c in pool if c.get("delta") is not None]
    if have_delta:
        chosen = min(have_delta, key=lambda c: abs(abs(c["delta"]) - target_delta))
    else:
        # No greeks: approximate a ~0.90-delta call as a strike well in the money.
        # target_delta 0.90 → roughly (1 − 0.90) below spot as a rough proxy.
        proxy = (underlying_price or 0) * (1 - (1 - target_delta))
        chosen = min(pool, key=lambda c: abs(c["strike"] - proxy))
    return _augment(chosen, underlying_price)


def get_nearby_strikes(contracts: list[dict], target_strike: float,
                       underlying_price: float | None, count: int = 3) -> list[dict]:
    """The `count` available strikes nearest `target_strike` (a single
    expiration's contracts), sorted ascending and each augmented with
    mark/intrinsic/extrinsic plus a `suggested` flag on the closest strike."""
    by_strike = {c["strike"]: c for c in contracts if c.get("strike") is not None}
    if not by_strike:
        return []
    nearest = sorted(by_strike.values(), key=lambda c: abs(c["strike"] - target_strike))[:count]
    closest = min(by_strike, key=lambda s: abs(s - target_strike))
    out = []
    for c in sorted(nearest, key=lambda c: c["strike"]):
        row = _augment(c, underlying_price)
        row["suggested"] = c["strike"] == closest
        out.append(row)
    return out
