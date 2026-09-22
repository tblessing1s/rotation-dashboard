"""Day-trade sleeve persistence — side-channel, per-trading-day files under
``DATA_DIR/daytrade_log/``, the same zero-authority pattern ``csp_dry_powder.py``
uses (see its module docstring): nothing here is a real position or execution,
so none of it belongs in state.json / the single source of truth for the real
CFM book.

  ``YYYY-MM-DD.json``       nightly screener output (``daytrade.universe.screen``)
                             — whole-file atomic write, overwritten if the
                             screener re-runs the same day. SHARED across
                             every account: which stocks qualify doesn't
                             depend on account data.
  ``YYYY-MM-DD.bars.jsonl``  5-min bars ingested during the signal window
                             (``daytrade.bars.ingest``) — one JSON object per
                             line, append-only. Also SHARED.
  ``YYYY-MM-DD.<account_id>.signals.jsonl``  every setup/entry/exit event
                             that account's signal engine produced
                             (``daytrade.signals.run_day``, rule 8: "log
                             every signal, taken or not") — one JSON object
                             per line, append-only. PER ACCOUNT: an account
                             sizes off its own budget and runs its own
                             guardrails/trial, so two accounts can each set
                             up (or skip) the same symbol independently.
  ``YYYY-MM-DD.<account_id>.trades.json``  that account's TRADE LOG for the
                             day (``daytrade.adapters.PaperAdapter``) — one
                             row per trade_id, keyed (not append-only like
                             the signals log: a trade's row is updated in
                             place as it fills and later exits), whole-file
                             atomic write. Aggregated and fill-oriented
                             (entry/exit prices, sizes, $ P&L) where the
                             signals log is raw and decision-oriented (every
                             setup/skip/entry/exit, taken or not) — the two
                             intentionally overlap in the trades an account
                             actually took, read the signals log for "what
                             did the strategy consider" and the trade log
                             for "what actually filled".

Screener/bars are the only files with no account in their name — every other
file's day-key is followed by the account_id, mirroring accounts.py's own
``state.json`` / ``state.<id>.json`` sibling convention (see
``daytrade.settings`` for the enable/disable toggle this all serves).
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


def _screen_health_path() -> str:
    return os.path.join(STORE_DIR, "screen_health.json")


def _bars_path(day: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.bars.jsonl")


def _signals_path(day: str, account_id: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.{account_id}.signals.jsonl")


def _trades_path(day: str, account_id: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.{account_id}.trades.json")


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


def save_screen_health(entry: dict) -> None:
    """Overwrite the one 'last successful screen' record — not a history,
    just the single latest success, keyed by trigger ("scheduled" | "manual")
    so the SCHEDULED after-close run's own health (the thing an operator
    actually needs to trust — "did last night's job really run" — without
    watching the app around 4:45pm ET) is never masked by an unrelated manual
    "Rescan now" click. Same atomic-write shape as save_screen."""
    _ensure_dir()
    path = _screen_health_path()
    with _lock:
        current = load_screen_health()
        current[entry["trigger"]] = entry
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def load_screen_health() -> dict:
    """{"scheduled": {...}, "manual": {...}} — each present only once a
    screen has actually succeeded with that trigger. Never raises: a
    missing/corrupt file just means "no recorded success yet"."""
    path = _screen_health_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


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


def latest_bars(day: str) -> dict[str, dict]:
    """The most recently ingested bar per symbol for one trading day, keyed
    by symbol. ``append_bars`` always appends each ingest tick's rows after
    every earlier tick's (bars.ingest dedupes but never reorders), so the
    LAST occurrence of a symbol in file order is its most recent bar —
    no need to compare timestamps. Empty dict if nothing's been ingested
    yet (e.g. before the window opens, or a date with no screener run)."""
    out: dict[str, dict] = {}
    for row in load_bars(day):
        symbol = row.get("symbol")
        if symbol:
            out[symbol] = row
    return out


def save_trades(day: str, account_id: str, trades: dict) -> None:
    """Atomic whole-file write of one account's trade log for one day —
    trade_id -> row. Same durability shape as save_screen; the caller
    (PaperAdapter) owns loading the existing dict, mutating it, and calling
    this with the merged result."""
    _ensure_dir()
    path = _trades_path(day, account_id)
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(trades, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def load_trades(day: str, account_id: str) -> dict:
    """That account's trade log for one trading day — trade_id -> row. Empty
    dict if no trade has opened yet."""
    path = _trades_path(day, account_id)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def iter_all_trades(account_id: str):
    """Yield ``(day, trades)`` for every day that account has ever had a
    trade log, oldest first (the ``YYYY-MM-DD`` filename sorts
    chronologically). Used by ``daytrade.trial`` to aggregate that account's
    whole paper-trading trial across days without a separate running counter
    that could drift from what actually closed. A malformed file is
    skipped, never fatal."""
    if not os.path.isdir(STORE_DIR):
        return
    suffix = f".{account_id}.trades.json"
    for name in sorted(os.listdir(STORE_DIR)):
        if not name.endswith(suffix):
            continue
        day = name[: -len(suffix)]
        try:
            with open(os.path.join(STORE_DIR, name), encoding="utf-8") as fh:
                yield day, json.load(fh)
        except (OSError, ValueError):
            continue


def append_signals(day: str, account_id: str, rows: list[dict]) -> int:
    """Append one account's signal-engine events for one trading day.
    Returns how many were written. Dedup against events already logged
    (e.g. an earlier scheduler tick's replay) is the caller's job —
    daytrade.signals.run_day does it by ``id`` before calling this, the same
    division of labour as bars.ingest dedup-ing before append_bars."""
    if not rows:
        return 0
    _ensure_dir()
    path = _signals_path(day, account_id)
    with _lock:
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return len(rows)


def load_signals(day: str, account_id: str, symbol: str | None = None) -> list[dict]:
    """That account's signal-engine events for one trading day, oldest
    first. A malformed line is skipped, never fatal."""
    path = _signals_path(day, account_id)
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
