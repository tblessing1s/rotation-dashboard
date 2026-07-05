"""Per-ticker implied-volatility history + IV rank.

Juice adequacy answers "is there enough premium to clear the bar" — but it
prices the weekly short at the stock's trailing *realized* vol, so it can't see
whether THIS week's implied vol is rich or cheap for this name. IV rank closes
that gap: it's where the current IV sits in its own trailing-year range.

    IV rank       = (iv_now − iv_min) / (iv_max − iv_min) × 100   (0 = year low, 100 = year high)
    IV percentile = % of stored days whose IV ≤ iv_now

We don't pull a year of chains for this — we accrue one point per day from the
IVs the app *already* computes (the option-chain view records the weekly ATM IV;
nightly maintenance records it for held names). History lives in
``DATA_DIR/iv_history.json`` — market data, not a trading record, so it stays out
of state.json. Rank needs a minimum sample to mean anything; below it, callers
get ``iv_rank: None`` rather than a misleading number.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

IV_HISTORY_PATH = os.path.join(config.DATA_DIR, "iv_history.json")
_MAX_POINTS = 260          # ~1 trading year per ticker
_MIN_POINTS = 20           # below this, a rank is noise — report None
_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        with open(IV_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    tmp = f"{IV_HISTORY_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, IV_HISTORY_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def record(ticker: str, iv: float | None, day: str | None = None) -> bool:
    """Append today's IV for a ticker (one point per calendar day — the last
    write of the day wins). Returns True if stored. Silently ignores junk IVs so
    a bad chain never poisons the series."""
    if iv is None:
        return False
    try:
        iv = float(iv)
    except (TypeError, ValueError):
        return False
    if not (0 < iv < 1000):  # IV is a percent here (e.g. 42.5); reject nonsense
        return False
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return False
    day = day or _today()
    with _lock:
        data = _load()
        series = data.setdefault(ticker, [])
        if series and series[-1].get("date") == day:
            series[-1]["iv"] = round(iv, 2)      # replace today's point
        else:
            series.append({"date": day, "iv": round(iv, 2)})
        del series[:-_MAX_POINTS]
        _save(data)
    return True


def series(ticker: str) -> list[dict]:
    return list(_load().get((ticker or "").strip().upper(), []))


def iv_rank(ticker: str, current_iv: float | None = None) -> dict:
    """IV rank + percentile for a ticker over its stored trailing year. When
    ``current_iv`` is given it's included as today's point in the comparison (so
    a fresh live IV ranks against history even before it's persisted)."""
    rows = series(ticker)
    ivs = [float(r["iv"]) for r in rows if r.get("iv") is not None]
    now = None
    if current_iv is not None:
        try:
            now = float(current_iv)
        except (TypeError, ValueError):
            now = None
    if now is None:
        now = ivs[-1] if ivs else None
    elif not ivs or ivs[-1] != round(now, 2):
        ivs = ivs + [now]

    out = {"ticker": (ticker or "").strip().upper(), "iv_now": round(now, 2) if now is not None else None,
           "days": len(ivs), "iv_rank": None, "iv_percentile": None,
           "iv_min": None, "iv_max": None}
    if now is None or len(ivs) < _MIN_POINTS:
        return out
    lo, hi = min(ivs), max(ivs)
    out["iv_min"], out["iv_max"] = round(lo, 2), round(hi, 2)
    out["iv_rank"] = round((now - lo) / (hi - lo) * 100, 1) if hi > lo else 0.0
    out["iv_percentile"] = round(sum(1 for v in ivs if v <= now) / len(ivs) * 100, 1)
    return out
