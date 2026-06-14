"""
Service layer that wires the pure backtest engine (backtest.py) to the
datastore and the provider chain.

Responsibilities
----------------
* Build datastore-backed loaders for the engine (intraday + daily bars).
* Default each ticker's sector proxy from config so SPY/sector context works
  out of the box.
* Report intraday coverage gaps and, on request, backfill them from Schwab
  (Yahoo as last resort) — the engine itself never contacts a provider.
* Persist named backtest configurations in the `kv` table.

The request path stays datastore-only unless the caller explicitly asks to
backfill (mirrors the dashboard's "providers are only touched on purpose" rule).
"""
from __future__ import annotations

import time

import config as cfg
import db
import backtest as engine
from providers import build_chain
from providers.base import ProviderError, with_retries

_CONFIG_KV_KEY = "backtest_configs"


# ---------------------------------------------------------------------------
# Datastore-backed loaders
# ---------------------------------------------------------------------------
def _make_loaders(interval_min: int):
    daily_cache: dict[str, object] = {}

    def get_intraday(symbol, date_str, interval=interval_min):
        return db.get_intraday_bars(symbol, date_str, date_str, interval)

    def get_daily(symbol):
        if symbol not in daily_cache:
            daily_cache[symbol] = db.get_bars(symbol)
        return daily_cache[symbol]

    return get_intraday, get_daily


def _apply_default_sector_map(config: dict) -> dict:
    """Fill each ticker's sector proxy from config.ENTRY_CANDIDATE_PROXY unless
    the caller supplied one. Lets SPY/sector skip conditions work without the
    user hand-mapping every symbol."""
    provided = {k.upper(): v for k, v in (config.get("sector_map") or {}).items()}
    proxies = getattr(cfg, "ENTRY_CANDIDATE_PROXY", {})
    for ticker in config.get("tickers", []):
        if ticker not in provided and ticker in proxies:
            provided[ticker] = proxies[ticker]
    config["sector_map"] = provided
    return config


def _context_symbols(config: dict) -> list[str]:
    """Every symbol the run reads intraday: tickers + sector proxies + SPY."""
    syms = list(config.get("tickers", []))
    syms += [v for v in (config.get("sector_map") or {}).values() if v]
    syms.append(getattr(cfg, "BENCHMARK", "SPY"))
    return list(dict.fromkeys(s for s in syms if s))


# ---------------------------------------------------------------------------
# Coverage + backfill
# ---------------------------------------------------------------------------
def coverage_report(config: dict) -> dict:
    """Which (symbol, date) intraday sessions are missing from the datastore."""
    interval = int(config.get("interval_min", 5))
    start = config["date_range"]["start"]
    end = config["date_range"]["end"]
    dates = engine._session_dates(start, end)
    missing: list[dict] = []
    per_symbol = {}
    for sym in _context_symbols(config):
        present = db.intraday_coverage(sym, start, end, interval)
        gaps = [d for d in dates if d not in present]
        per_symbol[sym] = {"sessions": len(dates), "present": len(dates) - len(gaps),
                           "missing": len(gaps)}
        for d in gaps:
            missing.append({"symbol": sym, "date": d})
    return {"sessions": len(dates), "missing": missing, "perSymbol": per_symbol,
            "complete": not missing}


def backfill(symbols: list[str], start: str, end: str, interval_min: int = 5) -> dict:
    """Pull intraday bars for `symbols` over [start, end] and store them.

    Tries each provider in priority order (Schwab first, Yahoo last) and writes
    accepted candles append-only. Returns a per-symbol status so the UI can show
    what was filled and what failed (e.g. Schwab token expired)."""
    chain = [p for p in build_chain()]
    results = {}
    total_written = 0
    for sym in symbols:
        errors = []
        wrote = 0
        source = None
        for provider in chain:
            try:
                bars = with_retries(
                    lambda: provider.get_intraday_bars(sym, start, end, interval_min),
                    attempts=2, base_delay=2.0, label=f"{provider.name} {sym} intraday",
                )
                wrote = db.append_intraday_bars(sym, bars, provider.name, interval_min)
                source = provider.name
                break
            except NotImplementedError:
                continue
            except Exception as e:  # noqa: BLE001 — fall through to the next provider
                errors.append(f"{provider.name}: {e}")
        total_written += wrote
        results[sym] = {"rowsWritten": wrote, "source": source,
                        "error": None if source else ("; ".join(errors) or "no intraday provider")}
        time.sleep(0.1)  # be gentle with rate limits
    ok = any(r["source"] for r in results.values())
    return {"ok": ok, "rowsWritten": total_written, "perSymbol": results,
            "providers": [p.name for p in chain]}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def run(raw_config: dict, auto_backfill: bool = False) -> dict:
    """Validate, optionally backfill missing sessions, then run the backtest.

    Returns ``{ok, errors?, result?, coverage, backfill?}``. With
    ``auto_backfill`` the missing sessions are pulled from the provider chain
    before the run; otherwise a coverage gap is reported (not an error) so the
    UI can offer a one-click backfill.
    """
    config, errors = engine.validate_config(raw_config)
    if errors:
        return {"ok": False, "errors": errors}
    config = _apply_default_sector_map(config)

    backfill_result = None
    coverage = coverage_report(config)
    if auto_backfill and not coverage["complete"]:
        symbols = sorted({m["symbol"] for m in coverage["missing"]})
        backfill_result = backfill(symbols, config["date_range"]["start"],
                                   config["date_range"]["end"], int(config.get("interval_min", 5)))
        coverage = coverage_report(config)

    get_intraday, get_daily = _make_loaders(int(config.get("interval_min", 5)))
    result = engine.run_backtest(config, get_intraday=get_intraday, get_daily=get_daily)
    out = {"ok": True, "result": result, "coverage": coverage}
    if backfill_result is not None:
        out["backfill"] = backfill_result
    return out


# ---------------------------------------------------------------------------
# Saved configurations (optional convenience)
# ---------------------------------------------------------------------------
def list_configs() -> dict:
    return db.kv_get(_CONFIG_KV_KEY) or {}


def save_config(name: str, config: dict) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("config name required")
    store = list_configs()
    store[name] = config
    db.kv_set(_CONFIG_KV_KEY, store)
    return store


def delete_config(name: str) -> dict:
    store = list_configs()
    store.pop(name, None)
    db.kv_set(_CONFIG_KV_KEY, store)
    return store
