"""Append-only scan transition-events log — the pipeline's audit trail + the
retrospective capture (Phase-0 Q9).

Every nightly scan diff (``scan_diff.diff``) appends its emitted events here,
timestamped, so the deferred miss-analysis can later ask "did a bench name go
READY and did we enter it" by joining these transitions against the immutable
executions ledger. DERIVED telemetry under ``DATA_DIR`` — NOT in state.json, NOT
rebuilt by ``recompute_derived``; single nightly writer; the EVENTS list is
append-only (never rewritten). A small ``snapshot`` (yesterday's occupied sectors)
rides alongside for the sector-slot-open transition detection — that one value is
last-write-wins daily state, the events never are.

Mirrors ``scan_rejection_log``'s storage discipline (atomic replace, best-effort,
never raises into its caller).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

LOG_PATH = os.path.join(config.DATA_DIR, "scan_diff_log.json")
_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            data.setdefault("snapshot", {})
            return data
    except (OSError, ValueError):
        pass
    return {"events": [], "snapshot": {}}


def _save(data: dict) -> None:
    tmp = f"{LOG_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, LOG_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_snapshot() -> dict:
    """The last persisted daily snapshot ({date, occupied_sectors}) for the
    sector-slot-open transition. {} before the first run."""
    return _load().get("snapshot") or {}


def append(events: list[dict], occupied_sectors: list[str] | None = None,
           day: str | None = None, max_events: int | None = None) -> dict:
    """Append today's transition ``events`` (each stamped with date + ts) and
    replace the occupied-sector snapshot. Append-only for events; last-write-wins
    for the snapshot. Best-effort — never raises into the nightly sweep. Returns
    {ok, appended}."""
    day = day or _today()
    max_events = max_events or config.SCAN_DIFF_LOG_MAX
    try:
        with _lock:
            data = _load()
            ts = _now_iso()
            for e in events or []:
                data["events"].append({"date": day, "ts": ts, **e})
            del data["events"][:-max_events]
            data["snapshot"] = {"date": day,
                                "occupied_sectors": sorted(set(occupied_sectors or []))}
            _save(data)
        return {"ok": True, "appended": len(events or [])}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}


def recent(limit: int = 100) -> list[dict]:
    """The newest ``limit`` transition events (newest first) for the UI / audit."""
    return list(reversed(_load()["events"]))[:limit]
