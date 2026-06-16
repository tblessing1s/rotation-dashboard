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
intraday_bars       5-minute (or other interval) OHLCV rows used by the
                    backtesting engine; same append-only / source-priority
                    pattern as `bars`, keyed by the candle's UTC epoch.
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

CREATE TABLE IF NOT EXISTS intraday_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,          -- YYYY-MM-DD, exchange-local (America/New_York)
    time TEXT NOT NULL,          -- HH:MM, exchange-local candle start
    epoch_ms INTEGER NOT NULL,   -- candle start in UTC ms; canonical ordering key
    interval_min INTEGER NOT NULL DEFAULT 5,
    open REAL, high REAL, low REAL,
    close REAL NOT NULL,
    volume REAL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intraday_symbol_date ON intraday_bars(symbol, interval_min, date);
CREATE INDEX IF NOT EXISTS idx_intraday_symbol_epoch ON intraday_bars(symbol, interval_min, epoch_ms);

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
    dedup_key TEXT,
    created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quarantine_dedup ON quarantine(dedup_key, resolved);

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

CREATE TABLE IF NOT EXISTS setup_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,          -- YYYY-MM-DD, exchange-local trading session
    ticker TEXT NOT NULL,
    candle_time TEXT NOT NULL,   -- HH:MM of the closed candle (Central, matches backtest)
    direction TEXT,              -- Long | Short
    level_type TEXT,             -- Y-HIGH | Y-LOW
    level REAL,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    position_size INTEGER,
    volume_ratio REAL,
    payload TEXT NOT NULL,       -- full signal dict as JSON
    created_at TEXT NOT NULL
);
-- One row per detected setup candle: re-running detection on the same closed
-- candle is idempotent (mirrors the append-only/dedup pattern used elsewhere).
CREATE UNIQUE INDEX IF NOT EXISTS idx_setup_signals_dedup
    ON setup_signals(date, ticker, candle_time);

CREATE TABLE IF NOT EXISTS intraday_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    level_type TEXT,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    exit_price REAL,
    position_size INTEGER NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    outcome TEXT NOT NULL DEFAULT 'OPEN', -- OPEN | WIN | LOSS | CLOSED | SKIP
    r_result REAL,
    account_type TEXT NOT NULL DEFAULT 'PAPER',
    order_id TEXT,
    payload TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intraday_trades_signal
    ON intraday_trades(date, ticker, entry_time, account_type);
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
        "SELECT id, date, open, high, low, close, volume, source, fetched_at"
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
# Intraday bars (5-minute OHLCV for the backtesting engine)
# ---------------------------------------------------------------------------
EXCHANGE_TZ = "America/New_York"


