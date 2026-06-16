"""Daily stock screener backed by Alpha Vantage.

Alpha Vantage has no server-side market screener, so this scans a *universe*
(a curated high-liquidity list + the day's most-active movers) and applies the
price / average-volume / ATR% filters locally with the same indicator helpers
the rest of the app uses. A ≥10M average-volume floor already reduces the whole
US market to a few hundred names, so this is effectively a full-market scan for
day-trading criteria — without scraping a site that blocks bots.

The expensive part (one daily-bars call per universe symbol) is computed in a
background thread and cached in the datastore; the request path only filters the
cached snapshot, mirroring the app's "providers are never touched at view time"
rule.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone

import config as cfg
import db
import indicators as ind
from providers import alphavantage

_SNAPSHOT_KEY = "screener_snapshot"
_build_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pure logic (unit-tested without any network)
# ---------------------------------------------------------------------------
def universe(movers: list[str] | None = None) -> list[str]:
    """De-duped scan universe: curated liquid names + entry candidates + movers."""
    syms = (
        list(getattr(cfg, "SCREENER_UNIVERSE", []))
        + list(getattr(cfg, "ENTRY_CANDIDATES", []))
        + list(movers or [])
    )
    seen, out = set(), []
    for s in syms:
        s = str(s).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def enrich(symbol: str, bars) -> dict | None:
    """Turn a symbol's daily bars into a screener row, or None if too short.

    Computes the three filter inputs in one pass: latest close (price), 20-day
    average volume, and ATR%(14). Relative volume (today vs its 20-day average)
    and the day's percent change are added as day-trading context.
    """
    if bars is None or len(bars) < 21:
        return None
    close = bars["Close"].dropna()
    if len(close) < 21:
        return None
    price = float(close.iloc[-1])
    if price <= 0:
        return None

    atr_pct = ind.atr_percent(bars["High"], bars["Low"], bars["Close"])
    avg_vol = ind.avg_volume_20d(bars["Volume"]) if "Volume" in bars else None

    last_vol = None
    if "Volume" in bars:
        vseries = bars["Volume"].dropna()
        if not vseries.empty:
            last_vol = float(vseries.iloc[-1])
    rvol = round(last_vol / avg_vol, 2) if (last_vol and avg_vol) else None

    prev = float(close.iloc[-2])
    change_pct = round((price / prev - 1) * 100, 2) if prev else None

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "atrPct": None if atr_pct is None else round(float(atr_pct), 2),
        "changePct": change_pct,
        "avgVol": None if avg_vol is None else int(avg_vol),
        "rvol": rvol,
        "sector": "",  # Alpha Vantage daily bars carry no sector tag.
        "source": "alphavantage",
    }


def filter_rows(rows: list[dict], price_min: float, price_max: float,
                vol_min: float, atr_min: float, atr_max: float,
                limit: int = 50) -> list[dict]:
    """Apply the day-trading filters and sort by ATR% descending (most volatile
    bounded names first), capped at `limit`."""
    out = []
    for r in rows:
        price, atr, avg_vol = r.get("price"), r.get("atrPct"), r.get("avgVol")
        if price is None or atr is None:
            continue
        if not (price_min <= price <= price_max):
            continue
        if not (atr_min <= atr <= atr_max):
            continue
        if avg_vol is None or avg_vol < vol_min:
            continue
        out.append(r)
    out.sort(key=lambda r: r["atrPct"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Snapshot build (network) + cache
# ---------------------------------------------------------------------------
def build_rows(symbols: list[str], fetch_bars) -> tuple[list[dict], dict]:
    """Enrich every symbol via `fetch_bars(symbol) -> DataFrame|None`.

    Per-symbol failures are collected, never raised, so one bad ticker never
    sinks the whole scan. Returns (rows, errors_by_symbol).
    """
    rows, errors = [], {}
    for sym in symbols:
        try:
            bars = fetch_bars(sym)
            row = enrich(sym, bars)
            if row is not None:
                rows.append(row)
        except Exception as e:  # noqa: BLE001 — keep scanning the rest of the universe
            errors[sym] = str(e)
    return rows, errors


def build_snapshot() -> dict:
    """Fetch movers + daily bars for the whole universe and cache the result.

    Runs off the request path (background thread / cron). Stores the enriched
    rows in the datastore under a single key so the API just filters them.
    """
    import time

    movers = alphavantage.top_movers()
    discovery = movers.get("most_actively_traded", []) + movers.get("top_gainers", [])
    symbols = universe(discovery)

    sleep_s = float(getattr(cfg, "SCREENER_FETCH_SLEEP", 0.25))

    def fetch(sym):
        bars = alphavantage.daily_bars(sym)
        if sleep_s:
            time.sleep(sleep_s)  # be gentle with the rate limit
        return bars

    rows, errors = build_rows(symbols, fetch)
    snapshot = {
        "rows": rows,
        "universeSize": len(symbols),
        "scanned": len(symbols),
        "matchedUniverse": len(rows),
        "errors": len(errors),
        "builtAt": _utcnow_iso(),
        "source": "alphavantage",
    }
    db.kv_set(_SNAPSHOT_KEY, snapshot)
    return snapshot


def cached_snapshot() -> dict | None:
    return db.kv_get(_SNAPSHOT_KEY)


def is_fresh(snapshot: dict | None) -> bool:
    """True when the snapshot was built within the cache window (default 12h).

    Average volume and ATR move once per trading day, so a half-day cache means
    at most a couple of builds per session while keeping "Run screen" instant.
    """
    if not snapshot or not snapshot.get("builtAt"):
        return False
    try:
        built = datetime.strptime(snapshot["builtAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    age_h = (datetime.now(timezone.utc) - built).total_seconds() / 3600
    return age_h <= float(getattr(cfg, "SCREENER_CACHE_HOURS", 12))


def building() -> bool:
    """True while a background build holds the lock."""
    if _build_lock.acquire(blocking=False):
        _build_lock.release()
        return False
    return True


def refresh_in_background() -> bool:
    """Kick a background build unless one is already running. Returns True when a
    new build was started."""
    if not _build_lock.acquire(blocking=False):
        return False

    def _run():
        try:
            build_snapshot()
        except Exception:  # noqa: BLE001 — log and release; the API serves stale meanwhile
            traceback.print_exc()
        finally:
            _build_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def get_snapshot(auto_refresh: bool = True) -> tuple[dict | None, bool]:
    """Return (snapshot, building). Triggers a background refresh when the cache
    is missing or stale; the (possibly stale) cache is served immediately."""
    snap = cached_snapshot()
    is_building = building()
    if auto_refresh and not is_fresh(snap) and not is_building:
        if refresh_in_background():
            is_building = True
    return snap, is_building


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
