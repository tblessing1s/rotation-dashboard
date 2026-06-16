"""
Scheduled ingestion for the rotation dashboard.

This is the only place external data enters the system. A run:
  1. fetches daily bars for the full symbol universe through the provider
     chain (best provider first, Yahoo as last resort),
  2. fetches the FRED macro series with retries + backoff,
  3. appends everything to the SQLite datastore (history is never overwritten),
  4. recomputes indicator and macro snapshots from stored data so the request
     path never computes from scratch or touches a provider.

Triggered by: the Fly cron machine hitting POST /api/ingest, the CLI
(`python cli.py ingest --now`), or a background catch-up thread when the app
wakes up with stale data.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd

import config as cfg
import db
import indicators as ind
import macro as macro_calc
import validation
from providers import build_chain
from providers.base import ProviderError, with_retries
from providers import fred

STATE_FILE = os.path.join(db.DATA_DIR, "state.json")

_run_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def watchlist_symbols() -> list[str]:
    """Symbols (and their sector proxies) from the saved entry watch list."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        return []
    out = []
    for item in state.get("entryWatchSymbols") or []:
        if isinstance(item, dict):
            for key in ("symbol", "sectorProxy"):
                val = str(item.get(key) or "").strip().upper()
                if val:
                    out.append(val)
        elif isinstance(item, str) and item.strip():
            out.append(item.strip().upper())
    return out


def universe() -> list[str]:
    syms = (
        list(cfg.QUOTE_SYMBOLS)
        + list(cfg.TRACKED)
        + [cfg.BENCHMARK]
        + list(cfg.BREADTH_SYMBOLS)
        + list(cfg.ENTRY_CANDIDATES)
        + watchlist_symbols()
    )
    return list(dict.fromkeys(s.upper() if not s.startswith("^") else s for s in syms))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_symbol(symbol: str, chain, start: str):
    """Try each provider in order; return (bars, source). Raises ProviderError."""
    errors = []
    for provider in chain:
        if not provider.supports(symbol):
            continue
        try:
            bars = with_retries(
                lambda: provider.get_daily_bars(symbol, start),
                attempts=2,
                base_delay=2.0,
                label=f"{provider.name} {symbol}",
            )
            return bars, provider.name
        except Exception as e:  # noqa: BLE001 — fall through to next provider
            errors.append(f"{provider.name}: {e}")
    raise ProviderError("; ".join(errors) or f"no provider supports {symbol}")


def ingest_bars(symbols: list[str], detail: dict) -> None:
    chain = build_chain()
    detail["providers"] = [p.name for p in chain]
    start = (datetime.now() - timedelta(days=cfg.HISTORY_DAYS)).strftime("%Y-%m-%d")
    ok, failed, written, quarantined = [], {}, 0, 0
    for symbol in symbols:
        try:
            bars, source = fetch_symbol(symbol, chain, start)
            accepted, rejected = validation.validate_bars(symbol, bars)
            for rej in rejected:
                if db.quarantine(
                    "bar", symbol, rej, rej["reason"], source,
                    dedup_key=f"bar:{symbol}:{rej['date']}:{source}",
                ):
                    quarantined += 1
                    print(f"[quarantine] {symbol} {rej['date']}: {rej['reason']}")
            written += db.append_bars(symbol, accepted, source)
            ok.append(symbol)
        except Exception as e:  # noqa: BLE001 — keep going; last good value stays current
            failed[symbol] = str(e)
            print(f"[ingest] bars {symbol} failed: {e}")
        time.sleep(0.1)  # be gentle with rate limits
    detail["bars"] = {"ok": len(ok), "failed": failed, "rowsWritten": written, "quarantined": quarantined}
    cross_check(chain, detail)


