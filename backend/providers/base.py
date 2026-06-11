from __future__ import annotations

import time

import pandas as pd


class ProviderError(Exception):
    """Raised by providers on fetch failure so the chain can fall through."""


class Provider:
    name = "base"

    def get_daily_bars(self, symbol: str, start: str) -> pd.DataFrame:
        """Daily OHLCV from `start` (YYYY-MM-DD) to now.

        Returns a DataFrame with columns Open/High/Low/Close/Volume indexed by
        date. Raises ProviderError on failure or empty data.
        """
        raise NotImplementedError

    def supports(self, symbol: str) -> bool:
        return True


def with_retries(fn, attempts: int = 3, base_delay: float = 2.0, label: str = ""):
    """Run fn() with exponential backoff. Re-raises the last error."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — providers raise varied errors
            last = e
            if i < attempts - 1:
                delay = base_delay * (2**i)
                print(f"[retry] {label or fn} failed ({e}); retrying in {delay:.0f}s")
                time.sleep(delay)
    raise last
