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
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

import alpha_vantage
import config
import fetch_budget
import schwab_api

# One client per OAuth connection (accounts.connection_id): the shared grant every
# book uses by default, plus one per account that authenticates as its own Schwab
# login. Keyed rather than singular because an access token belongs to a grant.
_clients: dict[str, schwab_api.SchwabClient] = {}
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
    """The MARKET-DATA client — bars, quotes, chains, fundamentals.

    One cached instance per connection, not one per process: a book that
    authenticates as a different Schwab login needs its own access token, and
    sharing one instance would let whichever login refreshed last answer for
    both. Books on the shared grant (the default, and every single-account
    install) all get the same instance they always had. Account calls take
    ``broker_client()`` instead — see there for why they must not fall back.
    """
    return _client_for(schwab_api.market_connection())


def _client_for(connection: str) -> schwab_api.SchwabClient:
    with _client_lock:
        existing = _clients.get(connection)
        if existing is None:
            existing = schwab_api.SchwabClient(connection=connection)
            _clients[connection] = existing
        return existing


def broker_client() -> schwab_api.SchwabClient:
    """The client for ACCOUNT calls — orders, transactions, cash, positions.

    Strictly this book's own connection, with no fallback to the shared login:
    market data is interchangeable between grants, an account is not. A book that
    authenticates separately and isn't connected yet raises here rather than
    quietly placing its order through the deployment's main login.
    """
    connection = schwab_api.active_connection()
    # Zero-arg on purpose: `configured()` already resolves the ACTIVE connection,
    # and it is the seam the suite substitutes.
    if not schwab_api.configured():
        owner = None
        try:
            import accounts
            owner = accounts.connection_owner(connection)
        except Exception:  # noqa: BLE001
            owner = None
        raise schwab_api.SchwabError(
            f"this account's own Schwab login isn't connected — connect it in "
            f"Settings → Accounts (/auth/schwab?account={owner})" if owner else
            "Schwab isn't connected — reconnect it on the Settings tab")
    # Resolved through client(): with this connection configured, the market
    # fallback is a no-op and returns the same pinned instance — so there stays
    # ONE place clients are built (and one seam the suite substitutes).
    return client()


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
    if schwab_api.market_configured():
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
            if schwab_api.market_configured():
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
    # Out of time already: serve whatever is cached rather than queueing on the
    # lock below. This is the ONE check that fixes the observed hang — three
    # requests for the same ticker used to pile up on `_symbol_lock` behind a
    # single long provider fetch, so the second and third paid the first one's
    # full retry budget before even starting their own.
    if fetch_budget.current().expired():
        _last_error[symbol] = "request deadline reached; served cached data"
        return _fallback(symbol)
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
    # `propagate` carries the caller's budget into the pool threads. A worker
    # thread starts with an EMPTY context, so without it every batch read would
    # silently revert to the patient budget and an interactive request would hang
    # in the batch path exactly as it used to in the single path.
    results = _executor.map(
        fetch_budget.propagate(lambda s: (s, get_daily(s, force=force))), syms)
    return dict(results)


def prefetch(symbols, force: bool = False) -> None:
    """Warm the cache for many symbols in parallel (results discarded). Callers
    then compute from the now-warm per-symbol cache."""
    get_many(symbols, force=force)


# ---- Short-lived quote cache ------------------------------------------------
# A LIVE quote (Schwab / Alpha Vantage) is reused for config.QUOTE_CACHE_SECONDS
# by display readers — the price strip on every tab, the position card, the
# alert sweep — so they share one request instead of each asking Schwab. Every
# request counts against the app-wide ~120/min cap that the 429s came from.
# Anything that BOOKS a price (order placement, fill capture) calls fresh_quote
# and never sees a cached value.
_quote_cache: dict[str, tuple[float, dict]] = {}
_quote_cache_lock = threading.Lock()
_quote_clock = time.monotonic


def _cached_quote(symbol: str) -> dict | None:
    ttl = float(config.QUOTE_CACHE_SECONDS or 0)
    if ttl <= 0:
        return None
    with _quote_cache_lock:
        hit = _quote_cache.get(symbol)
    if hit and _quote_clock() - hit[0] <= ttl:
        return dict(hit[1], cached=True)
    return None


