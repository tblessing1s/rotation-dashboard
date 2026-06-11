"""Pluggable market-data providers.

Each provider implements `get_daily_bars(symbol, start)` returning a DataFrame
with columns Open/High/Low/Close/Volume indexed by date, and has a `name` used
as the `source` tag on every stored row. `build_chain()` returns providers in
priority order based on available credentials.
"""
from __future__ import annotations

from .base import Provider, ProviderError
from .yahoo import YahooProvider


def build_chain() -> list[Provider]:
    chain: list[Provider] = []
    try:
        from .schwab import SchwabProvider

        if SchwabProvider.configured():
            chain.append(SchwabProvider())
    except ImportError:
        pass
    chain.append(YahooProvider())
    return chain