def cross_check(chain, detail: dict) -> None:
    """Compare regime-gating market inputs across two providers.

    When both Schwab and Yahoo are available, divergence on the latest common
    close beyond tolerance is flagged in the data-issues panel instead of
    silently trusting the priority source.
    """
    if len(chain) < 2:
        detail["crossCheck"] = "single provider — skipped"
        return
    primary, secondary = chain[0], chain[1]
    start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    results = {}
    for symbol in cfg.CROSS_CHECK_SYMBOLS:
        try:
            bars_a = primary.get_daily_bars(symbol, start)
            bars_b = secondary.get_daily_bars(symbol, start)
        except Exception as e:  # noqa: BLE001 — cross-check is best-effort
            results[symbol] = f"unavailable: {e}"
            continue
        closes_a = {str(pd.Timestamp(i).date()): float(v) for i, v in bars_a["Close"].items()}
        closes_b = {str(pd.Timestamp(i).date()): float(v) for i, v in bars_b["Close"].items()}
        common = sorted(closes_a.keys() & closes_b.keys())
        if not common:
            results[symbol] = "no common dates"
            continue
        date = common[-1]
        close_a, close_b = closes_a[date], closes_b[date]
        tol = cfg.CROSS_CHECK_TOLERANCE_PER_SYMBOL.get(symbol, cfg.CROSS_CHECK_TOLERANCE)
        diff = abs(close_a - close_b) / max(close_a, close_b)
        results[symbol] = {"date": date, primary.name: close_a, secondary.name: close_b,
                           "diffPct": round(diff * 100, 2), "tolerancePct": tol * 100}
        if diff > tol:
            db.quarantine(
                "divergence", symbol, results[symbol],
                f"{primary.name} {close_a} vs {secondary.name} {close_b} on {date}"
                f" diverge {diff * 100:.2f}% (tolerance {tol * 100:.0f}%)",
                f"{primary.name}/{secondary.name}",
                dedup_key=f"divergence:{symbol}:{date}",
            )
    detail["crossCheck"] = results


def fetch_macro_series(series_id: str):
    """Fetch one Level 1 macro series, FRED first with an Alpha Vantage fallback.

    Returns (series, source). FRED is the primary source; when it fails (the
    keyless graph CSV increasingly 403s) and an Alpha Vantage key is configured,
    the same series is pulled from Alpha Vantage's economic-indicator endpoints
    at a matching cadence so the regime gate keeps filling. Raises ProviderError
    only when *both* sources fail.
    """
    from providers import alphavantage

    try:
        return fred.fetch_series(series_id), "fred"
    except Exception as fred_err:  # noqa: BLE001 — fall through to the AV fallback
        if not alphavantage.configured():
            raise
        try:
            return alphavantage.economic_series(series_id), "alphavantage"
        except Exception as av_err:  # noqa: BLE001
            raise ProviderError(
                f"FRED failed ({fred_err}); Alpha Vantage fallback failed ({av_err})"
            ) from av_err


def ingest_fred(detail: dict) -> None:
    ok, failed, written, fallback = [], {}, 0, []
    for series_id in cfg.FRED_SERIES:
        try:
            series, source = fetch_macro_series(series_id)
            written += db.append_macro_series(series_id, series, source)
            ok.append(series_id)
            if source != "fred":
                fallback.append(series_id)
                print(f"[ingest] FRED {series_id} via {source} fallback")
        except Exception as e:  # noqa: BLE001
            failed[series_id] = str(e)
            print(f"[ingest] macro {series_id} failed: {e}")
    detail["fred"] = {"ok": ok, "failed": failed, "rowsWritten": written, "fallback": fallback}


# ---------------------------------------------------------------------------
# Snapshot computation (from stored data only)
# ---------------------------------------------------------------------------
def compute_indicator_snapshots(symbols: list[str], detail: dict) -> None:
    spy = db.get_bars(cfg.BENCHMARK)
    count = 0
    for symbol in symbols:
        bars = db.get_bars(symbol)
        if bars is None:
            continue
        payload = ind.compute_all(bars, spy, cfg)
        payload["source"] = bars.attrs.get("source")
        payload["fetchedAt"] = bars.attrs.get("fetched_at")
        db.save_snapshot("indicators", symbol, payload, payload.get("asOf"))
        count += 1
    detail["indicatorSnapshots"] = count


