"""
Indicator math for the rotation dashboard.

The five key sector indicators intentionally mirror the formulas supplied in
this project brief:
- RS3M = symbol 63-bar percent change minus SPY 63-bar percent change, matching
  the standard ~3 trading-month lookback used on daily Schwab/thinkorswim data.
- RS3M_MOM = percent change of current RS3M versus the average RS3M over the
  latest 10 RS3M readings, using ``abs(average)`` in the denominator.
- VolumeRatio = latest volume divided by the prior 20-day average volume, times 100.
- VolumeAccel = latest 5-day average volume divided by the previous 5-day
  average volume, times 100.
- RSI = 14-period Wilder RSI by default, matching thinkorswim's default RSI.
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
    """Latest volume divided by the prior 20-day average volume, times 100."""
    v = vols.dropna()
    if len(v) < window + 1:
        return None
    avg = v.iloc[-(window + 1):-1].mean()
    if avg == 0:
        return None
    return float(v.iloc[-1] / avg * 100)


def volume_acceleration(vols: pd.Series, window: int = 5) -> float | None:
    """Latest 5-day average volume divided by the previous 5-day average."""
    v = vols.dropna()
    if len(v) < window * 2:
        return None
    current_avg = v.iloc[-window:].mean()
    previous_avg = v.iloc[-(window * 2):-window].mean()
    if previous_avg == 0:
        return None
    return float(current_avg / previous_avg * 100)


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
                smooth: int = 1, method: str = "return_spread", ema_span: int = 1) -> pd.Series:
    """Symbol-vs-SPY relative strength over `lookback` rows, in percent.

    The default uses raw Schwab/thinkorswim daily closes over 63 trading bars
    (about three months): ``((current / lookback_ago) - 1) * 100`` for the
    symbol minus the same SPY percent change. The optional legacy EMA/smoothing
    arguments are retained so older config payloads do not break.
    """
    df = pd.DataFrame({"s": sym_close, "p": spy_close}).dropna()
    rs_method = (method or "return_spread").lower()
    span = max(int(ema_span or 1), 1)

    if rs_method == "ema" and span > 1:
        sym_base = ema(df["s"], span)
        spy_base = ema(df["p"], span)
    else:
        sym_base = df["s"]
        spy_base = df["p"]

    sym_ret = ((sym_base - sym_base.shift(lookback)) / sym_base.shift(lookback)) * 100
    spy_ret = ((spy_base - spy_base.shift(lookback)) / spy_base.shift(lookback)) * 100
    rs = sym_ret - spy_ret
    if smooth and smooth > 1:
        rs = rs.ewm(span=smooth, adjust=False).mean()
    return rs.dropna()


def rs3m_momentum(rs3m_values: pd.Series, window: int = 10) -> float | None:
    """Current RS3M percent change versus the latest 10-value RS3M average."""
    values = rs3m_values.dropna()
    if len(values) < window:
        return None
    latest = values.iloc[-window:]
    avg = latest.mean()
    if avg == 0:
        return 0.0
    current = latest.iloc[-1]
    return float(((current - avg) / abs(avg)) * 100)


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
    if spy_bars is not None and len(spy_bars) >= lookback + mom_window and len(close) >= lookback + mom_window:
        series = rs3m_series(close, spy_bars["Close"],
                             lookback=lookback, smooth=getattr(cfg, "MOM_SMOOTH", 1),
                             method=getattr(cfg, "RS3M_METHOD", "return_spread"),
                             ema_span=getattr(cfg, "RS3M_EMA_SPAN", 1))
        if len(series) >= mom_window:
            rs3m_val = float(series.iloc[-1])
            rs3m_mom = rs3m_momentum(series, mom_window)
            mom_scale = float(getattr(cfg, "MOM_SCALE", 1.0) or 1.0)
            if rs3m_mom is not None:
                rs3m_mom *= mom_scale
            if len(series) >= mom_window + 1:
                prev_mom = rs3m_momentum(series.iloc[:-1], mom_window)
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
        "volRatio": _round(volume_ratio(vol), 0),
        "volAccel": _round(volume_acceleration(vol), 0),
        "mfi": _round(mfi(high, low, close, vol)),
        "rs3m": _round(rs3m_val, 2),
        "rs3mMom": _round(rs3m_mom, 2),
        "rs3mTrend": rs3m_trend,
        "rs3mMethod": getattr(cfg, "RS3M_METHOD", "return_spread"),
        "rs3mLookback": lookback,
        "rs3mMomWindow": mom_window,
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
