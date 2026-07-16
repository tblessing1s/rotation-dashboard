"""Scan rejection-reason log — the empirical calibration dataset.

`verdict_reasons` exists per row at render time; what was missing is the PERSISTED
record of it over time. Each scan run logs, per symbol: the verdict, the BINDING
CONSTRAINT (the single first-failing check — a READ of the already-computed worst
input, never a re-evaluation), the shadow SCORE (+ its parts), the two-speed RS
state (+ raw level/slope), and net juice/week. Plus the structure enums, SYM color,
sector RS1M and IVR — so one future calibration pass can jointly evaluate:

  * "is the gate too strict" — the distribution of binding constraints over time,
  * the Level-2 RS1M-vs-RS3M choice (sector_rs1m recorded alongside the binding),
  * structure thresholds (base_stage / inst_flow that drove each read),
  * SCORE weights (score + score_parts, for sensitivity),
  * RS-slope graduation to blocking (rs_state + raw level/slope over time).

Like ``symbol_genius_history`` / ``regime_history`` / ``iv_history`` this is DERIVED
telemetry (recomputable from cached bars + the pure classifiers), kept in a
standalone append-only store under ``DATA_DIR`` — NOT in state.json and NOT rebuilt
by ``recompute_derived`` (which keys off the executions ledger). One record per
symbol per trading day (idempotent per day — the last write of the day wins). The
nightly maintenance sweep appends today's full-universe scan.

Recording changes NO behavior — it observes the canonical verdict + shadow signals
exactly as computed. Pure storage; no clock beyond "today", no provider calls.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

LOG_PATH = os.path.join(config.DATA_DIR, "scan_rejection_log.json")
_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"symbols": {}}


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


# ---------------------------------------------------------------------------
# Pure extraction — the binding constraint is a READ of the first (worst) reason.
# ---------------------------------------------------------------------------
def binding_constraint(row: dict) -> str | None:
    """The single first-failing check for a row — the first entry of the canonical
    verdict's ``reasons`` (compose_verdict already ordered them worst-first). None
    for a READY row. This is a READ of the already-computed reasons, NOT a
    re-evaluation of the gate."""
    if row.get("verdict") == "READY":
        return None
    reasons = row.get("verdict_reasons") or []
    # Skip the informational RS TURNING annotation (appended after the real inputs);
    # the binding constraint is the first genuine verdict input.
    for r in reasons:
        if not str(r).startswith("rs:"):
            return r
    return reasons[0] if reasons else None


def _record_from_row(row: dict) -> dict:
    """The persisted fields for one scan row (compact but calibration-sufficient)."""
    binding = row.get("binding") or {}
    return {
        "verdict": row.get("verdict"),
        "binding_constraint": binding_constraint(row),
        # Structured binding (Phase-0 Q9 capture): the level / check id / trigger
        # kind of the first-failing gate check, so the deferred miss-analysis can
        # ask "did L4-blocked names resolve into entries we missed" without a
        # string parse. None on a READY row.
        "binding_level": binding.get("level"),
        "binding_check": binding.get("id"),
        "binding_kind": binding.get("kind"),
        # Spot price at the scan (Phase-0 Q9): the forward-return anchor — a later
        # pass joins subsequent price against this to answer "did the skipped name
        # keep rising". Nothing recovers it after the fact, so capture it now.
        "price": row.get("price"),
        "score": row.get("score"),
        "score_parts": row.get("score_parts"),
        "rs_state": row.get("rs_state"),
        "rs_level": row.get("rs_level"),
        "rs_slope": row.get("rs_slope"),
        "net_juice_weekly_pct": row.get("net_juice_weekly_pct"),
        # Extra provenance for the structure / Level-2 / IVR calibration questions.
        "base_stage": row.get("base_stage"),
        "inst_flow": row.get("inst_flow"),
        "sym": row.get("sym"),
        "sector_rs1m": row.get("sector_rs1m"),
        "iv_rank": row.get("iv_rank"),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def series(ticker: str) -> list[dict]:
    """All stored scan records for one symbol, chronological (oldest first)."""
    return list(_load()["symbols"].get((ticker or "").upper(), []))


def summary(window: int | None = None) -> dict:
    """A calibration-oriented rollup over the retained records: how often each
    binding constraint bound, and the READY rate — the empirical read on whether
    the gate is too strict. ``window`` bounds each symbol to its newest N records."""
    data = _load()["symbols"]
    binding_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    total = 0
    for recs in data.values():
        for rec in (recs[-window:] if window else recs):
            total += 1
            v = rec.get("verdict") or "UNKNOWN"
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
            bc = rec.get("binding_constraint")
            if bc is not None:
                binding_counts[bc] = binding_counts.get(bc, 0) + 1
    ready = verdict_counts.get("READY", 0)
    return {
        "records": total,
        "symbols": len(data),
        "verdict_counts": verdict_counts,
        "binding_counts": dict(sorted(binding_counts.items(),
                                      key=lambda kv: kv[1], reverse=True)),
        "ready_rate": round(ready / total * 100, 1) if total else None,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def record_scan(rows: list[dict], day: str | None = None,
                max_days: int | None = None) -> dict:
    """Append today's scan record for every row in one load/save. Idempotent per
    day (the last write of a day replaces that day's point per symbol). Best-effort:
    a malformed row is skipped, never raised, so a telemetry append can't sink the
    sweep that called it. Returns {ok, recorded}."""
    max_days = max_days or config.SCAN_REJECTION_LOG_DAYS
    day = day or _today()
    try:
        with _lock:
            data = _load()
            n = 0
            for row in rows or []:
                ticker = (row.get("ticker") or "").upper()
                if not ticker:
                    continue
                point = {"date": day, **_record_from_row(row)}
                recs = data["symbols"].setdefault(ticker, [])
                if recs and recs[-1].get("date") == day:
                    recs[-1] = point
                else:
                    recs.append(point)
                    recs.sort(key=lambda r: r.get("date", ""))
                del recs[:-max_days]
                n += 1
            _save(data)
        return {"ok": True, "recorded": n}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}
