"""Daily Symbol Genius color history — a shadow-log for measuring SYM flip
frequency BEFORE committing to a per-symbol yellow dwell.

The audit deferred the Symbol Genius dwell (a GREEN→YELLOW 3-day / RED-immediate
anti-flap hold) with an explicit prerequisite: *shadow-log flip frequency first*,
so the churn a dwell would suppress is measured, not guessed. This is that log.

Like ``regime_history`` / ``iv_history`` / ``burn_marks`` it is DERIVED telemetry
(recomputable from cached bars), kept in a standalone store under ``DATA_DIR`` —
NOT in state.json and NOT rebuilt by ``recompute_derived`` (which keys off the
executions ledger). One record per symbol per trading day: the published Symbol
Genius color + green count. The nightly maintenance sweep appends today's point
(idempotent per day); ``flip_stats`` reports how often each name's color changed
over the retained window, so the dwell decision has data behind it.

Recording does NOT change any behavior — SYM is still computed and displayed
exactly as before. This only observes it.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

HISTORY_PATH = os.path.join(config.DATA_DIR, "symbol_genius_history.json")
_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"symbols": {}}


def _save(data: dict) -> None:
    tmp = f"{HISTORY_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, HISTORY_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def series(ticker: str) -> list[dict]:
    """All stored points for one symbol, chronological (oldest first)."""
    return list(_load()["symbols"].get(ticker.upper(), []))


def flip_count(ticker: str) -> int:
    """Number of color changes across the retained window for one symbol (a
    green→yellow→green round trip counts as 2). Points with a null color are
    skipped so a data gap doesn't read as two flips."""
    colors = [r.get("color") for r in series(ticker) if r.get("color")]
    return sum(1 for a, b in zip(colors, colors[1:]) if a != b)


def flip_stats() -> dict:
    """Per-symbol flip counts + a universe summary, for deciding whether a
    per-symbol yellow dwell is worth building. ``flips_per_month`` normalizes for
    an easy read; ``names_flipped`` is how many names changed color at least once
    over the window."""
    data = _load()["symbols"]
    per: dict[str, dict] = {}
    total_flips = 0
    names_flipped = 0
    for t, recs in data.items():
        colors = [r.get("color") for r in recs if r.get("color")]
        flips = sum(1 for a, b in zip(colors, colors[1:]) if a != b)
        span = len(colors)
        per[t] = {
            "records": span,
            "flips": flips,
            "flips_per_month": round(flips / span * 21, 2) if span > 1 else 0.0,
            "current": colors[-1] if colors else None,
        }
        total_flips += flips
        names_flipped += 1 if flips else 0
    return {
        "symbols": per,
        "summary": {
            "names": len(per),
            "total_flips": total_flips,
            "names_flipped": names_flipped,
            "window_days": config.SYMBOL_GENIUS_HISTORY_DAYS,
        },
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def record(ticker: str, color: str | None, greens: int | None,
           day: str | None = None) -> None:
    """Append one day's Symbol Genius color for a symbol (idempotent per day — the
    last write of a day replaces that day's point). Capped to the newest
    ``config.SYMBOL_GENIUS_HISTORY_DAYS`` points per symbol."""
    ticker = (ticker or "").upper()
    if not ticker:
        return
    day = day or _today()
    point = {"date": day, "color": color, "greens": greens}
    with _lock:
        data = _load()
        recs = data["symbols"].setdefault(ticker, [])
        if recs and recs[-1].get("date") == day:
            recs[-1] = point
        else:
            recs.append(point)
            recs.sort(key=lambda r: r.get("date", ""))
        del recs[:-config.SYMBOL_GENIUS_HISTORY_DAYS]
        _save(data)


def record_many(entries: list[dict], day: str | None = None) -> int:
    """Record several {ticker, color, greens} points for one day in a single
    load/save. Returns the number written. Used by the nightly sweep."""
    day = day or _today()
    entries = [e for e in (entries or []) if (e.get("ticker") or "").strip()]
    if not entries:
        return 0
    with _lock:
        data = _load()
        for e in entries:
            t = e["ticker"].upper()
            point = {"date": day, "color": e.get("color"), "greens": e.get("greens")}
            recs = data["symbols"].setdefault(t, [])
            if recs and recs[-1].get("date") == day:
                recs[-1] = point
            else:
                recs.append(point)
                recs.sort(key=lambda r: r.get("date", ""))
            del recs[:-config.SYMBOL_GENIUS_HISTORY_DAYS]
        _save(data)
    return len(entries)


def record_today(tickers: list[str], *, day: str | None = None) -> dict:
    """Compute and persist today's Symbol Genius color for each ticker, from cached
    bars. Best-effort: a name whose bars are missing records a null color (still a
    valid 'no read' point). Never raises into the maintenance sweep."""
    try:
        import data_handler
        import symbol_genius
        entries = []
        for t in dict.fromkeys((t or "").upper() for t in (tickers or []) if t):
            try:
                sg = symbol_genius.compute(data_handler.get_daily(t))
                entries.append({"ticker": t, "color": sg["color"], "greens": sg["greens"]})
            except Exception:  # noqa: BLE001 — one name must not sink the sweep
                entries.append({"ticker": t, "color": None, "greens": None})
        n = record_many(entries, day=day)
        return {"ok": True, "recorded": n}
    except Exception as e:  # noqa: BLE001 — a telemetry append must not sink maintenance
        return {"ok": False, "error": str(e)}
