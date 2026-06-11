from __future__ import annotations

import json
import os
from io import StringIO
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .base import ProviderError, with_retries

USER_AGENT = "rotation-dashboard/1.0 (personal trading dashboard)"

API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def _get(url: str, timeout: int) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _series_from_pairs(series_id: str, dates, values) -> pd.Series:
    idx = pd.to_datetime(pd.Series(dates))
    vals = pd.to_numeric(pd.Series(values).replace(".", pd.NA), errors="coerce")
    series = pd.Series(vals.to_numpy(), index=idx).dropna()
    if series.empty:
        raise ProviderError(f"FRED {series_id}: no observations")
    return series


def _fetch_api(series_id: str, api_key: str, timeout: int) -> pd.Series:
    """Fetch via the official keyed FRED API (JSON)."""
    params = urlencode({"series_id": series_id, "api_key": api_key, "file_type": "json"})
    url = f"{API_URL}?{params}"
    raw = with_retries(
        lambda: _get(url, timeout), attempts=4, base_delay=2.0, label=f"FRED API {series_id}"
    )
    try:
        obs = json.loads(raw).get("observations", [])
    except (ValueError, AttributeError) as e:
        raise ProviderError(f"FRED {series_id}: bad API response ({e})") from e
    return _series_from_pairs(series_id, [o["date"] for o in obs], [o["value"] for o in obs])


def _fetch_csv(series_id: str, timeout: int) -> pd.Series:
    """Fetch via the keyless graph CSV (no API key)."""
    url = f"{CSV_URL}?id={series_id}"
    csv = with_retries(
        lambda: _get(url, timeout), attempts=4, base_delay=2.0, label=f"FRED CSV {series_id}"
    )
    df = pd.read_csv(StringIO(csv))
    if "observation_date" not in df.columns or series_id not in df.columns:
        raise ProviderError(f"FRED {series_id}: unexpected CSV columns {list(df.columns)}")
    return _series_from_pairs(series_id, df["observation_date"], df[series_id])


def fetch_series(series_id: str, timeout: int = 20) -> pd.Series:
    """Fetch a FRED series, preferring the keyed API with a keyless CSV fallback.

    Uses the official FRED API when FRED_API_KEY is set (the keyless graph CSV
    increasingly returns HTTP 403 to programmatic requests). Falls back to the
    CSV if the API call fails, and uses the CSV directly when no key is set.
    """
    api_key = os.environ.get("FRED_API_KEY")
    errors = []

    if api_key:
        try:
            return _fetch_api(series_id, api_key, timeout)
        except Exception as e:  # noqa: BLE001 — fall through to the CSV fallback
            errors.append(f"api: {e}")
            print(f"[fred] {series_id} API fetch failed ({e}); trying keyless CSV")

    try:
        return _fetch_csv(series_id, timeout)
    except Exception as e:  # noqa: BLE001
        errors.append(f"csv: {e}")

    hint = "" if api_key else " (set FRED_API_KEY for the keyed API)"
    raise ProviderError(f"FRED {series_id}: {'; '.join(errors)}{hint}")
