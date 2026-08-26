"""Alpha Vantage data client — daily OHLCV and quotes.

KEPT and slimmed from the prior build. Used as the fallback for price/volume
when Schwab is unavailable (the only other data source in this build). Set
ALPHAVANTAGE_API_KEY to enable.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

import fetch_budget

API_URL = "https://www.alphavantage.co/query"
USER_AGENT = "rotation-dashboard/2.0 (cfm dashboard)"


class AlphaVantageError(RuntimeError):
    """``key`` is the Alpha Vantage payload key that produced this error, when
    one did. It — not the message text — decides whether a retry is worth the
    caller's time: "Note" is the per-minute throttle and lifts on its own,
    while "Error Message" (a bad request) and "Information" (the DAILY cap)
    will say exactly the same thing on every subsequent attempt."""

    RETRYABLE_KEYS = ("Note",)

    def __init__(self, message: str, key: str | None = None):
        super().__init__(message)
        self.key = key

    @property
    def terminal(self) -> bool:
        """True when retrying cannot change the answer. A transport failure
        (``key is None``) is NOT terminal — that one really is transient."""
        return self.key is not None and self.key not in self.RETRYABLE_KEYS


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
    # Same budget the Schwab leg honours — AV is the FALLBACK, so it is reached
    # with the request's clock already part-spent. Retrying here on a budget of
    # its own is how a 60s browser timeout got blown through twice over.
    budget = fetch_budget.current()
    attempts = min(3, budget.attempts) if budget.interactive else 3
    for attempt in range(attempts):
        if attempt and budget.expired():
            raise AlphaVantageError(
                f"Alpha Vantage: request deadline reached after {attempt} attempt(s)")
        try:
            with urlopen(req, timeout=budget.cap_timeout(timeout)) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise AlphaVantageError(f"unexpected payload: {str(data)[:120]}")
            for soft in ("Error Message", "Note", "Information"):
                if soft in data:
                    raise AlphaVantageError(f"Alpha Vantage: {data[soft]}", key=soft)
            return data
        except AlphaVantageError as e:
            # Retryability is decided by WHICH key the payload carried, not by
            # matching the message text. The old check read `"Error Message" in
            # str(e)` — but the message is the key's VALUE, so it matched only
            # if Alpha Vantage's prose happened to contain the key name. Hard
            # errors were being retried right alongside throttles.
            #
            # Only "Note" (the per-minute throttle) is worth waiting out. A hard
            # "Error Message" and the DAILY-CAP "Information" are terminal: the
            # cap does not lift for the rest of the day, so sleeping and asking
            # again just spends the caller's deadline being told no twice more.
            # That is what made an exhausted free-tier key look like a hung
            # server rather than a provider saying no.
            last_err = e
            if e.terminal:
                raise
            time.sleep(budget.sleep_for(2.0 * (attempt + 1)))
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt >= attempts - 1:
                break
            time.sleep(budget.sleep_for(2.0 * (attempt + 1)))
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


def _get_csv(params: dict, timeout: int = 20) -> str:
    """Like _get but for the CSV endpoints (e.g. EARNINGS_CALENDAR). Alpha
    Vantage still returns a JSON note/error object on rate limits, so detect that
    and surface it rather than handing back a stray '{...}' as if it were CSV."""
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
            stripped = raw.lstrip()
            if stripped.startswith("{"):
                data = json.loads(stripped)
                for soft in ("Error Message", "Note", "Information"):
                    if soft in data:
                        raise AlphaVantageError(f"Alpha Vantage: {data[soft]}")
                raise AlphaVantageError(f"unexpected payload: {stripped[:120]}")
            return raw
        except AlphaVantageError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2.0 * (attempt + 1))
    raise AlphaVantageError(f"Alpha Vantage CSV request failed: {last_err}")


def earnings_calendar(symbol: str, horizon: str = "3month", timeout: int = 20) -> list[dict]:
    """Upcoming scheduled-earnings rows for one symbol (CSV endpoint).

    Each row carries at least `symbol`, `name`, `reportDate`, `fiscalDateEnding`,
    `estimate`, `currency`. Horizon is one of '3month' | '6month' | '12month'.
    """
    text = _get_csv(
        {"function": "EARNINGS_CALENDAR", "symbol": symbol.upper(), "horizon": horizon},
        timeout,
    )
    return [dict(row) for row in csv.DictReader(io.StringIO(text)) if row.get("reportDate")]


def overview(symbol: str, timeout: int = 20) -> dict:
    """Company fundamentals (function=OVERVIEW). Includes `DividendYield` as a
    decimal string (e.g. '0.0312'), or 'None'/'-' when the name pays none."""
    return _get({"function": "OVERVIEW", "symbol": symbol}, timeout)


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
