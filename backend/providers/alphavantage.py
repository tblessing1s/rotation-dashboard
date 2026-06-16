"""Alpha Vantage provider — daily bars, top movers, and economic indicators.

Authenticated JSON API (no scraping), so it is a durable replacement for the
Finviz screen path (finviz.com 403s datacenter/bot traffic) and a resilient
fallback for the FRED macro series (the keyless FRED CSV increasingly 403s too).

Set ALPHAVANTAGE_API_KEY to enable. Like every other provider, fetching only
happens inside scheduled ingestion / the screener's background refresh — never
on the request path.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .base import ProviderError, with_retries

API_URL = "https://www.alphavantage.co/query"
USER_AGENT = "rotation-dashboard/1.0 (personal trading dashboard)"

# FRED series id -> (Alpha Vantage function, request params). Intervals are
# chosen to match the cadence the macro calculators assume: the Fed-policy model
# steps the funds rate 63 *observations* back (≈3 months of daily data), so the
# funds rate must be daily; CPI/unemployment are monthly and GDP quarterly.
_ECON_SERIES = {
    "DFF": ("FEDERAL_FUNDS_RATE", {"interval": "daily"}),
    "CPIAUCSL": ("CPI", {"interval": "monthly"}),
    "GDPC1": ("REAL_GDP", {"interval": "quarterly"}),
    "UNRATE": ("UNEMPLOYMENT", {}),
}


def configured() -> bool:
    return bool(_api_key())


def _api_key() -> str | None:
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    return key.strip() if key and key.strip() else None


def _get(params: dict, timeout: int) -> dict:
    """Call the Alpha Vantage query endpoint and return parsed JSON.

    Raises ProviderError on transport failure, a non-JSON body, or any of Alpha
    Vantage's soft-error envelopes (rate-limit "Note"/"Information", "Error
    Message"), which arrive as HTTP 200 with no data.
    """
    key = _api_key()
    if not key:
        raise ProviderError("ALPHAVANTAGE_API_KEY not set")
    url = f"{API_URL}?{urlencode({**params, 'apikey': key})}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001 — network/HTTP errors all become ProviderError
        raise ProviderError(f"Alpha Vantage request failed: {e}") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ProviderError(f"Alpha Vantage returned non-JSON: {raw[:120]}") from e
    if not isinstance(data, dict):
        raise ProviderError(f"Alpha Vantage unexpected payload: {str(data)[:120]}")
    for soft_err in ("Error Message", "Note", "Information"):
        if soft_err in data:
            raise ProviderError(f"Alpha Vantage: {data[soft_err]}")
    return data


def daily_bars(symbol: str, outputsize: str = "compact", timeout: int = 20) -> pd.DataFrame:
    """Daily OHLCV for `symbol`, ascending by date.

    `outputsize="compact"` returns ~100 trading days — plenty for a 20-day
    average volume and a 14-day ATR — and keeps each call light. Uses the
    non-adjusted TIME_SERIES_DAILY endpoint (available without a premium tier;
    raw OHLCV is all the screener needs).
    """
    data = with_retries(
        lambda: _get(
            {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": outputsize},
            timeout,
        ),
        attempts=3, base_delay=2.0, label=f"AV daily {symbol}",
    )
    series = data.get("Time Series (Daily)")
    if not series:
        raise ProviderError(f"Alpha Vantage: no daily series for {symbol}")
    rows = []
    for date, ohlcv in series.items():
        rows.append((
            date,
            _num(ohlcv.get("1. open")), _num(ohlcv.get("2. high")),
            _num(ohlcv.get("3. low")), _num(ohlcv.get("4. close")),
            _num(ohlcv.get("5. volume")),
        ))
    df = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close", "Volume"])
    df = df.dropna(subset=["Close"]).set_index(pd.to_datetime(df["date"])).drop(columns=["date"])
    df = df.sort_index()
    if df.empty:
        raise ProviderError(f"Alpha Vantage: empty daily series for {symbol}")
    return df


def top_movers(timeout: int = 20) -> dict:
    """Return Alpha Vantage's TOP_GAINERS_LOSERS lists as ticker lists.

    The `most_actively_traded` list is the day's highest-volume US names — the
    discovery layer that surfaces in-range movers a static universe would miss.
    Best-effort: returns empty lists on failure so a screener build still runs
    off its curated universe.
    """
    try:
        data = _get({"function": "TOP_GAINERS_LOSERS"}, timeout)
    except ProviderError:
        return {"most_actively_traded": [], "top_gainers": [], "top_losers": []}
    out = {}
    for key in ("most_actively_traded", "top_gainers", "top_losers"):
        out[key] = [
            str(row.get("ticker", "")).strip().upper()
            for row in (data.get(key) or [])
            if row.get("ticker")
        ]
    return out


def economic_series(series_id: str, timeout: int = 20) -> pd.Series:
    """Fetch a FRED-equivalent macro series from Alpha Vantage.

    Maps the dashboard's FRED series ids (DFF/CPIAUCSL/GDPC1/UNRATE) to the
    matching Alpha Vantage economic-indicator function at a compatible interval,
    returning a date-indexed float Series shaped exactly like fred.fetch_series
    so it can fall in behind FRED transparently.
    """
    if series_id not in _ECON_SERIES:
        raise ProviderError(f"Alpha Vantage has no mapping for FRED series {series_id}")
    function, extra = _ECON_SERIES[series_id]
    data = with_retries(
        lambda: _get({"function": function, **extra}, timeout),
        attempts=3, base_delay=2.0, label=f"AV econ {series_id}",
    )
    points = data.get("data")
    if not points:
        raise ProviderError(f"Alpha Vantage {function}: no data")
    idx = pd.to_datetime([p["date"] for p in points])
    vals = pd.to_numeric(pd.Series([p.get("value") for p in points]).replace(".", pd.NA),
                         errors="coerce")
    series = pd.Series(vals.to_numpy(), index=idx).dropna().sort_index()
    if series.empty:
        raise ProviderError(f"Alpha Vantage {function}: no numeric observations")
    return series


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
