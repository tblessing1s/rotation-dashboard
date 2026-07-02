"""CFM indicator math on daily OHLCV frames.

All functions take a pandas DataFrame with columns Open/High/Low/Close/Volume
indexed by date (ascending), as returned by data_handler. They return plain
floats (or None when there is insufficient history) so they serialize straight
to JSON for the API.
"""
from __future__ import annotations

import math

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


def hist_vol(df: pd.DataFrame, window: int = 20) -> float | None:
    """Annualized realized (historical) volatility over `window` daily bars, as a
    percent — the yardstick for judging whether an option's IV is rich or cheap.
    std of daily log returns × sqrt(252)."""
    c = _close(df)
    if len(c) < window + 1:
        return None
    rets = np.log(c / c.shift(1)).dropna()
    if len(rets) < window:
        return None
    vol = rets.tail(window).std(ddof=1) * np.sqrt(252) * 100
    return None if pd.isna(vol) else float(round(vol, 2))


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


def short_strike_from_table(price: float, atr_value: float, atr_mult: float,
                            itm_pct: float) -> float:
    """Weekly short strike from the regime x posture table (config.STRIKE_TABLE):
    the DEEPER (more protective, further below price) of an ATR-distance strike
    and an ITM%-floor strike, rounded to $0.50. `itm_pct` is a decimal (0.03 = 3%).
    """
    atr_strike = price - atr_mult * atr_value
    itm_strike = price * (1 - itm_pct)
    raw = min(atr_strike, itm_strike)
    return round(raw * 2) / 2


