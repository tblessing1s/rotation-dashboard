"""Alpha Vantage data client — daily OHLCV and quotes.

KEPT and slimmed from the prior build. Used as the fallback for price/volume
when Schwab is unavailable (the only other data source in this build). Set
ALPHAVANTAGE_API_KEY to enable.
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

API_URL = "https://www.alphavantage.co/query"
USER_AGENT = "rotation-dashboard/2.0 (cfm dashboard)"


class AlphaVantageError(RuntimeError):
    pass


def _api_key() -> str | None:
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    return key.strip() if key and key.strip() else None


def configured() -> bool:
    return bool(_api_key())


def _get(params: dict, timeout: int = 20) -> dict:
    key = _api_key()
    if not key:
        raise AlphaVantageError("ALPHAVANTAGE_API_KEY not set")
    url = f"{API_URL}?{urlencode({**params, 'apikey': key})}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise AlphaVantageError(f"unexpected payload: {str(data)[:120]}")
            for soft in ("Error Message", "Note", "Information"):
                if soft in data:
                    raise AlphaVantageError(f"Alpha Vantage: {data[soft]}")
            return data
        except AlphaVantageError as e:
            # Rate-limit notes are retryable; hard errors are not.
            last_err = e
            if "Error Message" in str(e):
                raise
            time.sleep(2.0 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2.0 * (attempt + 1))
    raise AlphaVantageError(f"Alpha Vantage request failed: {last_err}")


def daily_bars(symbol: str, outputsize: str = "full", timeout: int = 20) -> pd.DataFrame:
    """Daily OHLCV ascending by date. `outputsize='full'` returns 20+ years;
    'compact' returns ~100 days."""
    data = _get({"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": outputsize}, timeout)
    series = data.get("Time Series (Daily)")
    if not series:
        raise AlphaVantageError(f"no daily series for {symbol}")
    rows = []
    for date, ohlcv in series.items():
        rows.append((date, _num(ohlcv.get("1. open")), _num(ohlcv.get("2. high")),
                     _num(ohlcv.get("3. low")), _num(ohlcv.get("4. close")), _num(ohlcv.get("5. volume"))))
    df = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close", "Volume"])
    df = df.dropna(subset=["Close"]).set_index(pd.to_datetime(df["date"])).drop(columns=["date"]).sort_index()
    if df.empty:
        raise AlphaVantageError(f"empty daily series for {symbol}")
    return df


def global_quote(symbol: str, timeout: int = 20) -> dict:
    """Latest price/volume snapshot for one symbol."""
    data = _get({"function": "GLOBAL_QUOTE", "symbol": symbol}, timeout)
    q = data.get("Global Quote") or {}
    if not q:
        raise AlphaVantageError(f"no quote for {symbol}")
    return {
        "symbol": symbol,
        "last": _num(q.get("05. price")),
        "volume": _num(q.get("06. volume")),
        "prevClose": _num(q.get("08. previous close")),
    }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
