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

    def get_intraday_bars(self, symbol: str, start: str, end: str,
                          interval_min: int = 5, extended_hours: bool = False) -> pd.DataFrame:
        """Intraday fallback. Yahoo only serves ~60 days of 5-minute history and
        is rate-limited; the backtester prefers Schwab and uses this when Schwab
        is unavailable. `end` is made inclusive (Yahoo's end is exclusive)."""
        interval = f"{interval_min}m"
        end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            df = yf.download(
                symbol, start=start, end=end_excl, interval=interval,
                progress=False, auto_adjust=False, prepost=extended_hours,
            )
        except Exception as e:
            raise ProviderError(f"yahoo {symbol} intraday: {e}") from e
        if df is None or df.empty:
            raise ProviderError(f"yahoo {symbol} intraday: empty response")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if df.empty:
            raise ProviderError(f"yahoo {symbol} intraday: no usable rows")
        idx = pd.DatetimeIndex(pd.to_datetime(df.index))
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        df.index = idx.tz_convert("America/New_York")
        return df
