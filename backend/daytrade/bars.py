"""Day-trade 5-min bar ingestion — strategy rule 2's signal window.

Runs on the ``config.DAYTRADE_BAR_INTERVAL_MINUTES`` cadence during the
8:30-10:00 AM CT window and appends each of today's screener picks' latest
completed 5-min candle to that trading day's bar log (``daytrade/store.py``).
The signal engine that reads these bars for the setup/entry/stop rules (2-8)
is a later phase — this only guarantees the candles exist when that phase
needs them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import data_handler

from daytrade import store

logger = logging.getLogger("cfm.daytrade")


def _todays_picks(day: str) -> list[str]:
    screened = store.load_screen(day)
    if not screened:
        return []
    return [p["symbol"] for p in screened.get("picks", [])]


def _bar_row(symbol: str, day: str, bar) -> dict:
    when = bar.name
    return {
        "symbol": symbol,
        "date": day,
        "datetime": when.isoformat() if hasattr(when, "isoformat") else str(when),
        "open": float(bar["Open"]),
        "high": float(bar["High"]),
        "low": float(bar["Low"]),
        "close": float(bar["Close"]),
        "volume": float(bar["Volume"]),
    }


def ingest(now: datetime | None = None, symbols: list[str] | None = None) -> dict:
    """Fetch the latest 5-min bar for each of today's screener picks (or an
    explicit ``symbols`` list) and append any not already logged to today's
    bar file. A single symbol's fetch failure is logged and skipped rather
    than aborting the rest."""
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    symbols = symbols if symbols is not None else _todays_picks(day)

    already = {(r["symbol"], r["datetime"]) for r in store.load_bars(day)}
    rows: list[dict] = []
    errors: dict[str, str] = {}
    for symbol in symbols:
        try:
            candles = data_handler.client().get_intraday_bars(symbol, minutes=5)
            row = _bar_row(symbol, day, candles.iloc[-1])
        except Exception as e:  # noqa: BLE001 — one symbol's outage must not skip the rest
            errors[symbol] = str(e)
            continue
        key = (row["symbol"], row["datetime"])
        if key in already:
            continue
        rows.append(row)
        already.add(key)

    written = store.append_bars(day, rows)
    if errors:
        logger.warning("daytrade bar ingest: %d symbol(s) failed: %s", len(errors), errors)
    return {"symbols": symbols, "written": written, "errors": errors}
