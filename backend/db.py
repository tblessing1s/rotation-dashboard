"""
SQLite datastore for the rotation dashboard.

All externally fetched data lands here (append-only) and the Flask app reads
exclusively from here — never from a provider at view time.

Tables
------
bars                daily OHLCV rows; append-only. A (symbol, date) can have
                    multiple rows over time (re-fetches, corrections, multiple
                    sources); reads resolve the canonical row per date by
                    source priority, then fetched_at.
macro_observations  FRED-style series points (DFF, CPIAUCSL, GDPC1, UNRATE);
                    same append-only pattern.
snapshots           computed indicator / macro payloads (JSON), written by
                    ingestion so the request path never computes from scratch.
overrides           manual values entered in the UI; source='manual',
                    always win over ingested values.
quarantine          rows that failed validation, with the reason.
ingest_runs         one row per ingestion run, for the status report.
kv                  small key/value scratch (e.g. cached provider tokens).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")
DB_FILE = os.environ.get("DB_FILE") or os.path.join(DATA_DIR, "rotation.db")

# Canonical row per (symbol, date): lowest rank wins, fetched_at breaks ties.
SOURCE_PRIORITY = {"manual": 0, "schwab": 1, "tiingo": 2, "alpaca": 3, "yahoo": 4}
DEFAULT_PRIORITY = 9

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL,
    close REAL NOT NULL,
    volume REAL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol_date ON bars(symbol, date);

CREATE TABLE IF NOT EXISTS macro_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series TEXT NOT NULL,
    date TEXT NOT NULL,
    value REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macro_series_date ON macro_observations(series, date);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    as_of TEXT,
    payload TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_kind_key ON snapshots(kind, key, id);

CREATE TABLE IF NOT EXISTS overrides (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    symbol TEXT,
    payload TEXT,
    reason TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    trigger TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    """One connection per thread; WAL so a background ingest never blocks reads."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------
def append_bars(symbol: str, bars: pd.DataFrame, source: str) -> int:
    """Append daily bars (index: dates; columns: Open/High/Low/Close/Volume).

    Append-only with dedup: a row is written only when its values differ from
    the latest stored row for that (symbol, date, source), so re-running
    ingestion is idempotent but provider corrections still land as new rows.
    Returns the number of rows written.
    """
    conn = connect()
    now = utcnow()
    existing = {}
    for row in conn.execute(
        "SELECT date, open, high, low, close, volume FROM bars"
        " WHERE symbol=? AND source=? ORDER BY fetched_at, id",
        (symbol, source),
    ):
        existing[row["date"]] = (row["open"], row["high"], row["low"], row["close"], row["volume"])

    to_insert = []
    for idx, row in bars.iterrows():
        date = str(pd.Timestamp(idx).date())
        vals = tuple(
            None if pd.isna(row[c]) else round(float(row[c]), 6)
            for c in ("Open", "High", "Low", "Close", "Volume")
        )
        if vals[3] is None:
            continue
        prev = existing.get(date)
        if prev is not None and all(_close_enough(a, b) for a, b in zip(prev, vals)):
            continue
        to_insert.append((symbol, date, *vals, source, now))

    if to_insert:
        with conn:
            conn.executemany(
                "INSERT INTO bars (symbol, date, open, high, low, close, volume, source, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                to_insert,
            )
    return len(to_insert)


def _close_enough(a, b, rel=1e-9) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= rel * max(abs(float(a)), abs(float(b)), 1.0)


def get_bars(symbol: str) -> pd.DataFrame | None:
    """Canonical daily series: per date, best source (priority) then newest fetch."""
    conn = connect()
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, source, fetched_at"
        " FROM bars WHERE symbol=? ORDER BY date, fetched_at, id",
        (symbol,),
    ).fetchall()
    if not rows:
        return None
    best: dict[str, sqlite3.Row] = {}
    for row in rows:
        cur = best.get(row["date"])
        if cur is None or _beats(row, cur):
            best[row["date"]] = row
    ordered = [best[d] for d in sorted(best)]
    df = pd.DataFrame(
        {
            "Open": [r["open"] for r in ordered],
            "High": [r["high"] for r in ordered],
            "Low": [r["low"] for r in ordered],
            "Close": [r["close"] for r in ordered],
            "Volume": [r["volume"] for r in ordered],
        },
        index=pd.to_datetime([r["date"] for r in ordered]),
    )
    df.attrs["source"] = ordered[-1]["source"]
    df.attrs["fetched_at"] = ordered[-1]["fetched_at"]
    return df


def _beats(row: sqlite3.Row, cur: sqlite3.Row) -> bool:
    """Better source priority wins; within a source, the newer fetch wins."""
    rp = SOURCE_PRIORITY.get(row["source"], DEFAULT_PRIORITY)
    cp = SOURCE_PRIORITY.get(cur["source"], DEFAULT_PRIORITY)
    if rp != cp:
        return rp < cp
    return (row["fetched_at"], row["id"]) >= (cur["fetched_at"], cur["id"])


def latest_bar(symbol: str) -> dict | None:
    df = get_bars(symbol)
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    return {
        "symbol": symbol,
        "date": str(df.index[-1].date()),
        "open": None if pd.isna(last["Open"]) else float(last["Open"]),
        "close": float(last["Close"]),
        "source": df.attrs.get("source"),
        "fetched_at": df.attrs.get("fetched_at"),
    }


