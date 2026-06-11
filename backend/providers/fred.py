from __future__ import annotations

from io import StringIO
from urllib.request import Request, urlopen

import pandas as pd

from .base import ProviderError, with_retries

USER_AGENT = "rotation-dashboard/1.0 (personal trading dashboard)"


def fetch_series(series_id: str, timeout: int = 20) -> pd.Series:
    """Fetch a FRED series via the no-key graph CSV, with retries + backoff."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

    def _fetch():
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")

    try:
        csv = with_retries(_fetch, attempts=4, base_delay=2.0, label=f"FRED {series_id}")
    except Exception as e:
        raise ProviderError(f"FRED {series_id}: {e}") from e

    df = pd.read_csv(StringIO(csv))
    if "observation_date" not in df.columns or series_id not in df.columns:
        raise ProviderError(f"FRED {series_id}: unexpected CSV columns {list(df.columns)}")
    df["observation_date"] = pd.to_datetime(df["observation_date"])
    vals = pd.to_numeric(df[series_id].replace(".", pd.NA), errors="coerce")
    series = pd.Series(vals.to_numpy(), index=df["observation_date"]).dropna()
    if series.empty:
        raise ProviderError(f"FRED {series_id}: no observations")
    return series
