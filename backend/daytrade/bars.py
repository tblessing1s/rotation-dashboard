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
            # Two distinct provider failure modes, neither caught by a bare
            # `<= now` check alone:
            #  1. Padding — a same-day response includes slots for the rest
            #     of the session that haven't happened yet, carrying the
            #     last traded price forward under a not-yet-reached
            #     timestamp. `<= now` catches this one.
            #  2. An entirely PRIOR trading day — e.g. right at/after today's
            #     open, before today's own candles exist yet, the provider
            #     hands back yesterday's full session instead of today's
            #     partial one. Every one of those candles is trivially
            #     `<= now` (they're all in the past), so `<= now` alone lets
            #     a whole stale session straight through — iloc[-1] then
            #     picks yesterday's last candle (typically ~15:55-16:00 ET)
            #     as if it were today's latest, and the signal engine, which
            #     correctly never sees a real in-window candle for today,
            #     silently never evaluates the symbol at all. Require BOTH:
            #     not after `now`, AND actually dated `day`.
            candles = candles[(candles.index <= now) & (candles.index.strftime("%Y-%m-%d") == day)]
            if candles.empty:
                raise ValueError("no candle for today at or before now")
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
