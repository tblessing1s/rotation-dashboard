"""
Sanity checks applied to every fetched bar before it is written.

A bar that fails goes to the quarantine table with the reason; the last good
value stays current (and is rendered stale by the UI rather than wrong).
The move band is configurable per symbol — VIX legitimately moves in a day
what XLP moves in a year.
"""
from __future__ import annotations

import pandas as pd

import config as cfg


def max_move_for(symbol: str) -> float:
    return cfg.VALIDATION_MAX_MOVE_PER_SYMBOL.get(symbol, cfg.VALIDATION_MAX_MOVE)


def check_bar(row: pd.Series, prev_close: float | None, band: float) -> str | None:
    """Return a rejection reason, or None when the bar is sane."""
    close = row.get("Close")
    if close is None or pd.isna(close):
        return "close is null"
    close = float(close)
    if close <= 0:
        return f"close {close} <= 0"
    for col in ("Open", "High", "Low"):
        val = row.get(col)
        if val is not None and not pd.isna(val) and float(val) <= 0:
            return f"{col.lower()} {float(val)} <= 0"
    high, low = row.get("High"), row.get("Low")
    if high is not None and low is not None and not pd.isna(high) and not pd.isna(low) \
            and float(high) < float(low):
        return f"high {float(high)} < low {float(low)}"
    vol = row.get("Volume")
    if vol is not None and not pd.isna(vol) and float(vol) < 0:
        return f"volume {float(vol)} < 0"
    if prev_close is not None and prev_close > 0:
        move = abs(close - prev_close) / prev_close
        if move > band:
            return (
                f"close {close} moved {move * 100:.1f}% vs prior close {prev_close}"
                f" (band ±{band * 100:.0f}%)"
            )
    return None


def validate_bars(symbol: str, bars: pd.DataFrame, prior_close: float | None = None) -> tuple[pd.DataFrame, list[dict]]:
    """Split fetched bars into (accepted DataFrame, rejected row dicts).

    Bars are checked in date order; each accepted close becomes the reference
    for the next bar's move check. `prior_close` seeds the chain when the
    fetch starts after already-stored history.
    """
    band = max_move_for(symbol)
    bars = bars.sort_index()
    accepted_idx = []
    rejected: list[dict] = []
    prev = prior_close
    for idx, row in bars.iterrows():
        reason = check_bar(row, prev, band)
        if reason:
            rejected.append({
                "date": str(pd.Timestamp(idx).date()),
                "bar": {c: (None if pd.isna(row.get(c)) else float(row.get(c)))
                        for c in ("Open", "High", "Low", "Close", "Volume") if c in row},
                "reason": reason,
            })
        else:
            accepted_idx.append(idx)
            prev = float(row["Close"])
    return bars.loc[accepted_idx], rejected
