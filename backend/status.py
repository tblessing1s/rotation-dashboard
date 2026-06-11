"""Per-symbol / per-series freshness report, shared by /api/data-status and the CLI."""
from __future__ import annotations

import json

import config as cfg
import db
import ingest
import market_calendar as mcal


def data_status() -> dict:
    symbols = {}
    for symbol in ingest.universe():
        bar = db.latest_bar(symbol)
        if bar is None:
            symbols[symbol] = {"staleness": "missing"}
            continue
        symbols[symbol] = {
            "lastDate": bar["date"],
            "close": round(bar["close"], 2),
            "source": bar["source"],
            "fetchedAt": bar["fetched_at"],
            "staleness": mcal.staleness(bar["date"]),
        }

    series = {}
    for series_id in cfg.FRED_SERIES:
        s = db.get_macro_series(series_id)
        if s is None or s.empty:
            series[series_id] = {"staleness": "missing"}
            continue
        series[series_id] = {
            "lastDate": str(s.index[-1].date()),
            "value": float(s.iloc[-1]),
            "source": s.attrs.get("source"),
            "fetchedAt": s.attrs.get("fetched_at"),
        }

    last_run = db.last_ingest_run()
    if last_run and last_run.get("detail"):
        try:
            last_run["detail"] = json.loads(last_run["detail"])
        except (TypeError, ValueError):
            pass

    counts = {}
    for info in symbols.values():
        counts[info["staleness"]] = counts.get(info["staleness"], 0) + 1

    return {
        "symbols": symbols,
        "fredSeries": series,
        "summary": counts,
        "quarantineOpen": len(db.recent_quarantine()),
        "lastRun": last_run,
        "lastCompletedTradingDay": str(mcal.last_completed_trading_day()),
    }