def _to_exchange_index(index) -> pd.DatetimeIndex:
    """Normalize a bar index to a tz-aware America/New_York DatetimeIndex.

    Providers hand us either UTC-aware timestamps (Schwab/Yahoo) or tz-naive
    timestamps already in exchange wall-clock (synthetic/test data); naive input
    is interpreted as exchange-local so the stored date/time match the trading
    session a human would read off the chart.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is None:
        return idx.tz_localize(EXCHANGE_TZ, nonexistent="shift_forward", ambiguous="NaT")
    return idx.tz_convert(EXCHANGE_TZ)


def append_intraday_bars(symbol: str, bars: pd.DataFrame, source: str,
                         interval_min: int = 5) -> int:
    """Append intraday OHLCV (index: timestamps; columns: Open/High/Low/Close/Volume).

    Append-only with dedup on (symbol, interval, epoch, source): re-pulling the
    same window is idempotent, but a provider correction still lands as a new
    row that wins by source priority then fetched_at (mirrors `bars`).
    """
    conn = connect()
    now = utcnow()
    if bars is None or bars.empty:
        return 0
    et_index = _to_exchange_index(bars.index)
    # Resolution-independent epoch-ms (pandas may use us- or ns-precision dtypes).
    epochs = (et_index.tz_convert("UTC") - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)

    existing = {
        row["epoch_ms"]: (row["open"], row["high"], row["low"], row["close"], row["volume"])
        for row in conn.execute(
            "SELECT epoch_ms, open, high, low, close, volume FROM intraday_bars"
            " WHERE symbol=? AND interval_min=? AND source=? ORDER BY fetched_at, id",
            (symbol, interval_min, source),
        )
    }

    to_insert = []
    for pos in range(len(bars)):
        row = bars.iloc[pos]
        epoch_ms = int(epochs[pos])
        vals = tuple(
            None if pd.isna(row[c]) else round(float(row[c]), 6)
            for c in ("Open", "High", "Low", "Close", "Volume")
        )
        if vals[3] is None:
            continue
        prev = existing.get(epoch_ms)
        if prev is not None and all(_close_enough(a, b) for a, b in zip(prev, vals)):
            continue
        ts = et_index[pos]
        to_insert.append((
            symbol, ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"), epoch_ms,
            interval_min, *vals, source, now,
        ))

    if to_insert:
        with conn:
            conn.executemany(
                "INSERT INTO intraday_bars"
                " (symbol, date, time, epoch_ms, interval_min, open, high, low, close, volume, source, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                to_insert,
            )
    return len(to_insert)


def get_intraday_bars(symbol: str, start_date: str, end_date: str,
                      interval_min: int = 5) -> pd.DataFrame | None:
    """Canonical intraday series for [start_date, end_date] (inclusive, ET dates).

    Per candle epoch, the best source (priority) then newest fetch wins. Returns
    a DataFrame with Open/High/Low/Close/Volume indexed by tz-naive ET
    timestamps (exchange wall-clock), or None when nothing is stored.
    """
    conn = connect()
    rows = conn.execute(
        "SELECT id, epoch_ms, time, date, open, high, low, close, volume, source, fetched_at"
        " FROM intraday_bars WHERE symbol=? AND interval_min=? AND date BETWEEN ? AND ?"
        " ORDER BY epoch_ms, fetched_at, id",
        (symbol, interval_min, start_date, end_date),
    ).fetchall()
    if not rows:
        return None
    best: dict[int, sqlite3.Row] = {}
    for row in rows:
        cur = best.get(row["epoch_ms"])
        if cur is None or _beats(row, cur):
            best[row["epoch_ms"]] = row
    ordered = [best[e] for e in sorted(best)]
    index = pd.to_datetime([r["epoch_ms"] for r in ordered], unit="ms", utc=True) \
        .tz_convert(EXCHANGE_TZ).tz_localize(None)
    df = pd.DataFrame(
        {
            "Open": [r["open"] for r in ordered],
            "High": [r["high"] for r in ordered],
            "Low": [r["low"] for r in ordered],
            "Close": [r["close"] for r in ordered],
            "Volume": [r["volume"] for r in ordered],
        },
        index=index,
    )
    df.attrs["source"] = ordered[-1]["source"]
    df.attrs["fetched_at"] = ordered[-1]["fetched_at"]
    return df


def intraday_coverage(symbol: str, start_date: str, end_date: str,
                      interval_min: int = 5) -> set[str]:
    """Set of ET dates (YYYY-MM-DD) that have any intraday bar in the range."""
    conn = connect()
    return {
        row["date"]
        for row in conn.execute(
            "SELECT DISTINCT date FROM intraday_bars"
            " WHERE symbol=? AND interval_min=? AND date BETWEEN ? AND ?",
            (symbol, interval_min, start_date, end_date),
        )
    }


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


def snapshots_by_as_of(kind: str, keys: list[str], as_of: str | None = None, limit_sessions: int = 3) -> list[dict]:
    """Return latest snapshot payloads grouped by as_of for selected keys.

    Each group contains the most recent snapshot row for each key on that
    as_of date. This is intentionally read-only and used by request handlers
    that need historical computed payloads without recalculating or fetching
    provider data.
    """
    if not keys:
        return []
    conn = connect()
    placeholders = ",".join("?" for _ in keys)
    params: list = [kind, *keys]
    if as_of:
        as_of_rows = [as_of]
    else:
        rows = conn.execute(
            f"SELECT DISTINCT as_of FROM snapshots WHERE kind=? AND key IN ({placeholders})"
            " AND as_of IS NOT NULL ORDER BY as_of DESC LIMIT ?",
            (*params, max(1, int(limit_sessions))),
        ).fetchall()
        as_of_rows = [row["as_of"] for row in rows]

    sessions = []
    for session_as_of in as_of_rows:
        rows = conn.execute(
            f"""
            SELECT s.key, s.as_of, s.payload, s.computed_at
            FROM snapshots s
            JOIN (
                SELECT key, MAX(id) AS id
                FROM snapshots
                WHERE kind=? AND as_of=? AND key IN ({placeholders})
                GROUP BY key
            ) latest ON latest.id = s.id
            ORDER BY s.key
            """,
            (kind, session_as_of, *keys),
        ).fetchall()
        payloads = {}
        computed = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["_asOf"] = row["as_of"]
            payload["_computedAt"] = row["computed_at"]
            payloads[row["key"]] = payload
            computed.append(row["computed_at"])
        sessions.append({
            "asOf": session_as_of,
            "computedAt": max(computed) if computed else None,
            "snapshots": payloads,
        })
    return sessions


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
def quarantine(kind: str, symbol: str | None, payload, reason: str, source: str | None,
               dedup_key: str | None = None) -> bool:
    """Record a data issue. With a dedup_key, re-detecting the same issue on
    every ingest run does not re-alert while the original is unresolved.
    Returns True when a new row was written."""
    conn = connect()
    if dedup_key:
        row = conn.execute(
            "SELECT 1 FROM quarantine WHERE dedup_key=? AND resolved=0 LIMIT 1",
            (dedup_key,),
        ).fetchone()
        if row:
            return False
    with conn:
        conn.execute(
            "INSERT INTO quarantine (kind, symbol, payload, reason, source, dedup_key, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (kind, symbol, json.dumps(payload, default=str), reason, source, dedup_key, utcnow()),
        )
    return True


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
# Setup signals (intraday executor — Phase 1 detection log)
# ---------------------------------------------------------------------------
def record_setup_signal(signal: dict) -> bool:
    """Append a detected setup signal. Idempotent per (date, ticker, candle_time):
    re-detecting the same closed candle does not write a duplicate row. Returns
    True when a new row was written."""
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO setup_signals"
            " (date, ticker, candle_time, direction, level_type, level, entry_price,"
            "  stop_price, target_price, position_size, volume_ratio, payload, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                signal.get("date"), signal.get("ticker"), signal.get("candle_time"),
                signal.get("direction"), signal.get("level_type"), signal.get("level"),
                signal.get("entry_price"), signal.get("stop_price"), signal.get("target_price"),
                signal.get("position_size"), signal.get("volume_ratio"),
                json.dumps(signal, default=str), utcnow(),
            ),
        )
    return cur.rowcount > 0


def recent_setup_signals(date: str | None = None, limit: int = 100) -> list[dict]:
    """Stored signals (newest first), optionally scoped to one ET session date."""
    conn = connect()
    if date:
        rows = conn.execute(
            "SELECT payload, created_at FROM setup_signals WHERE date=?"
            " ORDER BY id DESC LIMIT ?",
            (date, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payload, created_at FROM setup_signals ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        payload["_recordedAt"] = row["created_at"]
        out.append(payload)
    return out


# ---------------------------------------------------------------------------
# Intraday paper trades
# ---------------------------------------------------------------------------
def record_intraday_trade(trade: dict) -> dict:
    """Insert one paper intraday trade from an executor signal.

    The unique key is the signal candle (date/ticker/entry_time/account_type), so
    clicking "execute" twice returns the already logged paper trade instead of
    creating a duplicate fill.
    """
    account_type = str(trade.get("account_type") or "PAPER").upper()
    now = utcnow()
    payload = json.dumps(trade, default=str)
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO intraday_trades"
            " (date, ticker, direction, level_type, entry_price, stop_price, target_price,"
            "  exit_price, position_size, entry_time, exit_time, outcome, r_result,"
            "  account_type, order_id, payload, notes, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade.get("date"), trade.get("ticker"), trade.get("direction"),
                trade.get("level_type"), trade.get("entry_price"), trade.get("stop_price"),
                trade.get("target_price"), trade.get("exit_price"), trade.get("position_size"),
                trade.get("entry_time"), trade.get("exit_time"), trade.get("outcome", "OPEN"),
                trade.get("r_result"), account_type, trade.get("order_id"), payload,
                trade.get("notes"), now, now,
            ),
        )
        inserted = cur.rowcount > 0
        if inserted:
            trade_id = cur.lastrowid
        else:
            row = conn.execute(
                "SELECT id FROM intraday_trades WHERE date=? AND ticker=? AND entry_time=? AND account_type=?",
                (trade.get("date"), trade.get("ticker"), trade.get("entry_time"), account_type),
            ).fetchone()
            trade_id = row["id"] if row else None
    return get_intraday_trade(trade_id) if trade_id else {}


def get_intraday_trade(trade_id: int | None) -> dict | None:
    if trade_id is None:
        return None
    conn = connect()
    row = conn.execute("SELECT * FROM intraday_trades WHERE id=?", (trade_id,)).fetchone()
    return _trade_row(row) if row else None


def list_intraday_trades(date: str | None = None, status: str | None = None,
                         limit: int = 100) -> list[dict]:
    """Return paper intraday trades newest first, optionally by date/status."""
    conn = connect()
    clauses = ["account_type='PAPER'"]
    params: list[object] = []
    if date:
        clauses.append("date=?")
        params.append(date)
    if status:
        clauses.append("outcome=?")
        params.append(status)
    params.append(limit)
    rows = conn.execute(
        "SELECT * FROM intraday_trades WHERE " + " AND ".join(clauses) +
        " ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_trade_row(row) for row in rows]


def _trade_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        out["payload"] = json.loads(out.get("payload") or "{}")
    except json.JSONDecodeError:
        out["payload"] = {}
    return out


def update_paper_trade(
    order_id: str,
    *,
    outcome: str | None = None,
    exit_price: float | None = None,
    exit_time: str | None = None,
    notes: str | None = None,
) -> dict | None:
    """Close or update a paper trade's outcome, exit price, and R-result."""
    conn = connect()
    row = conn.execute(
        "SELECT * FROM intraday_trades WHERE order_id = ?", (order_id,)
    ).fetchone()
    if not row:
        return None
    r_result = row["r_result"]
    if exit_price is not None:
        ep = float(row["entry_price"] or 0)
        sp = float(row["stop_price"] or 0)
        risk = abs(ep - sp)
        if risk > 0:
            direction = str(row["direction"] or "").upper()
            if direction in ("LONG", "BUY"):
                r_result = round((float(exit_price) - ep) / risk, 2)
            elif direction in ("SHORT", "SELL"):
                r_result = round((ep - float(exit_price)) / risk, 2)
    with conn:
        conn.execute(
            """
            UPDATE intraday_trades
               SET outcome    = COALESCE(?, outcome),
                   exit_price = COALESCE(?, exit_price),
                   exit_time  = COALESCE(?, exit_time),
                   notes      = COALESCE(?, notes),
                   r_result   = ?,
                   updated_at = ?
             WHERE order_id = ?
            """,
            [outcome, exit_price, exit_time, notes, r_result, utcnow(), order_id],
        )
    updated = conn.execute(
        "SELECT * FROM intraday_trades WHERE order_id = ?", (order_id,)
    ).fetchone()
    return _trade_row(updated) if updated else None


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
