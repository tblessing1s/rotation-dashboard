"""
Indicator math for the rotation dashboard.

All functions take plain Python lists / pandas Series and return scalars or
short series. Formulas verified against reference values (RSI matches Wilder).

Notes on RS3M calibration:
- RS3M can use EMA-smoothed prices before measuring the symbol-vs-SPY
  relative-strength return spread. This better mirrors EMA-based watchlist
  studies than a raw close-to-close return spread.
- The exact thinkorswim value can still differ if your ThinkScript uses a
  different EMA length, scaling, or formula. Tune RS3M_METHOD, RS3M_EMA_SPAN,
  RS3M_LOOKBACK, MOM_SMOOTH, and MOM_SCALE in config.py until direction, turning
  points, and magnitude line up with your TOS watchlist.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def rsi(closes: pd.Series, period: int = 14) -> float | None:
    """Wilder's RSI with the classic seeding: a simple average over the first
    `period` changes, then recursive smoothing. Matches Wilder's reference."""
    c = closes.dropna().to_numpy(dtype=float)
    if len(c) < period + 1:
        return None
    deltas = np.diff(c)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
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
    if len(vols) < window + 1:
        return None
    avg = vols.iloc[-(window + 1):-1].mean()
    if avg == 0:
        return None
    return float(vols.iloc[-1] / avg * 100)


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


def rs3m_series(sym_close: pd.Series, spy_close: pd.Series, lookback: int = 63,
                smooth: int = 1, method: str = "ema", ema_span: int = 21) -> pd.Series:
    """Relative strength of symbol vs SPY over `lookback` trading days, in %.

    ``method="ema"`` first smooths both close series with an EMA, then compares
    their lookback returns. ``method="return_spread"`` keeps the legacy raw
    close-to-close return spread. ``smooth`` optionally applies a second EMA to
    the RS3M series before RS3M_MOM is derived from it. Index is aligned on the
    intersection of the two series.
    """
    df = pd.DataFrame({"s": sym_close, "p": spy_close}).dropna()
    rs_method = (method or "return_spread").lower()
    span = max(int(ema_span or 1), 1)

    if rs_method == "ema":
        sym_base = ema(df["s"], span)
        spy_base = ema(df["p"], span)
    else:
        sym_base = df["s"]
        spy_base = df["p"]

    sym_ret = sym_base / sym_base.shift(lookback) - 1
    spy_ret = spy_base / spy_base.shift(lookback) - 1
    rs = (sym_ret - spy_ret) * 100
    if smooth and smooth > 1:
        rs = rs.ewm(span=smooth, adjust=False).mean()
    return rs.dropna()


def compute_all(bars: pd.DataFrame, spy_bars: pd.DataFrame | None, cfg) -> dict:
    """bars/spy_bars: DataFrames with columns Open, High, Low, Close, Volume.
    Returns a dict of computed indicators + metadata.
    """
    if bars is None or len(bars) < 64:
        return {"error": "insufficient history"}

    close = bars["Close"]
    high = bars["High"]
    low = bars["Low"]
    vol = bars["Volume"]

    rs3m_val = rs3m_mom = None
    rs3m_trend = None
    if spy_bars is not None and len(spy_bars) >= 64:
        series = rs3m_series(close, spy_bars["Close"],
                             lookback=cfg.RS3M_LOOKBACK, smooth=cfg.MOM_SMOOTH,
                             method=getattr(cfg, "RS3M_METHOD", "ema"),
                             ema_span=getattr(cfg, "RS3M_EMA_SPAN", 21))
        if len(series) > 11:
            rs3m_val = float(series.iloc[-1])
            mom = (series.iloc[-1] - series.iloc[-6]) * cfg.MOM_SCALE
            prev_mom = (series.iloc[-6] - series.iloc[-11]) * cfg.MOM_SCALE
            rs3m_mom = float(mom)
            rs3m_trend = "up" if mom > prev_mom else "down" if mom < prev_mom else "flat"

    ma21 = float(ema(close, 21).iloc[-1])
    price = float(close.iloc[-1])

    return {
        "asOf": str(bars.index[-1].date()),
        "price": round(price, 2),
        "ma21": round(ma21, 2),
        "priceAboveMA21": price > ma21,
        "rsi": _round(rsi(close)),
        "obv": obv_trend(close, vol),
        "volRatio": _round(volume_ratio(vol), 0),
        "mfi": _round(mfi(high, low, close, vol)),
        "rs3m": _round(rs3m_val, 2),
        "rs3mMom": _round(rs3m_mom, 2),
        "rs3mTrend": rs3m_trend,
        "rs3mMethod": getattr(cfg, "RS3M_METHOD", "ema"),
        "rs3mEmaSpan": getattr(cfg, "RS3M_EMA_SPAN", 21),
    }


def _round(v, n=1):
    return None if v is None else round(float(v), n)