def known_symbols() -> list[str]:
    conn = connect()
    return [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM bars ORDER BY symbol")]


# ---------------------------------------------------------------------------
# Macro observations
# ---------------------------------------------------------------------------
def append_macro_series(series_id: str, series: pd.Series, source: str) -> int:
    conn = connect()
    now = utcnow()
    existing = {
        row["date"]: row["value"]
        for row in conn.execute(
            "SELECT date, value FROM macro_observations WHERE series=? AND source=?"
            " ORDER BY fetched_at, id",
            (series_id, source),
        )
    }
    to_insert = []
    for idx, val in series.dropna().items():
        date = str(pd.Timestamp(idx).date())
        val = round(float(val), 6)
        prev = existing.get(date)
        if prev is not None and _close_enough(prev, val):
            continue
        to_insert.append((series_id, date, val, source, now))
    if to_insert:
        with conn:
            conn.executemany(
                "INSERT INTO macro_observations (series, date, value, source, fetched_at)"
                " VALUES (?,?,?,?,?)",
                to_insert,
            )
    return len(to_insert)


def get_macro_series(series_id: str) -> pd.Series | None:
    conn = connect()
    rows = conn.execute(
        "SELECT date, value, source, fetched_at FROM macro_observations"
        " WHERE series=? ORDER BY date, fetched_at, id",
        (series_id,),
    ).fetchall()
    if not rows:
        return None
    best = {}
    for row in rows:
        best[row["date"]] = row  # later fetch wins per date
    ordered = [best[d] for d in sorted(best)]
    s = pd.Series(
        [r["value"] for r in ordered],
        index=pd.to_datetime([r["date"] for r in ordered]),
        dtype=float,
    )
    s.attrs["source"] = ordered[-1]["source"]
    s.attrs["fetched_at"] = ordered[-1]["fetched_at"]
    return s


# ---------------------------------------------------------------------------
# Snapshots (computed indicators / macro)
# ---------------------------------------------------------------------------
def save_snapshot(kind: str, key: str, payload: dict, as_of: str | None) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO snapshots (kind, key, as_of, payload, computed_at) VALUES (?,?,?,?,?)",
            (kind, key, as_of, json.dumps(payload), utcnow()),
        )


def latest_snapshot(kind: str, key: str) -> dict | None:
    conn = connect()
    row = conn.execute(
        "SELECT as_of, payload, computed_at FROM snapshots WHERE kind=? AND key=?"
        " ORDER BY id DESC LIMIT 1",
        (kind, key),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload["_asOf"] = row["as_of"]
    payload["_computedAt"] = row["computed_at"]
    return payload


def latest_snapshots(kind: str) -> dict[str, dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT key, as_of, payload, computed_at FROM snapshots WHERE kind=?"
        " AND id IN (SELECT MAX(id) FROM snapshots WHERE kind=? GROUP BY key)",
        (kind, kind),
    ).fetchall()
    out = {}
    for row in rows:
        payload = json.loads(row["payload"])
        payload["_asOf"] = row["as_of"]
        payload["_computedAt"] = row["computed_at"]
        out[row["key"]] = payload
    return out


# ---------------------------------------------------------------------------
# Overrides (manual values always beat ingested values)
# ---------------------------------------------------------------------------
def set_override(scope: str, key: str, value, source: str = "manual") -> None:
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO overrides (scope, key, value, source, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,"
            " source=excluded.source, updated_at=excluded.updated_at",
            (scope, key, json.dumps(value), source, utcnow()),
        )


def clear_override(scope: str, key: str) -> None:
    conn = connect()
    with conn:
        conn.execute("DELETE FROM overrides WHERE scope=? AND key=?", (scope, key))


def get_overrides(scope: str) -> dict[str, dict]:
    conn = connect()
    return {
        row["key"]: {
            "value": json.loads(row["value"]),
            "source": row["source"],
            "updatedAt": row["updated_at"],
        }
        for row in conn.execute("SELECT * FROM overrides WHERE scope=?", (scope,))
    }


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------
def quarantine(kind: str, symbol: str | None, payload, reason: str, source: str | None) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO quarantine (kind, symbol, payload, reason, source, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (kind, symbol, json.dumps(payload, default=str), reason, source, utcnow()),
        )


def recent_quarantine(limit: int = 50) -> list[dict]:
    conn = connect()
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, kind, symbol, payload, reason, source, created_at FROM quarantine"
            " WHERE resolved=0 ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    ]


# ---------------------------------------------------------------------------
# Ingest runs
# ---------------------------------------------------------------------------
def start_ingest_run(trigger: str) -> int:
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (?,?)",
            (utcnow(), trigger),
        )
    return cur.lastrowid


def finish_ingest_run(run_id: int, status: str, detail: dict) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE ingest_runs SET finished_at=?, status=?, detail=? WHERE id=?",
            (utcnow(), status, json.dumps(detail, default=str), run_id),
        )


def last_ingest_run() -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def last_successful_ingest() -> dict | None:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM ingest_runs WHERE status IN ('ok','partial')"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# KV
# ---------------------------------------------------------------------------
def kv_get(key: str):
    conn = connect()
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else None


def kv_set(key: str, value) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )
