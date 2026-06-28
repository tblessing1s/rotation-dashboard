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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

import alpha_vantage
import config
import schwab_api

_client: schwab_api.SchwabClient | None = None
_client_lock = threading.Lock()
_mem_cache: dict[str, pd.DataFrame] = {}
# Last fetch error per symbol, so endpoints can explain a missing value instead
# of silently showing a blank.
_last_error: dict[str, str] = {}


def last_error(symbol: str) -> str | None:
    return _last_error.get(symbol.upper())

# Shared, bounded pool so batch reads fetch in parallel without spawning an
# unbounded number of provider connections (which would trip rate limits).
_FETCH_WORKERS = int(os.environ.get("DATA_FETCH_WORKERS", "8"))
_executor = ThreadPoolExecutor(max_workers=_FETCH_WORKERS, thread_name_prefix="data-fetch")
# Per-symbol locks dedupe concurrent fetches of the same symbol across requests.
_symbol_locks: dict[str, threading.Lock] = {}
_symbol_locks_guard = threading.Lock()


def _symbol_lock(symbol: str) -> threading.Lock:
    with _symbol_locks_guard:
        return _symbol_locks.setdefault(symbol, threading.Lock())


def client() -> schwab_api.SchwabClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = schwab_api.SchwabClient()
        return _client


def reset_caches() -> None:
    """Drop in-process caches — called when switching demo/live mode so the next
    reads come from the newly active store instead of the other mode's data."""
    _mem_cache.clear()
    _last_error.clear()


def _cache_path(symbol: str) -> str:
    safe = symbol.replace("^", "_idx_").replace("$", "_d_").replace("/", "_")
    return os.path.join(config.active_cache_dir(), f"{safe}.parquet")


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
    os.makedirs(config.active_cache_dir(), exist_ok=True)
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


def _fallback(symbol: str) -> pd.DataFrame | None:
    """The last good frame for a symbol when live fetch fails: parquet cache
    first, then the in-memory copy. (Never use `df or x` — a DataFrame has no
    unambiguous truth value.)"""
    cached = _read_cache(symbol)
    if cached is not None and not cached.empty:
        return cached
    return _mem_cache.get(symbol)


def get_daily(symbol: str, force: bool = False) -> pd.DataFrame | None:
    """Daily OHLCV for one symbol. Cached for the trading day; on provider
    failure falls back to the cached frame if one exists."""
    symbol = symbol.upper()
    # Demo mode is purely cache-backed (synthetic data, no providers).
    if config.demo_enabled():
        cached = _read_cache(symbol)
        return cached if (cached is not None and not cached.empty) else None
    path = _cache_path(symbol)
    if not force and _is_fresh(path):
        cached = _read_cache(symbol)
        if cached is not None and not cached.empty:
            return cached
    # Serialize fetches per symbol so concurrent requests don't all hit the
    # provider for the same name; the loser re-reads the freshly written cache.
    with _symbol_lock(symbol):
        if not force and _is_fresh(path):
            cached = _read_cache(symbol)
            if cached is not None and not cached.empty:
                return cached
        try:
            df = _fetch(symbol)
            _write_cache(symbol, df)
            _mem_cache[symbol] = df
            _last_error.pop(symbol, None)
            return df
        except Exception as e:  # noqa: BLE001 — degrade to last good data, never raise
            _last_error[symbol] = str(e)
            return _fallback(symbol)


def get_many(symbols, force: bool = False) -> dict[str, pd.DataFrame | None]:
    """Fetch many symbols in parallel over the shared pool. One symbol's failure
    never sinks the batch (get_daily degrades to cache and never raises)."""
    syms = list(dict.fromkeys(s.upper() for s in symbols))
    if not syms:
        return {}
    results = _executor.map(lambda s: (s, get_daily(s, force=force)), syms)
    return dict(results)


def prefetch(symbols, force: bool = False) -> None:
    """Warm the cache for many symbols in parallel (results discarded). Callers
    then compute from the now-warm per-symbol cache."""
    get_many(symbols, force=force)


def latest_quote(symbol: str) -> dict | None:
    """Live quote via Schwab, falling back to Alpha Vantage GLOBAL_QUOTE, then
    the last cached close. Used at execution time to capture the stock price."""
    symbol = symbol.upper()
    if config.demo_enabled():
        df = get_daily(symbol)
        if df is not None and not df.empty:
            return {"symbol": symbol, "price": float(df["Close"].iloc[-1]), "source": "demo"}
        return None
    if schwab_api.configured():
        try:
            q = client().get_quote(symbol)
            # last (intraday) -> mark -> close (off-hours / index quotes).
            price = (q or {}).get("last") or (q or {}).get("mark") or (q or {}).get("close")
            if price:
                _last_error.pop(symbol, None)
                return {"symbol": symbol, "price": price, "source": "schwab"}
        except Exception as e:  # noqa: BLE001
            _last_error[symbol] = str(e)
    if alpha_vantage.configured():
        try:
            q = alpha_vantage.global_quote(symbol)
            if q.get("last"):
                _last_error.pop(symbol, None)
                return {"symbol": symbol, "price": q["last"], "source": "alphavantage"}
        except Exception as e:  # noqa: BLE001
            _last_error[symbol] = str(e)
    df = get_daily(symbol)
    if df is not None and not df.empty:
        return {"symbol": symbol, "price": float(df["Close"].iloc[-1]), "source": "cache"}
    return None


def cache_age_hours(symbol: str) -> float | None:
    path = _cache_path(symbol.upper())
    if not os.path.exists(path):
        return None
    return round((datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600, 1)
