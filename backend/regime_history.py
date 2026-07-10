"""Daily market-regime history — one full Genius decision trace per trading day.

Regime is DERIVED data (recomputable from cached SPY bars), and it depends on
external market bars rather than on the executions ledger — so it does NOT belong
in the immutable executions and is NOT rebuilt by ``recompute_derived`` (which
keys off executions/positions only). It lives instead in a standalone telemetry
store under ``DATA_DIR``, exactly like ``iv_history.py`` / ``burn_marks.py``:
market data, not a trading record, kept out of state.json.

Each record is the full decision trace from ``regime_genius.compute_trace`` (the
four lights, the raw vote, the dwell state, the secondary breadth/VIX indicators,
and the published regime) stamped with its trading date. The store is:
  * idempotent per day — the last write of a day replaces that day's record;
  * capped to ``config.REGIME_HISTORY_DAYS`` newest records;
  * backfillable from cached parquet bars (legitimate here — it's derived data,
    unlike the entry-context snapshots which are frozen-at-trade-time raw record).

Calibration and the entry-context snapshot read this store for full regime
provenance; ``screening.regime()`` reads the prior published series to drive the
yellow dwell, and ``maintenance.nightly_refresh`` appends today's record.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

REGIME_HISTORY_PATH = os.path.join(config.DATA_DIR, "regime_history.json")
_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fmt_day(ts) -> str:
    """A 'YYYY-MM-DD' day string from a pandas Timestamp / datetime / str."""
    try:
        return ts.strftime("%Y-%m-%d")
    except AttributeError:
        return str(ts)[:10]


def _load() -> dict:
    try:
        with open(REGIME_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"records": []}


def _save(data: dict) -> None:
    tmp = f"{REGIME_HISTORY_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, REGIME_HISTORY_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def series() -> list[dict]:
    """All stored regime records, chronological (oldest first)."""
    return list(_load()["records"])


def latest(before: str | None = None) -> dict | None:
    """The most recent record, or the most recent strictly before ``before``
    (a 'YYYY-MM-DD' day) when given — used by the change alert to compare today's
    live regime against the last persisted day."""
    recs = _load()["records"]
    if before is not None:
        recs = [r for r in recs if r.get("date", "") < before]
    return recs[-1] if recs else None


def prior_published(before: str | None = None) -> list[str]:
    """Chronological list of prior PUBLISHED regimes, for the dwell input. When
    ``before`` is given, records on/after that day are excluded so today's own
    (already-persisted) record can't double-count in its own dwell computation."""
    recs = _load()["records"]
    if before is not None:
        recs = [r for r in recs if r.get("date", "") < before]
    return [r.get("published_regime") for r in recs if r.get("published_regime")]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def record(trace: dict, day: str | None = None, *, backfilled: bool = False) -> dict:
    """Persist one day's decision trace (idempotent per day — the last write of a
    day replaces that day's record). Returns the stored record."""
    day = day or _today()
    rec = {"date": day, "backfilled": bool(backfilled), **trace}
    with _lock:
        data = _load()
        records = data["records"]
        if records and records[-1].get("date") == day:
            records[-1] = rec                      # replace today's point
        else:
            # Guard against an out-of-order insert; keep the list sorted by date.
            records.append(rec)
            records.sort(key=lambda r: r.get("date", ""))
        del records[:-config.REGIME_HISTORY_DAYS]
        _save(data)
    return rec


def record_today(*, now: datetime | None = None) -> dict | None:
    """Compute and persist today's regime trace. Called once/day by the nightly
    maintenance job after the official close is cached. Best-effort: never raises
    into the maintenance sweep. Returns the stored record (or None on failure)."""
    try:
        import screening
        day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        trace = screening.regime()  # already the full trace; status == published
        return record(trace, day)
    except Exception:  # noqa: BLE001 — a telemetry append must not sink maintenance
        return None


# ---------------------------------------------------------------------------
# Backfill (bootstrap) — derived from cached parquet bars, so legitimate here
# ---------------------------------------------------------------------------
def _breadth_asof(frames: dict, ts) -> float | None:
    """Percent of the breadth universe above its 50-DMA as of date ``ts``, from
    cached frames (best-effort; a frame missing that date is skipped)."""
    import indicators
    flags = []
    for df in frames.values():
        if df is None or df.empty:
            continue
        try:
            sub = df.loc[:ts]
        except (KeyError, TypeError):
            continue
        if sub.empty:
            continue
        flag = indicators.above_ma(sub, config.BREADTH_MA_WINDOW)
        if flag is not None:
            flags.append(flag)
    if not flags:
        return None
    return round(sum(flags) / len(flags) * 100, 1)


def backfill(force: bool = False) -> dict:
    """Bootstrap the regime history from cached SPY bars as far back as the slow
    MA can be formed. This is DERIVED data (recomputable from OHLCV), so a full
    replay is legitimate — unlike entry-context snapshots. No-ops when history is
    already present unless ``force``. The published regime is the four lights +
    dwell only, so VIX (not reliably cached historically) is left null on
    backfilled records; breadth is recomputed per-day from cached frames purely as
    the secondary indicator. Returns a summary. Best-effort — never raises."""
    try:
        import data_handler
        import regime_genius
        with _lock:
            existing = _load()["records"]
        if existing and not force:
            return {"skipped": "history present", "records": len(existing)}

        spy = data_handler.get_daily(config.GENIUS_INDEX_SYMBOL)
        if spy is None or spy.empty:
            return {"skipped": "no SPY bars", "records": 0}
        frames = data_handler.get_many(config.BREADTH_SYMBOLS)

        slow = config.GENIUS_SLOW_MA
        published: list[str] = []
        records: list[dict] = []
        # Anchor EVERY day's recompute to the EARLIEST cached bar: sub is always a
        # prefix spy.iloc[:i+1] starting at index 0, never a rolling sub-window.
        # Parabolic SAR is forward-causal ONLY across prefixes that share bar 0
        # (its seed comes from the first two bars) — a shifted start would re-seed
        # and diverge, making the published regime for a past date non-reproducible.
        # So the full-history recompute below is the determinism guarantee, pinned
        # by test_regime_regression.test_sar_is_prefix_causal_equals_full_history
        # and test_four_light_regime_prefix_equals_full_history.
        index = list(spy.index)
        for i, ts in enumerate(index):
            if (i + 1) < slow:              # skip warm-up: no slow MA yet
                continue
            sub = spy.iloc[: i + 1]
            breadth = _breadth_asof(frames, ts)
            trace = regime_genius.compute_trace(sub, breadth, None, published)
            published.append(trace["published_regime"])
            records.append({"date": _fmt_day(ts), "backfilled": True, **trace})

        records = records[-config.REGIME_HISTORY_DAYS:]
        with _lock:
            _save({"records": records})
        return {"ok": True, "records": len(records),
                "from": records[0]["date"] if records else None,
                "to": records[-1]["date"] if records else None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
