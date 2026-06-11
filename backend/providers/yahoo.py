from __future__ import annotations

import pandas as pd
import yfinance as yf

from .base import Provider, ProviderError


class YahooProvider(Provider):
    """Last-resort fallback. Unofficial API: rate-limited and unreliable."""

    name = "yahoo"

    def get_daily_bars(self, symbol: str, start: str) -> pd.DataFrame:
        try:
            df = yf.download(symbol, start=start, progress=False, auto_adjust=False)
        except Exception as e:
            raise ProviderError(f"yahoo {symbol}: {e}") from e
        if df is None or df.empty:
            raise ProviderError(f"yahoo {symbol}: empty response")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if df.empty:
            raise ProviderError(f"yahoo {symbol}: no usable rows")
        return df
