"""Day-trade sleeve persistence — side-channel, per-trading-day files under
``DATA_DIR/daytrade_log/``, the same zero-authority pattern ``csp_dry_powder.py``
uses (see its module docstring): nothing here is a real position or execution,
so none of it belongs in state.json / the single source of truth for the real
CFM book. Two files per trading day:

  ``YYYY-MM-DD.json``       nightly screener output (``daytrade.universe.screen``)
                             — whole-file atomic write, overwritten if the
                             screener re-runs the same day.
  ``YYYY-MM-DD.bars.jsonl``  5-min bars ingested during the signal window
                             (``daytrade.bars.ingest``) — one JSON object per
                             line, append-only.
  ``YYYY-MM-DD.signals.jsonl``  every setup/entry/exit event the signal
                             engine produces (``daytrade.signals.run_day``,
                             rule 8: "log every signal, taken or not") — one
                             JSON object per line, append-only.

Market-wide, not per-account: the screener universe and bar prices don't
depend on which book is active (unlike CFM positions), so there is exactly one
day's file regardless of how many accounts the dashboard holds.
"""
from __future__ import annotations

import json
import os
import threading

import config

STORE_DIR = os.path.join(config.DATA_DIR, "daytrade_log")
SCHEMA_VERSION = 1
_lock = threading.RLock()


def _ensure_dir() -> None:
    os.makedirs(STORE_DIR, exist_ok=True)


def _screen_path(day: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.json")


def _bars_path(day: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.bars.jsonl")


def _signals_path(day: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.signals.jsonl")


def save_screen(result: dict) -> None:
    """Atomic whole-file write of one day's screener output (tmp + fsync +
    os.replace, the same durability shape as logging_handler._atomic_write)."""
    _ensure_dir()
    path = _screen_path(result["date"])
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def load_screen(day: str) -> dict | None:
    """That trading day's screener output, or None if it hasn't run yet."""
    path = _screen_path(day)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def append_bars(day: str, rows: list[dict]) -> int:
    """Append bar rows for one trading day. Returns how many were written."""
    if not rows:
        return 0
    _ensure_dir()
    path = _bars_path(day)
    with _lock:
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return len(rows)


def load_bars(day: str, symbol: str | None = None) -> list[dict]:
    """That trading day's ingested bars, oldest first. A malformed line is
    skipped, never fatal (same discipline as the order journal reader)."""
    path = _bars_path(day)
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if symbol is None or row.get("symbol") == symbol:
                out.append(row)
    return out


def append_signals(day: str, rows: list[dict]) -> int:
    """Append signal-engine events for one trading day. Returns how many were
    written. Dedup against events already logged (e.g. an earlier scheduler
    tick's replay) is the caller's job — daytrade.signals.run_day does it by
    ``id`` before calling this, the same division of labour as bars.ingest
    dedup-ing before append_bars."""
    if not rows:
        return 0
    _ensure_dir()
    path = _signals_path(day)
    with _lock:
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return len(rows)


def load_signals(day: str, symbol: str | None = None) -> list[dict]:
    """That trading day's signal-engine events, oldest first. A malformed
    line is skipped, never fatal."""
    path = _signals_path(day)
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if symbol is None or row.get("symbol") == symbol:
                out.append(row)
    return out