def _remember_quote(symbol: str, quote: dict) -> None:
    if (quote or {}).get("source") in ("schwab", "alphavantage"):
        with _quote_cache_lock:
            _quote_cache[symbol] = (_quote_clock(), dict(quote))


def clear_quote_cache() -> None:
    with _quote_cache_lock:
        _quote_cache.clear()


def fresh_quote(symbol: str) -> dict | None:
    """latest_quote with the cache BYPASSED — the quote for anything that gets
    booked (the spot at an order, at a fill). Goes through latest_quote so a
    test that stubs it still governs the capture path."""
    with _quote_cache_lock:
        _quote_cache.pop(symbol.upper(), None)
    return latest_quote(symbol)


def latest_quote(symbol: str) -> dict | None:
    """Live quote via Schwab, falling back to Alpha Vantage GLOBAL_QUOTE, then
    the last cached close. Served from the short quote cache when a live quote
    is younger than QUOTE_CACHE_SECONDS (see fresh_quote for the booking path)."""
    symbol = symbol.upper()
    if config.demo_enabled():
        df = get_daily(symbol)
        if df is not None and not df.empty:
            return {"symbol": symbol, "price": float(df["Close"].iloc[-1]), "source": "demo"}
        return None
    hit = _cached_quote(symbol)
    if hit is not None:
        return hit
    if schwab_api.market_configured():
        try:
            q = client().get_quote(symbol)
            # last (intraday) -> mark -> close (off-hours / index quotes).
            price = (q or {}).get("last") or (q or {}).get("mark") or (q or {}).get("close")
            if price:
                _last_error.pop(symbol, None)
                _record_success("schwab_quote", symbol)
                out = {"symbol": symbol, "price": price, "source": "schwab"}
                _remember_quote(symbol, out)
                return out
        except Exception as e:  # noqa: BLE001
            _last_error[symbol] = str(e)
    if alpha_vantage.configured():
        try:
            q = alpha_vantage.global_quote(symbol)
            if q.get("last"):
                _last_error.pop(symbol, None)
                _record_success("alpha_vantage_quote", symbol)
                out = {"symbol": symbol, "price": q["last"], "source": "alphavantage"}
                _remember_quote(symbol, out)
                return out
        except Exception as e:  # noqa: BLE001
            _last_error[symbol] = str(e)
    df = get_daily(symbol)
    if df is not None and not df.empty:
        return {"symbol": symbol, "price": float(df["Close"].iloc[-1]), "source": "cache"}
    return None


def latest_quotes(symbols) -> dict[str, dict | None]:
    """Quotes for many symbols in as few requests as possible: cache hits
    first, ONE batched Schwab call for the rest (which also fills the cache),
    then the per-symbol latest_quote fallbacks for anything still missing. A
    failure on one name never blanks the others."""
    syms = list(dict.fromkeys(s.upper() for s in symbols if s))
    out: dict[str, dict | None] = {}
    missing: list[str] = []
    for s in syms:
        hit = None if config.demo_enabled() else _cached_quote(s)
        if hit is not None:
            out[s] = hit
        else:
            missing.append(s)
    if len(missing) > 1 and not config.demo_enabled() and schwab_api.market_configured():
        try:
            for s, q in live_prices(missing).items():
                if q and q.get("source") == "schwab":
                    out[s] = {"symbol": s, "price": q["price"], "source": "schwab"}
        except Exception as e:  # noqa: BLE001 — fall through to per-symbol
            for s in missing:
                _last_error[s] = str(e)
        missing = [s for s in missing if s not in out]
    for s in missing:
        try:
            out[s] = latest_quote(s)
        except Exception as e:  # noqa: BLE001 — one dead quote must not blank the rest
            _last_error[s] = str(e)
            out[s] = None
    return out


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
    if schwab_api.market_configured():
        try:
            quotes = client().get_quotes(syms)
            for s, q in quotes.items():
                price = (q or {}).get("last") or (q or {}).get("mark") or (q or {}).get("close")
                if price:
                    out[s] = {"price": float(price), "source": "schwab"}
                    remaining.discard(s)
                    _last_error.pop(s, None)
                    _record_success("schwab_quote", s)
                    _remember_quote(s, {"symbol": s, "price": float(price), "source": "schwab"})
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
