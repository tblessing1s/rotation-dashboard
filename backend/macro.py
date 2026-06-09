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


def macro_fed_policy() -> dict:
    """Classify Fed stance from recent effective fed funds rate direction."""
    series = _fred_series(cfg.FRED_DFF_URL, "DFF")
    latest = float(series.iloc[-1])
    prior = float(series.iloc[-64]) if len(series) >= 64 else float(series.iloc[0])
    change = latest - prior
    if change >= 0.25:
        stance = "hawkish"
    elif change <= -0.25:
        stance = "dovish"
    else:
        stance = "holding"
    return {
        "value": stance,
        "rate": round(latest, 2),
        "change63d": round(change, 2),
        "asOf": str(series.index[-1].date()),
        "source": "FRED DFF, 63-trading-day rate change",
    }


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