# ---------------------------------------------------------------------------
# Black–Scholes greeks
# ---------------------------------------------------------------------------
# Schwab's chain `delta` field disagrees with thinkorswim (and is internally
# inconsistent with its own reported IV), so we recompute call delta the way TOS
# does: imply volatility from the option mark, then Black–Scholes–Merton. A
# continuous dividend yield q (decimal, 0 by default) is carried through: it
# discounts the spot leg by e^(−qT) and lowers the forward (r−q in d1), so a
# dividend-paying underlying gets a correctly lower call delta — most visible on
# the long-dated LEAP. Still a continuous-yield approximation of discrete
# dividends, and European (early exercise near ex-div is ignored — tiny here).

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    return (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def bs_call_delta(S: float, K: float, T: float, r: float, sigma: float,
                  q: float = 0.0) -> float | None:
    """Black–Scholes–Merton call delta = e^(−qT)·N(d1)."""
    if not (S and S > 0 and K and K > 0 and T and T > 0 and sigma and sigma > 0):
        return None
    return math.exp(-q * T) * _norm_cdf(_d1(S, K, T, r, sigma, q))


def _bs_call_price(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    d1 = _d1(S, K, T, r, sigma, q)
    d2 = d1 - sigma * math.sqrt(T)
    return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def implied_vol_call(price: float | None, S: float, K: float, T: float, r: float,
                     q: float = 0.0) -> float | None:
    """Implied volatility of a call from its price, via bisection. Price is
    monotonic in sigma, so bisection is robust even for deep-ITM options where
    Newton's method fails on tiny vega. Returns None when the price is outside the
    no-arbitrage band (e.g. a stale mark below intrinsic)."""
    if price is None or not (S and S > 0 and K and K > 0 and T and T > 0):
        return None
    lo, hi = 1e-4, 5.0
    p_lo, p_hi = _bs_call_price(S, K, T, r, lo, q), _bs_call_price(S, K, T, r, hi, q)
    if not (p_lo - 1e-9 <= price <= p_hi + 1e-9):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        pm = _bs_call_price(S, K, T, r, mid, q)
        if abs(pm - price) < 1e-6:
            return mid
        if pm < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _bs_put_price(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    d1 = _d1(S, K, T, r, sigma, q)
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


def implied_vol_put(price: float | None, S: float, K: float, T: float, r: float,
                    q: float = 0.0) -> float | None:
    """Implied volatility of a put from its price, via bisection. Used to recover
    a skew-aware vol for a deep-ITM CALL from its same-strike OTM put: the put
    carries real time value (so its price implies a usable vol) even when the
    provider's IV field is missing — e.g. off-hours, when Schwab returns NaN IVs
    and the ITM call's own near-intrinsic mark can't imply anything."""
    if price is None or not (S and S > 0 and K and K > 0 and T and T > 0):
        return None
    lo, hi = 1e-4, 5.0
    p_lo, p_hi = _bs_put_price(S, K, T, r, lo, q), _bs_put_price(S, K, T, r, hi, q)
    if not (p_lo - 1e-9 <= price <= p_hi + 1e-9):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        pm = _bs_put_price(S, K, T, r, mid, q)
        if abs(pm - price) < 1e-6:
            return mid
        if pm < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def call_greeks(S: float | None, K: float | None, dte: int | None, mark: float | None,
                reported_iv: float | None = None,
                r: float = config.RISK_FREE_RATE, q: float = 0.0) -> tuple[float | None, float | None]:
    """(delta, iv_pct) for a call via Black–Scholes–Merton.

    Uses Schwab's reported per-contract IV when present — thinkorswim is Schwab,
    so its greeks come from that same IV, and recomputing delta from it matches
    TOS even though Schwab's pre-computed `delta` field does not. Falls back to
    implying vol from the mark when no IV is reported (and the mid-price is a
    usable estimate for longer-dated contracts). `q` is the underlying's
    continuous dividend yield (decimal), which lowers a dividend payer's call
    delta. Returns (None, None) when inputs are insufficient."""
    T = (dte or 0) / 365.0
    if not (S and K and T > 0):
        return None, None
    iv = (reported_iv / 100.0) if reported_iv else None
    if iv is None and mark:
        iv = implied_vol_call(mark, S, K, T, r, q)
    if not iv or iv <= 0:
        return None, None
    d = bs_call_delta(S, K, T, r, iv, q)
    return (round(d, 4) if d is not None else None, round(iv * 100, 2))


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


def get_leap_strikes(contracts: list[dict], underlying_price: float | None,
                     target_delta: float = config.LEAP_TARGET_DELTA,
                     target_dte: int = config.LEAP_TARGET_DTE,
                     delta_min: float = config.LEAP_DELTA_MIN,
                     delta_max: float = config.LEAP_DELTA_MAX,
                     count: int = 5) -> list[dict]:
    """Candidate LEAP strikes to choose from, not just one.

    Picks the expiration whose DTE is closest to target_dte, then returns the
    call strikes whose |delta| falls in the preferred band [delta_min, delta_max],
    padded out to `count` with the next-closest-by-delta strikes so there's always
    a choice (e.g. when the chain only lists 0.93/0.85 around the band). Falls back
    to a strike heuristic when greeks are missing. Each row is augmented with
    mark/intrinsic/extrinsic and a `suggested` flag on the strike nearest
    target_delta. Sorted ascending by strike."""
    pool = [c for c in contracts if c.get("dte") is not None]
    if not pool:
        return []
    best_dte = min({c["dte"] for c in pool}, key=lambda d: abs(d - target_dte))
    pool = [c for c in pool if c["dte"] == best_dte]

    with_delta = [c for c in pool if c.get("delta") is not None]
    if with_delta:
        in_band = [c for c in with_delta if delta_min <= abs(c["delta"]) <= delta_max]
        nearest = sorted(with_delta, key=lambda c: abs(abs(c["delta"]) - target_delta))
        chosen = list(in_band)
        for c in nearest:  # pad with nearest-by-delta until we have `count`
            if len(chosen) >= count:
                break
            if c not in chosen:
                chosen.append(c)
    else:
        # No greeks (market closed): approximate a deep-ITM strike near the target.
        proxy = (underlying_price or 0) * target_delta
        chosen = sorted(pool, key=lambda c: abs(c["strike"] - proxy))[:count]

    by_strike = {}
    for c in chosen:
        by_strike.setdefault(c["strike"], c)
    rows = sorted(by_strike.values(), key=lambda c: c["strike"])
    if any(c.get("delta") is not None for c in rows):
        sug = min(rows, key=lambda c: abs(abs(c.get("delta") or 0) - target_delta))
    else:
        proxy = (underlying_price or 0) * target_delta
        sug = min(rows, key=lambda c: abs(c["strike"] - proxy))
    out = []
    for c in rows:
        row = _augment(c, underlying_price)
        row["suggested"] = c is sug
        out.append(row)
    return out


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
