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
# Last SUCCESSFUL fetch per source (schwab_bars / alpha_vantage_bars /
# schwab_quote / alpha_vantage_quote), so silent data failures are visible:
# a source that hasn't succeeded all day on a market day is a red flag even
# when the cache is quietly serving stale frames.
_last_success: dict[str, dict] = {}
_fallback_events = 0  # times Alpha Vantage had to cover for Schwab (bars)


def last_error(symbol: str) -> str | None:
    return _last_error.get(symbol.upper())


def _record_success(source: str, symbol: str) -> None:
    _last_success[source] = {"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                             "symbol": symbol}


def health() -> dict:
    """Per-source last-success timestamps + recent errors for the health panel."""
    return {
        "sources": dict(_last_success),
        "fallback_events": _fallback_events,
        "recent_errors": dict(list(_last_error.items())[-10:]),
    }

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


def _cached_frame(symbol: str) -> pd.DataFrame | None:
    """Warm-cache read that memoizes the parsed frame in ``_mem_cache``.

    The parquet on disk is the source of truth, but parsing it is not free: a
    full-universe sweep calls ``get_daily`` ~4-5x per ticker (score row, then the
    entry gate re-reading SPY + the sector ETF + the ticker again), so a ~530-name
    universe would otherwise re-read and re-parse thousands of parquet files from
    disk on every scan even when nothing has changed. Serving the already-parsed
    frame from memory turns those repeat reads into a dict lookup. Correctness is
    unchanged: ``_mem_cache`` is only ever populated from the same parquet (here or
    on a live fetch that also wrote it), and callers only reach this helper while
    the parquet is still fresh — once it ages out, ``get_daily`` bypasses the cache
    and refetches, refreshing memory in lockstep with disk."""
    df = _mem_cache.get(symbol)
    if df is not None and not df.empty:
        return df
    df = _read_cache(symbol)
    if df is not None and not df.empty:
        _mem_cache[symbol] = df
    return df


def _write_cache(symbol: str, df: pd.DataFrame) -> None:
    os.makedirs(config.active_cache_dir(), exist_ok=True)
    try:
        df.to_parquet(_cache_path(symbol))
    except Exception:  # noqa: BLE001 — cache write failures are non-fatal
        pass


def _fetch(symbol: str) -> pd.DataFrame:
    global _fallback_events
    start = (datetime.now() - timedelta(days=config.HISTORY_DAYS)).strftime("%Y-%m-%d")
    errors = []
    if schwab_api.configured():
        try:
            df = client().get_daily_bars(symbol, start)
            _record_success("schwab_bars", symbol)
            return df
        except Exception as e:  # noqa: BLE001 — fall through to the next source
            errors.append(f"schwab: {e}")
    if alpha_vantage.configured():
        try:
            # Align to the SAME calendar window as the Schwab path (startDate =
            # `start`) instead of a fixed row-count tail: Schwab measures calendar
            # days and AV measures trading rows, so a tail(HISTORY_DAYS) would hand
            # back a different (deeper) history than Schwab for the same symbol —
            # a different earliest bar, which would make the classifier's
            # prefix-causal replay depend on which provider served the frame. AV
            # full is 20+ yrs; slicing from `start` keeps both keyed off one window.
            df = alpha_vantage.daily_bars(symbol).loc[start:]
            _record_success("alpha_vantage_bars", symbol)
            if schwab_api.configured():
                _fallback_events += 1
            return df
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
        return _cached_frame(symbol)
    path = _cache_path(symbol)
    if not force and _is_fresh(path):
        cached = _cached_frame(symbol)
        if cached is not None and not cached.empty:
            return cached
    # Serialize fetches per symbol so concurrent requests don't all hit the
    # provider for the same name; the loser re-reads the freshly written cache.
    with _symbol_lock(symbol):
        if not force and _is_fresh(path):
            cached = _cached_frame(symbol)
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
                _record_success("schwab_quote", symbol)
                return {"symbol": symbol, "price": price, "source": "schwab"}
        except Exception as e:  # noqa: BLE001
            _last_error[symbol] = str(e)
    if alpha_vantage.configured():
        try:
            q = alpha_vantage.global_quote(symbol)
            if q.get("last"):
                _last_error.pop(symbol, None)
                _record_success("alpha_vantage_quote", symbol)
                return {"symbol": symbol, "price": q["last"], "source": "alphavantage"}
        except Exception as e:  # noqa: BLE001
            _last_error[symbol] = str(e)
    df = get_daily(symbol)
    if df is not None and not df.empty:
        return {"symbol": symbol, "price": float(df["Close"].iloc[-1]), "source": "cache"}
    return None


def live_price(symbol: str) -> float | None:
    """The current tradeable price as a float, or None. Thin wrapper over
    latest_quote (Schwab last/mark -> Alpha Vantage -> cached close), so callers
    that only need the number don't unpack the quote dict. Off-hours / no
    provider it degrades to the last cached close, same as latest_quote."""
    q = latest_quote(symbol)
    price = (q or {}).get("price")
    return float(price) if price is not None else None


def live_prices(symbols) -> dict[str, dict]:
    """Live prices for many symbols, resolved in as few provider calls as
    possible: ONE Schwab batch-quotes call, then Alpha Vantage GLOBAL_QUOTE
    per-symbol for whatever Schwab didn't cover, then the last cached close as a
    last resort. Returns {symbol: {"price": float, "source": str}} for every
    symbol that resolved (missing symbols are simply absent).

    Unlike daily bars — which are end-of-day and so lag the live market intraday
    — a quote carries the CURRENT price (Schwab last/mark). This is what the Scan
    refresh overlays onto a row so the displayed price is actually live."""
    syms = list(dict.fromkeys(s.upper() for s in symbols if s))
    out: dict[str, dict] = {}
    if not syms:
        return out
    # Demo mode has no providers — serve the synthetic cached close.
    if config.demo_enabled():
        for s in syms:
            df = get_daily(s)
            if df is not None and not df.empty:
                out[s] = {"price": float(df["Close"].iloc[-1]), "source": "demo"}
        return out

    remaining = set(syms)
    if schwab_api.configured():
        try:
            quotes = client().get_quotes(syms)
            for s, q in quotes.items():
                price = (q or {}).get("last") or (q or {}).get("mark") or (q or {}).get("close")
                if price:
                    out[s] = {"price": float(price), "source": "schwab"}
                    remaining.discard(s)
                    _last_error.pop(s, None)
                    _record_success("schwab_quote", s)
        except Exception as e:  # noqa: BLE001 — degrade to the per-symbol fallbacks
            for s in syms:
                _last_error[s] = str(e)

    if remaining and alpha_vantage.configured():
        for s in list(remaining):
            try:
                q = alpha_vantage.global_quote(s)
                if q.get("last"):
                    out[s] = {"price": float(q["last"]), "source": "alphavantage"}
                    remaining.discard(s)
                    _last_error.pop(s, None)
                    _record_success("alpha_vantage_quote", s)
            except Exception as e:  # noqa: BLE001
                _last_error[s] = str(e)

    # Last resort — the cached daily close (visibly labelled, so a stale
    # provider is obvious in the UI instead of masquerading as a live quote).
    for s in list(remaining):
        df = get_daily(s)
        if df is not None and not df.empty:
            out[s] = {"price": float(df["Close"].iloc[-1]), "source": "cache"}
    return out


def cache_age_hours(symbol: str) -> float | None:
    path = _cache_path(symbol.upper())
    if not os.path.exists(path):
        return None
    return round((datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600, 1)
