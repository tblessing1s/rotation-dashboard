"""Level 1 macro data calculations for the rotation dashboard."""
from __future__ import annotations

import re
import time
from datetime import datetime
from html import unescape
from io import StringIO
from urllib.request import Request, urlopen

import pandas as pd

import config as cfg

_macro_mem: tuple[float, dict] | None = None


def _fetch_public_url(url: str) -> str:
    """Fetch a public no-key data page with a browser-like user agent."""
    req = Request(url, headers={"User-Agent": cfg.PUBLIC_DATA_USER_AGENT})
    with urlopen(req, timeout=12) as resp:
        return resp.read().decode("utf-8", "ignore")


def _fred_series(url: str, value_col: str) -> pd.Series:
    """Load a public FRED graph CSV as a numeric Series indexed by date."""
    csv = _fetch_public_url(url)
    df = pd.read_csv(StringIO(csv))
    df["observation_date"] = pd.to_datetime(df["observation_date"])
    vals = pd.to_numeric(df[value_col].replace(".", pd.NA), errors="coerce")
    return pd.Series(vals.to_numpy(), index=df["observation_date"]).dropna()


def _parse_finviz_sma50_breadth(page: str) -> dict:
    """Extract Finviz homepage market-breadth 'Above ... SMA50 Below ...' counts."""
    text = unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text)
    match = re.search(
        r"Above\s+([0-9]+(?:\.[0-9]+)?)%\s*\(([0-9,]+)\)\s*SMA50\s*Below\s*\(([0-9,]+)\)",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("Finviz SMA50 breadth block not found")

    percent = float(match.group(1))
    above = int(match.group(2).replace(",", ""))
    below = int(match.group(3).replace(",", ""))
    total = above + below
    if total <= 0:
        raise ValueError("Finviz SMA50 breadth count is zero")

    return {
        "value": round(percent, 0),
        "rawPercent": round(percent, 1),
        "above": above,
        "below": below,
        "total": total,
    }


def macro_breadth() -> dict:
    """Percent of Finviz-tracked stocks trading above their 50-day SMA."""
    parsed = _parse_finviz_sma50_breadth(_fetch_public_url(cfg.FINVIZ_MARKET_URL))
    return {
        **parsed,
        "source": "Finviz market breadth: stocks above 50-day SMA",
        "url": cfg.FINVIZ_MARKET_URL,
    }


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


def macro_fed_policy() -> dict:
    """Classify Fed stance from current inflation, growth, labor, and rate conditions."""
    return classify_fed_policy(
        _fred_series(cfg.FRED_DFF_URL, "DFF"),
        _fred_series(cfg.FRED_CPI_URL, "CPIAUCSL"),
        _fred_series(cfg.FRED_GDPC1_URL, "GDPC1"),
        _fred_series(cfg.FRED_UNRATE_URL, "UNRATE"),
    )


def macro_inflation() -> dict:
    """Latest CPI year-over-year inflation rate."""
    series = _fred_series(cfg.FRED_CPI_URL, "CPIAUCSL")
    latest = float(series.iloc[-1])
    year_ago = float(series.iloc[-13]) if len(series) >= 13 else float(series.iloc[0])
    yoy = (latest / year_ago - 1) * 100
    return {
        "value": round(yoy, 1),
        "index": round(latest, 3),
        "asOf": str(series.index[-1].date()),
        "source": "FRED CPIAUCSL year-over-year",
    }


def macro_growth() -> dict:
    """Classify growth from real GDP annualized quarterly momentum."""
    series = _fred_series(cfg.FRED_GDPC1_URL, "GDPC1")
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


def macro_snapshot(latest_quote) -> dict:
    """Return best-effort Level 1 macro values with field-level metadata."""
    global _macro_mem
    now = time.time()
    ttl = cfg.MACRO_CACHE_TTL_MINUTES * 60
    if _macro_mem and now - _macro_mem[0] < ttl:
        return _macro_mem[1]

    fields = {}
    errors = {}

    vix = latest_quote("^VIX")
    if vix.get("error"):
        errors["vix"] = "quote unavailable"
    else:
        fields["vix"] = {"value": vix["close"], "asOf": vix["date"], "source": "Yahoo Finance ^VIX"}

    calculators = {
        "breadth": macro_breadth,
        "fed": macro_fed_policy,
        "growth": macro_growth,
        "inflation": macro_inflation,
    }
    for key, fn in calculators.items():
        try:
            result = fn()
            if result.get("value") is None:
                errors[key] = result.get("error", "unavailable")
            else:
                fields[key] = result
        except Exception as e:
            errors[key] = str(e)

    snapshot = {
        "values": {key: meta["value"] for key, meta in fields.items()},
        "fields": fields,
        "errors": errors,
        "asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    _macro_mem = (now, snapshot)
    return snapshot
