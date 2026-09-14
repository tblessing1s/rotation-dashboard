"""Day-trade nightly screener — strategy rule 1.

Screens the existing CFM ticker universe (``sector_data.all_tickers()``) for
$20-150 stocks, avg daily volume > 1M, and ATR% between 2% and 5%
(``config.DAYTRADE_*``), and records each candidate's most recent completed
session's high/low as the next day's prior-day levels for the setup/entry
rules (a later phase). Reuses the CFM daily-bar plumbing
(``data_handler.get_daily``) rather than a new data path — the day-trade
sleeve is a new STRATEGY, not a new DATA SOURCE.

``screen()`` logs every candidate looked at, pass or fail (``screened``), not
just the picks — adherence/coverage is auditable from day one, the same spirit
as the brief's "log every signal, taken or not" rule applied to the universe
step.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
import data_handler
import indicators
import sector_data

from daytrade import store

logger = logging.getLogger("cfm.daytrade")


def _evaluate(symbol: str) -> dict:
    """One ticker's screener readout. Never raises — a fetch failure is
    recorded as a failed candidate, not a crash of the whole nightly sweep."""
    try:
        df = data_handler.get_daily(symbol)
    except Exception as e:  # noqa: BLE001 — one bad symbol must not sink the sweep
        return {"symbol": symbol, "qualified": False, "reason": f"data unavailable: {e}"}
    if df is None or df.empty:
        return {"symbol": symbol, "qualified": False, "reason": "no data"}

    last = df.iloc[-1]
    price = float(last["Close"])
    avg_volume = float(df["Volume"].tail(config.DAYTRADE_AVG_VOLUME_LOOKBACK_DAYS).mean())
    atr14 = indicators.atr(df, window=config.DAYTRADE_ATR_WINDOW)
    atr_pct = indicators.atr_pct(df, window=config.DAYTRADE_ATR_WINDOW)

    row = {
        "symbol": symbol,
        "price": round(price, 2),
        "avg_volume": round(avg_volume),
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "atr_pct": atr_pct,
        "prior_day_high": round(float(last["High"]), 2),
        "prior_day_low": round(float(last["Low"]), 2),
    }
    reasons = []
    if not (config.DAYTRADE_MIN_PRICE <= price <= config.DAYTRADE_MAX_PRICE):
        reasons.append("price out of range")
    if avg_volume <= config.DAYTRADE_MIN_AVG_VOLUME:
        reasons.append("avg volume too low")
    if atr_pct is None or not (config.DAYTRADE_ATR_PCT_MIN <= atr_pct <= config.DAYTRADE_ATR_PCT_MAX):
        reasons.append("ATR% out of range")
    row["qualified"] = not reasons
    row["reason"] = None if row["qualified"] else "; ".join(reasons)
    return row


def rank(qualified: list[dict]) -> list[dict]:
    """PROPOSED_DEFAULT tie-break when more than DAYTRADE_UNIVERSE_MAX names
    qualify: most-liquid first (avg_volume desc). The brief specifies the
    qualifying bounds but not a selection order among names that clear them;
    liquidity is the safer default for a 1%-risk intraday strategy. Revisit
    once the signal engine (a later phase) has real fills to calibrate
    against."""
    return sorted(qualified, key=lambda r: r["avg_volume"], reverse=True)


def screen(tickers: list[str] | None = None, now: datetime | None = None) -> dict:
    """Run the nightly screener once and persist the result. Returns the same
    dict that gets written to disk."""
    now = now or datetime.now(timezone.utc)
    tickers = tickers if tickers is not None else sector_data.all_tickers()

    screened = [_evaluate(t) for t in tickers]
    qualified = [r for r in screened if r.get("qualified")]
    picks = rank(qualified)[:config.DAYTRADE_UNIVERSE_MAX]

    result = {
        "schema_version": store.SCHEMA_VERSION,
        "date": now.strftime("%Y-%m-%d"),
        "computed_at": now.astimezone(timezone.utc).isoformat(),
        "picks": picks,
        "screened": screened,
    }
    store.save_screen(result)
    if len(picks) < config.DAYTRADE_UNIVERSE_MIN:
        logger.warning("daytrade screener: only %d name(s) qualified (rule 1 wants 3-5)",
                        len(picks))
    return result
