"""Level 1 macro calculations for the rotation dashboard.

Pure functions only: every calculator takes already-fetched series/bars as
arguments. Fetching lives in the providers package and runs inside scheduled
ingestion (see ingest.py) — never at view time.
"""
from __future__ import annotations

import pandas as pd


def _series_change(series: pd.Series, periods: int) -> float:
    """Return latest value minus the value N observations ago."""
    prior_idx = -(periods + 1)
    prior = float(series.iloc[prior_idx]) if len(series) > periods else float(series.iloc[0])
    return float(series.iloc[-1]) - prior


def _cpi_yoy(series: pd.Series, offset: int = 0) -> float:
    """Return CPI YoY percent for latest observation, optionally offset by months."""
    latest_idx = -(offset + 1)
    year_ago_idx = latest_idx - 12
    latest = float(series.iloc[latest_idx])
    year_ago = float(series.iloc[year_ago_idx]) if len(series) >= offset + 13 else float(series.iloc[0])
    return (latest / year_ago - 1) * 100 if year_ago else 0.0


def classify_fed_policy(rate_series: pd.Series, cpi_series: pd.Series, gdp_series: pd.Series, unemployment_series: pd.Series) -> dict:
    """Classify Fed policy from current inflation, growth, labor, and rate conditions."""
    latest_rate = float(rate_series.iloc[-1])
    rate_change = _series_change(rate_series, 63)

    cpi_yoy = _cpi_yoy(cpi_series)
    cpi_yoy_3m_ago = _cpi_yoy(cpi_series, 3) if len(cpi_series) >= 16 else cpi_yoy
    cpi_trend_3m = cpi_yoy - cpi_yoy_3m_ago

    latest_gdp = float(gdp_series.iloc[-1])
    prev_gdp = float(gdp_series.iloc[-2]) if len(gdp_series) >= 2 else latest_gdp
    prev2_gdp = float(gdp_series.iloc[-3]) if len(gdp_series) >= 3 else prev_gdp
    qoq_annualized = ((latest_gdp / prev_gdp) ** 4 - 1) * 100 if prev_gdp else 0.0
    previous_qoq_annualized = ((prev_gdp / prev2_gdp) ** 4 - 1) * 100 if prev2_gdp else qoq_annualized
    growth_accelerating = qoq_annualized > previous_qoq_annualized + 0.5
    growth_slowing = qoq_annualized < previous_qoq_annualized - 0.5

    unemployment = float(unemployment_series.iloc[-1])
    unemployment_change_3m = _series_change(unemployment_series, 3)
    real_policy_rate = latest_rate - cpi_yoy

    hawkish = []
    dovish = []
    if rate_change >= 0.25:
        hawkish.append("fed funds rate rising")
    elif rate_change <= -0.25:
        dovish.append("fed funds rate falling")

    if cpi_yoy >= 3.0:
        hawkish.append("inflation above 3%")
    elif cpi_yoy <= 2.6:
        dovish.append("inflation near target")

    if cpi_trend_3m >= 0.2:
        hawkish.append("inflation re-accelerating")
    elif cpi_trend_3m <= -0.2:
        dovish.append("inflation cooling")

    if growth_accelerating or qoq_annualized >= 2.0:
        hawkish.append("growth firm")
    elif growth_slowing or qoq_annualized < 1.0:
        dovish.append("growth slowing")

    if unemployment <= 4.2 and unemployment_change_3m <= 0.1:
        hawkish.append("labor market tight")
    elif unemployment >= 4.5 or unemployment_change_3m >= 0.3:
        dovish.append("labor market softening")

    if real_policy_rate >= 1.0:
        hawkish.append("real policy rate restrictive")
    elif real_policy_rate <= 0.25:
        dovish.append("real policy rate accommodative")

    score = len(hawkish) - len(dovish)
    if score >= 2:
        stance = "hawkish"
    elif score <= -2:
        stance = "dovish"
    else:
        stance = "holding"

    return {
        "value": stance,
        "rate": round(latest_rate, 2),
        "change63d": round(rate_change, 2),
        "cpiYoY": round(cpi_yoy, 1),
        "cpiTrend3m": round(cpi_trend_3m, 1),
        "qoqAnnualizedGrowth": round(qoq_annualized, 1),
        "unemployment": round(unemployment, 1),
        "unemploymentChange3m": round(unemployment_change_3m, 1),
        "realPolicyRate": round(real_policy_rate, 1),
        "score": score,
        "hawkishConditions": hawkish,
        "dovishConditions": dovish,
        "asOf": str(rate_series.index[-1].date()),
        "source": "FRED DFF/CPI/GDP/UNRATE current-conditions model",
    }


def inflation_from_cpi(series: pd.Series) -> dict:
    """Latest CPI year-over-year inflation rate."""
    latest = float(series.iloc[-1])
    year_ago = float(series.iloc[-13]) if len(series) >= 13 else float(series.iloc[0])
    yoy = (latest / year_ago - 1) * 100
    return {
        "value": round(yoy, 1),
        "index": round(latest, 3),
        "asOf": str(series.index[-1].date()),
        "source": "FRED CPIAUCSL year-over-year",
    }


def growth_from_gdp(series: pd.Series) -> dict:
    """Classify growth from real GDP annualized quarterly momentum."""
    latest = float(series.iloc[-1])
    prev = float(series.iloc[-2]) if len(series) >= 2 else latest
    prev2 = float(series.iloc[-3]) if len(series) >= 3 else prev
    qoq_ann = ((latest / prev) ** 4 - 1) * 100 if prev else 0.0
    prev_qoq_ann = ((prev / prev2) ** 4 - 1) * 100 if prev2 else qoq_ann
    if qoq_ann > prev_qoq_ann + 0.5:
        growth = "accelerating"
    elif qoq_ann < prev_qoq_ann - 0.5:
        growth = "slowing"
    else:
        growth = "stable"
    return {
        "value": growth,
        "qoqAnnualized": round(qoq_ann, 1),
        "previousQoqAnnualized": round(prev_qoq_ann, 1),
        "asOf": str(series.index[-1].date()),
        "source": "FRED GDPC1 real GDP quarterly momentum",
    }


def breadth_from_bars(bars_by_symbol: dict[str, pd.DataFrame | None], window: int = 50) -> dict:
    """Percent of the configured broad-market ETF universe above its 50-day MA."""
    total = above = 0
    members = []
    as_of = None
    for sym, bars in bars_by_symbol.items():
        if bars is None or len(bars) < window:
            continue
        close = bars["Close"].dropna()
        if len(close) < window:
            continue
        price = float(close.iloc[-1])
        ma = float(close.iloc[-window:].mean())
        is_above = price > ma
        total += 1
        above += 1 if is_above else 0
        members.append({"symbol": sym, "above": is_above, "price": round(price, 2), "ma50": round(ma, 2)})
        bar_date = str(bars.index[-1].date())
        as_of = bar_date if as_of is None else max(as_of, bar_date)
    if total == 0:
        return {"value": None, "error": "no breadth data", "members": []}
    return {
        "value": round(above / total * 100, 0),
        "above": above,
        "total": total,
        "window": window,
        "members": members,
        "asOf": as_of,
        "source": "Configured ETF universe above 50-day MA",
    }
