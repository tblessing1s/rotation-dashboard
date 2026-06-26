"""Daily OHLCV access with a parquet cache.

Source order: Schwab (primary) -> Alpha Vantage (fallback). Results are cached
to parquet under DATA_DIR/cache and reused for the rest of the trading day, so
repeated API requests don't re-hit a provider. If both providers fail but a
cached frame exists, the stale frame is returned (visibly aged) rather than
nothing — the dashboard never blanks out on a transient outage.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

import pandas as pd

import alpha_vantage
import config
import schwab_api

_client: schwab_api.SchwabClient | None = None
_client_lock = threading.Lock()
_mem_cache: dict[str, pd.DataFrame] = {}


def client() -> schwab_api.SchwabClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = schwab_api.SchwabClient()
        return _client


def _cache_path(symbol: str) -> str:
    safe = symbol.replace("^", "_idx_").replace("$", "_d_").replace("/", "_")
    return os.path.join(config.CACHE_DIR, f"{safe}.parquet")


def _is_fresh(path: str, max_age_hours: int = 12) -> bool:
    if not os.path.exists(path):
        return False
    age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))
    return age < timedelta(hours=max_age_hours)


def _read_cache(symbol: str) -> pd.DataFrame | None:
    path = _cache_path(symbol)
    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path)
    except Exception:  # noqa: BLE001 — corrupt cache should never break a read
        return None


def _write_cache(symbol: str, df: pd.DataFrame) -> None:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    try:
        df.to_parquet(_cache_path(symbol))
    except Exception:  # noqa: BLE001 — cache write failures are non-fatal
        pass


def _fetch(symbol: str) -> pd.DataFrame:
    start = (datetime.now() - timedelta(days=config.HISTORY_DAYS)).strftime("%Y-%m-%d")
    errors = []
    if schwab_api.configured():
        try:
            return client().get_daily_bars(symbol, start)
        except Exception as e:  # noqa: BLE001 — fall through to the next source
            errors.append(f"schwab: {e}")
    if alpha_vantage.configured():
        try:
            return alpha_vantage.daily_bars(symbol).tail(config.HISTORY_DAYS)
        except Exception as e:  # noqa: BLE001
            errors.append(f"alphavantage: {e}")
    raise RuntimeError(f"no data source produced {symbol} ({'; '.join(errors) or 'no provider configured'})")


def get_daily(symbol: str, force: bool = False) -> pd.DataFrame | None:
    """Daily OHLCV for one symbol. Cached for the trading day; on provider
    failure falls back to the cached frame if one exists."""
    symbol = symbol.upper()
    path = _cache_path(symbol)
    if not force and _is_fresh(path):
        cached = _read_cache(symbol)
        if cached is not None and not cached.empty:
            return cached
    try:
        df = _fetch(symbol)
        _write_cache(symbol, df)
        _mem_cache[symbol] = df
        return df
    except Exception:  # noqa: BLE001
        return _read_cache(symbol) or _mem_cache.get(symbol)


def get_many(symbols, force: bool = False) -> dict[str, pd.DataFrame | None]:
    return {s.upper(): get_daily(s, force=force) for s in dict.fromkeys(symbols)}


def latest_quote(symbol: str) -> dict | None:
    """Live quote via Schwab, falling back to Alpha Vantage GLOBAL_QUOTE, then
    the last cached close. Used at execution time to capture the stock price."""
    symbol = symbol.upper()
    if schwab_api.configured():
        try:
            q = client().get_quote(symbol)
            if q and q.get("last"):
                return {"symbol": symbol, "price": q["last"], "source": "schwab"}
        except Exception:  # noqa: BLE001
            pass
    if alpha_vantage.configured():
        try:
            q = alpha_vantage.global_quote(symbol)
            if q.get("last"):
                return {"symbol": symbol, "price": q["last"], "source": "alphavantage"}
        except Exception:  # noqa: BLE001
            pass
    df = get_daily(symbol)
    if df is not None and not df.empty:
        return {"symbol": symbol, "price": float(df["Close"].iloc[-1]), "source": "cache"}
    return None


def cache_age_hours(symbol: str) -> float | None:
    path = _cache_path(symbol.upper())
    if not os.path.exists(path):
        return None
    return round((datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600, 1)