def compute_macro_snapshot(detail: dict) -> None:
    fields = {}
    errors = {}

    vix = db.latest_bar(cfg.VIX_PROXY_SYMBOL)
    if vix is None:
        errors["vix"] = f"no stored {cfg.VIX_PROXY_SYMBOL} bar"
    else:
        fields["vix"] = {
            "value": round(vix["close"], 2),
            "asOf": vix["date"],
            "source": f"{vix['source']} {cfg.VIX_PROXY_SYMBOL}",
            "fetchedAt": vix["fetched_at"],
        }

    breadth_bars = {sym: db.get_bars(sym) for sym in cfg.BREADTH_SYMBOLS}
    breadth = macro_calc.breadth_from_bars(breadth_bars, cfg.BREADTH_MA_WINDOW)
    if breadth.get("value") is None:
        errors["breadth"] = breadth.get("error", "unavailable")
    else:
        fields["breadth"] = breadth

    series = {sid: db.get_macro_series(sid) for sid in cfg.FRED_SERIES}
    missing = [sid for sid, s in series.items() if s is None or s.empty]
    fred_fetched_at = min(
        (s.attrs.get("fetched_at") or "" for s in series.values() if s is not None),
        default=None,
    )

    if missing:
        for key in ("fed", "growth", "inflation"):
            errors[key] = f"missing FRED series: {', '.join(missing)}"
    else:
        calculators = {
            "fed": lambda: macro_calc.classify_fed_policy(
                series["DFF"], series["CPIAUCSL"], series["GDPC1"], series["UNRATE"]
            ),
            "growth": lambda: macro_calc.growth_from_gdp(series["GDPC1"]),
            "inflation": lambda: macro_calc.inflation_from_cpi(series["CPIAUCSL"]),
        }
        for key, fn in calculators.items():
            try:
                result = fn()
                if result.get("value") is None:
                    errors[key] = result.get("error", "unavailable")
                else:
                    result["fetchedAt"] = fred_fetched_at
                    fields[key] = result
            except Exception as e:  # noqa: BLE001
                errors[key] = str(e)

    payload = {
        "values": {key: meta["value"] for key, meta in fields.items()},
        "fields": fields,
        "errors": errors,
    }
    as_of = max((meta.get("asOf") or "" for meta in fields.values()), default=None) or None
    db.save_snapshot("macro", "macro", payload, as_of)
    detail["macro"] = {"fields": sorted(fields), "errors": errors}


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------
def run(trigger: str = "manual", symbols: list[str] | None = None) -> dict:
    """Execute one ingestion cycle. Returns the run detail dict."""
    if not _run_lock.acquire(blocking=False):
        return {"skipped": "ingestion already running"}
    try:
        run_id = db.start_ingest_run(trigger)
        detail: dict = {}
        status = "ok"
        try:
            targets = symbols if symbols else universe()
            ingest_bars(targets, detail)
            if symbols is None:
                ingest_fred(detail)
            compute_indicator_snapshots(targets, detail)
            compute_macro_snapshot(detail)
            _prewarm_screener(symbols, detail)
            if detail.get("bars", {}).get("failed") or detail.get("fred", {}).get("failed"):
                status = "partial"
        except Exception as e:  # noqa: BLE001
            status = "error"
            detail["error"] = str(e)
            detail["traceback"] = traceback.format_exc()
        db.finish_ingest_run(run_id, status, detail)
        return {"status": status, **detail}
    finally:
        _run_lock.release()


def _prewarm_screener(symbols: list[str] | None, detail: dict) -> None:
    """Rebuild the daily-screener universe snapshot after a full ingest cycle so
    the first scan of the session is instant instead of reporting "building".

    Only runs on full runs (not targeted symbol re-ingests) and only when an
    Alpha Vantage key is configured; otherwise the screener simply stays lazy.
    """
    if symbols is not None:
        return
    try:
        from providers import alphavantage
        import screener

        if alphavantage.configured() and screener.refresh_in_background():
            detail["screenerPrewarm"] = "started"
    except Exception as e:  # noqa: BLE001 — pre-warming is best-effort
        detail["screenerPrewarm"] = f"skipped: {e}"


def run_in_background(trigger: str, symbols: list[str] | None = None) -> None:
    threading.Thread(target=run, args=(trigger, symbols), daemon=True).start()


def is_stale(max_age_hours: float | None = None) -> bool:
    """True when the newest successful ingest is older than the threshold."""
    max_age_hours = max_age_hours if max_age_hours is not None else cfg.INGEST_STALE_AFTER_HOURS
    last = db.last_successful_ingest()
    if last is None or not last.get("finished_at"):
        return True
    try:
        finished = datetime.strptime(last["finished_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True
    return (datetime.utcnow() - finished) > timedelta(hours=max_age_hours)
