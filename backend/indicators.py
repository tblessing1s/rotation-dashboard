"""
Indicator math for the rotation dashboard.

The key sector indicators intentionally mirror the Thinkorswim formulas supplied
in this project brief and are designed to run on daily Schwab bars:
- RS3M = ``close / close("SPY")`` relative-strength ratio versus the same ratio
  63 bars ago, expressed as a percent change.
- RS3M_Momentum = percent rate-of-change between current RS3M and the supplied
  shifted RS3M reference: ``rs[68] / rs[131] - 1``.
- VolumeRatio = latest volume divided by the 20-bar simple average volume, times 100.
- VolumeAccel = latest 5-bar simple average volume divided by the latest 20-bar
  simple average volume, times 100.
- MFI, Accumulation/Distribution, and RSI use standard daily OHLCV formulas.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def rsi(closes: pd.Series, period: int = 14, method: str = "wilder") -> float | None:
    """Latest RSI value.

    ``method="wilder"`` matches thinkorswim's default RSI study
    (``average type = Wilders``): seed the average gain/loss with the first
    ``period`` changes, then recursively smooth each subsequent change.
    ``method="simple"`` retains the earlier plain average of the latest
    ``period`` changes for backward-compatible fixtures/configs.
    """
    c = closes.dropna().to_numpy(dtype=float)
    if len(c) < period + 1:
        return None

    deltas = np.diff(c)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)

    if (method or "wilder").lower() == "simple":
        avg_gain = gains[-period:].sum() / period
        avg_loss = losses[-period:].sum() / period
    else:
        avg_gain = gains[:period].sum() / period
        avg_loss = losses[:period].sum() / period
        for gain, loss in zip(gains[period:], losses[period:]):
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def obv_trend(closes: pd.Series, vols: pd.Series, ema_span: int = 20) -> str | None:
    if len(closes) < ema_span + 5:
        return None
    direction = np.sign(closes.diff().fillna(0))
    obv = (direction * vols).cumsum()
    obv_ema = obv.ewm(span=ema_span, adjust=False).mean()
    last, le = obv.iloc[-1], obv_ema.iloc[-1]
    slope = obv.iloc[-1] - obv.iloc[-6]
    if last > le and slope > 0:
        return "rising"
    if last < le and slope < 0:
        return "falling"
    return "flat"


def volume_ratio(vols: pd.Series, window: int = 20) -> float | None:
    """Latest volume divided by the latest 20-bar simple average, times 100.

    This matches the supplied thinkScript:
    ``volume / MovingAverage(AverageType.SIMPLE, volume, 20) * 100``.
    The 20-bar average includes the current bar, just as thinkorswim's study
    value does on the current daily candle.
    """
    v = vols.dropna()
    if len(v) < window:
        return None
    avg = v.iloc[-window:].mean()
    if avg == 0:
        return None
    return float(v.iloc[-1] / avg * 100)


def volume_acceleration(vols: pd.Series, short_window: int = 5, long_window: int = 20) -> float | None:
    """Latest 5-bar SMA volume divided by the latest 20-bar SMA volume.

    This matches the supplied thinkScript:
    ``MovingAverage(SIMPLE, volume, 5) / MovingAverage(SIMPLE, volume, 20) * 100``.
    """
    v = vols.dropna()
    if len(v) < max(short_window, long_window):
        return None
    vol5 = v.iloc[-short_window:].mean()
    vol20 = v.iloc[-long_window:].mean()
    if vol20 == 0:
        return None
    return float(vol5 / vol20 * 100)


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    tp = (high + low + close) / 3
    raw = tp * vol
    delta = tp.diff()
    pos = raw.where(delta > 0, 0.0)
    neg = raw.where(delta < 0, 0.0)
    pos_sum = pos.iloc[-period:].sum()
    neg_sum = neg.iloc[-period:].sum()
    if neg_sum == 0:
        return 100.0
    mr = pos_sum / neg_sum
    return float(100 - 100 / (1 + mr))


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def moving_average(series: pd.Series, window: int, method: str = "sma") -> pd.Series:
    """Moving average helper for MA21. Defaults to SMA like thinkorswim's
    SimpleMovingAvg study; ``method="ema"`` preserves the previous behavior.
    """
    if (method or "sma").lower() == "ema":
        return ema(series, window)
    return series.rolling(window=window, min_periods=window).mean()


def rs3m_series(sym_close: pd.Series, spy_close: pd.Series, lookback: int = 63,
                smooth: int = 1, method: str = "ratio", ema_span: int = 1) -> pd.Series:
    """RS3M from the supplied thinkScript formula, using SPY as benchmark.

    thinkScript equivalent::

        def rs = close / close("SPY");
        def past = rs[63];
        plot RS3M = if !IsNaN(past) then (rs / past - 1) * 100 else Double.NaN;

    ``method`` keeps legacy options alive for older config payloads; the default
    and new ``ratio`` mode are the exact supplied formula.
    """
    df = pd.DataFrame({"s": sym_close, "p": spy_close}).dropna()
    rs_method = (method or "ratio").lower()
    span = max(int(ema_span or 1), 1)

    if rs_method == "ema" and span > 1:
        sym_base = ema(df["s"], span)
        spy_base = ema(df["p"], span)
        ratio = sym_base / spy_base
    elif rs_method == "return_spread":
        sym_ret = ((df["s"] - df["s"].shift(lookback)) / df["s"].shift(lookback)) * 100
        spy_ret = ((df["p"] - df["p"].shift(lookback)) / df["p"].shift(lookback)) * 100
        rs = sym_ret - spy_ret
        if smooth and smooth > 1:
            rs = rs.ewm(span=smooth, adjust=False).mean()
        return rs.dropna()
    else:
        ratio = df["s"] / df["p"]

    rs = ((ratio / ratio.shift(lookback)) - 1) * 100
    if smooth and smooth > 1:
        rs = rs.ewm(span=smooth, adjust=False).mean()
    return rs.dropna()


def rs3m_momentum_from_closes(sym_close: pd.Series, spy_close: pd.Series,
                              current_lookback: int = 63,
                              past_end_lag: int = 68,
                              past_lookback: int = 131) -> float | None:
    """RS3M_Momentum from the supplied thinkScript formula.

    The provided study computes current RS3M from ``rs / rs[63]`` and its
    comparison value from ``rs[68] / rs[131]``. The latter is intentionally
    parameterized by absolute lags to mirror the script exactly.
    """
    df = pd.DataFrame({"s": sym_close, "p": spy_close}).dropna()
    if len(df) <= max(current_lookback, past_end_lag, past_lookback):
        return None
    ratio = df["s"] / df["p"]
    current_past = ratio.iloc[-(current_lookback + 1)]
    prior_end = ratio.iloc[-(past_end_lag + 1)]
    prior_past = ratio.iloc[-(past_lookback + 1)]
    if current_past == 0 or prior_past == 0:
        return None
    current_rs3m = ((ratio.iloc[-1] / current_past) - 1) * 100
    previous_rs3m = ((prior_end / prior_past) - 1) * 100
    if previous_rs3m == 0:
        return 0.0
    return float((current_rs3m - previous_rs3m) / previous_rs3m * 100)


def rs3m_momentum(rs3m_values: pd.Series, window: int = 10) -> float | None:
    """Legacy current-RS3M versus latest-window average momentum."""
    values = rs3m_values.dropna()
    if len(values) < window:
        return None
    latest = values.iloc[-window:]
    avg = latest.mean()
    if avg == 0:
        return 0.0
    current = latest.iloc[-1]
    return float(((current - avg) / abs(avg)) * 100)


def accumulation_distribution(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series) -> pd.Series:
    """Accumulation/Distribution Line using the standard Chaikin formula."""
    df = pd.DataFrame({"h": high, "l": low, "c": close, "v": vol}).dropna()
    if df.empty:
        return pd.Series(dtype=float)
    denominator = df["h"] - df["l"]
    multiplier = ((df["c"] - df["l"]) - (df["h"] - df["c"])) / denominator.replace(0, np.nan)
    money_flow_volume = multiplier.fillna(0.0) * df["v"]
    return money_flow_volume.cumsum()


def accumulation_distribution_trend(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series,
                                    signal_span: int = 20, slope_bars: int = 5) -> str | None:
    """Rising/flat/falling state for the Accumulation/Distribution Line."""
    ad = accumulation_distribution(high, low, close, vol)
    if len(ad) < signal_span + slope_bars:
        return None
    signal = ad.ewm(span=signal_span, adjust=False).mean()
    slope = ad.iloc[-1] - ad.iloc[-(slope_bars + 1)]
    if ad.iloc[-1] > signal.iloc[-1] and slope > 0:
        return "rising"
    if ad.iloc[-1] < signal.iloc[-1] and slope < 0:
        return "falling"
    return "flat"


def compute_all(bars: pd.DataFrame, spy_bars: pd.DataFrame | None, cfg) -> dict:
    """bars/spy_bars: DataFrames with columns Open, High, Low, Close, Volume.
    Returns a dict of computed indicators + metadata.
    """
    if bars is None or len(bars) < 21:
        return {"error": "insufficient history"}

    close = bars["Close"]
    high = bars["High"]
    low = bars["Low"]
    vol = bars["Volume"]

    rs3m_val = rs3m_mom = None
    rs3m_trend = None
    lookback = getattr(cfg, "RS3M_LOOKBACK", 63)
    mom_window = getattr(cfg, "RS3M_MOM_WINDOW", 10)
    mom_past_end_lag = getattr(cfg, "RS3M_MOM_PAST_END_LAG", 68)
    mom_past_lookback = getattr(cfg, "RS3M_MOM_PAST_LOOKBACK", 131)
    required_rs_rows = lookback + 1
    required_mom_rows = max(lookback, mom_past_end_lag, mom_past_lookback) + 1
    if spy_bars is not None and len(spy_bars) >= required_rs_rows and len(close) >= required_rs_rows:
        series = rs3m_series(close, spy_bars["Close"],
                             lookback=lookback, smooth=getattr(cfg, "MOM_SMOOTH", 1),
                             method=getattr(cfg, "RS3M_METHOD", "ratio"),
                             ema_span=getattr(cfg, "RS3M_EMA_SPAN", 1))
        if len(series) > 0:
            rs3m_val = float(series.iloc[-1])
            if len(spy_bars) >= required_mom_rows and len(close) >= required_mom_rows:
                rs3m_mom = rs3m_momentum_from_closes(
                    close,
                    spy_bars["Close"],
                    current_lookback=lookback,
                    past_end_lag=mom_past_end_lag,
                    past_lookback=mom_past_lookback,
                )
                mom_scale = float(getattr(cfg, "MOM_SCALE", 1.0) or 1.0)
                if rs3m_mom is not None:
                    rs3m_mom *= mom_scale
                prev_mom = rs3m_momentum_from_closes(
                    close.iloc[:-1],
                    spy_bars["Close"].iloc[:-1],
                    current_lookback=lookback,
                    past_end_lag=mom_past_end_lag,
                    past_lookback=mom_past_lookback,
                )
                if prev_mom is not None:
                    prev_mom *= mom_scale
                if prev_mom is not None and rs3m_mom is not None:
                    rs3m_trend = "up" if rs3m_mom > prev_mom else "down" if rs3m_mom < prev_mom else "flat"
            if rs3m_trend is None and rs3m_mom is not None:
                rs3m_trend = "up" if rs3m_mom > 0 else "down" if rs3m_mom < 0 else "flat"

    ma_method = getattr(cfg, "MA21_METHOD", "sma")
    ma21_series = moving_average(close, 21, ma_method)
    ma21 = float(ma21_series.iloc[-1])
    price = float(close.iloc[-1])

    return {
        "asOf": str(bars.index[-1].date()),
        "price": round(price, 2),
        "ma21": _round(ma21, 2),
        "priceAboveMA21": price > ma21,
        "rsi": _round(rsi(close, method=getattr(cfg, "RSI_METHOD", "wilder"))),
        "obv": obv_trend(close, vol),
        "accDist": accumulation_distribution_trend(high, low, close, vol),
        "volRatio": _round(volume_ratio(vol), 0),
        "volAccel": _round(volume_acceleration(vol), 0),
        "mfi": _round(mfi(high, low, close, vol)),
        "rs3m": _round(rs3m_val, 2),
        "rs3mMom": _round(rs3m_mom, 2),
        "rs3mTrend": rs3m_trend,
        "rs3mMethod": getattr(cfg, "RS3M_METHOD", "ratio"),
        "rs3mLookback": lookback,
        "rs3mMomWindow": mom_window,
        "rs3mMomPastEndLag": mom_past_end_lag,
        "rs3mMomPastLookback": mom_past_lookback,
        "rsiMethod": getattr(cfg, "RSI_METHOD", "wilder"),
        "ma21Method": ma_method,
    }


def _round(v, n=1):
    return None if v is None else round(float(v), n)


# ---------------------------------------------------------------------------
# Support / resistance — on-demand level detection for the Entry Watch cards.
# Computed from the same stored daily bars as the indicators above. Pivots
# (local swing highs/lows) are clustered into price zones, then scored by how
# many swings touched the zone and how much volume traded inside it.
# ---------------------------------------------------------------------------
def _swing_pivots(high, low, window: int):
    """Local maxima of the high series and minima of the low series.

    A bar is a pivot high if its high is the max across +/- ``window`` bars
    (and symmetrically for pivot lows). Returns two lists of prices.
    """
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    n = len(h)
    highs, lows = [], []
    for i in range(window, n - window):
        seg_h = h[i - window:i + window + 1]
        seg_l = l[i - window:i + window + 1]
        if h[i] >= seg_h.max():
            highs.append(h[i])
        if l[i] <= seg_l.min():
            lows.append(l[i])
    return highs, lows


def _cluster(levels, tol):
    """Group sorted price levels into zones where neighbours are within ``tol``."""
    if not levels:
        return []
    levels = sorted(levels)
    groups = [[levels[0]]]
    for lv in levels[1:]:
        if lv - groups[-1][-1] <= tol:
            groups[-1].append(lv)
        else:
            groups.append([lv])
    return groups


def support_resistance(bars: pd.DataFrame, swing_window: int = 5,
                       tol_pct: float = 0.015, max_zones: int = 3) -> dict:
    """Detect support/resistance zones from daily bars.

    Returns nearest-first support/resistance zone lists (each with band,
    centre, distance-to-price %, swing touches, and a strength score), plus a
    breakout trigger above the nearest resistance and a stop below the nearest
    support. Zones are split by whether their centre sits below (support) or
    at/above (resistance) the latest close.
    """
    if bars is None or len(bars) < swing_window * 2 + 10:
        return {"error": "insufficient history"}

    high = bars["High"].astype(float)
    low = bars["Low"].astype(float)
    close = bars["Close"].astype(float)
    vol = bars["Volume"].astype(float).fillna(0.0)
    price = float(close.iloc[-1])
    if price <= 0:
        return {"error": "insufficient history"}

    pivot_highs, pivot_lows = _swing_pivots(high, low, swing_window)
    groups = _cluster(pivot_highs + pivot_lows, price * tol_pct)
    avg_vol = float(vol.tail(60).mean()) or 1.0

    zones = []
    for grp in groups:
        lo, hi = min(grp), max(grp)
        center = sum(grp) / len(grp)
        in_band = (close >= lo * (1 - tol_pct)) & (close <= hi * (1 + tol_pct))
        vol_score = float(vol[in_band].sum()) / (avg_vol * len(grp))
        zones.append({
            "low": round(lo, 2),
            "high": round(hi, 2),
            "center": round(center, 2),
            "distancePct": round((center - price) / price * 100, 2),
            "touches": len(grp),
            "strength": round(len(grp) + min(vol_score, 5.0), 1),
        })

    support = sorted((z for z in zones if z["center"] < price),
                     key=lambda z: price - z["center"])[:max_zones]
    resistance = sorted((z for z in zones if z["center"] >= price),
                        key=lambda z: z["center"] - price)[:max_zones]

    nearest_support = support[0] if support else None
    nearest_resistance = resistance[0] if resistance else None

    return {
        "price": round(price, 2),
        "support": support,
        "resistance": resistance,
        "nearestSupport": nearest_support,
        "nearestResistance": nearest_resistance,
        "breakoutTrigger": round(nearest_resistance["high"] * 1.002, 2) if nearest_resistance else None,
        "stop": round(nearest_support["low"] * 0.99, 2) if nearest_support else None,
    }
