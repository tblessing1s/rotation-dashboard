"""Operator STRUCTURE_LABEL annotations — the manual half of the Level-4
chart-structure calibration dataset (TRAVIS_EXTENSION).

``chart_structure`` computes four structure metrics per scan and
``scan_rejection_log`` persists them alongside the verdict they did NOT
influence. What that dataset cannot produce on its own is the DEPENDENT
variable: whether Travis, looking at the chart, would call the spot compelling.
This store holds those labels so a future calibration pass can ask directly
whether ``structure_score`` separates the two populations — and therefore
whether any of it has earned blocking authority.

Storage discipline mirrors ``burn_marks`` / ``iv_history`` / ``scan_rejection_log``:
a standalone append-only JSON store under ``DATA_DIR``, atomic
tmp-then-``os.replace``, module lock, best-effort. Deliberately NOT in
``state.json``: that file's execution log is the append-only TRADING record and
``recompute_derived`` derives positions and ledgers from it. A subjective
compelling / not-compelling label is telemetry, not a trading fact — putting it
there would hand ``recompute_derived`` an event type it has no derivation for.
``burn_marks`` states the same rule for the same reason.

Labels are OBSERVATIONS. Nothing here is read by the verdict, the gate, the
executor, sizing, ranking or the recommendation pipeline — there is no consumer
outside the calibration read. In particular a label NEVER edits, relabels or
re-derives the historical verdict it annotates: the scan record and the human
opinion of it are two separate rows joined by ``scan_id``, which is what keeps
this from becoming an auto-remediation path.

PURE except for the file I/O and the "today" stamp.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

LABELS_PATH = os.path.join(config.DATA_DIR, "structure_labels.json")
_lock = threading.RLock()

# The label vocabulary. Deliberately three-valued: forcing a binary call on a
# chart the operator is genuinely unsure about would poison the calibration set
# with noise that looks like signal.
COMPELLING = "COMPELLING"
NOT_COMPELLING = "NOT_COMPELLING"
UNSURE = "UNSURE"
LABELS = (COMPELLING, NOT_COMPELLING, UNSURE)

_MAX_PER_TICKER = 500          # generous — labels are hand-entered, not swept
_NOTE_MAX = 500                # chars; a note is context, not an essay


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        with open(LABELS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("tickers"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"tickers": {}}


def _save(data: dict) -> None:
    tmp = f"{LABELS_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, LABELS_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def normalize_label(label: str | None) -> str | None:
    """The canonical label for free-ish input, or None if it isn't one of the
    three. Accepts the obvious shorthands so a curl call doesn't need the exact
    enum: yes/no/y/n/good/bad/1/0."""
    if not label:
        return None
    key = str(label).strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "YES": COMPELLING, "Y": COMPELLING, "GOOD": COMPELLING, "1": COMPELLING,
        "NO": NOT_COMPELLING, "N": NOT_COMPELLING, "BAD": NOT_COMPELLING,
        "0": NOT_COMPELLING, "NOT": NOT_COMPELLING,
        "MAYBE": UNSURE, "?": UNSURE,
    }
    if key in LABELS:
        return key
    return aliases.get(key)


def record_label(ticker: str, label: str, *, scan_id: str | None = None,
                 verdict: str | None = None, structure_score: int | None = None,
                 structure_score_of: int | None = None, note: str | None = None,
                 day: str | None = None) -> dict:
    """Append one operator label. APPEND-ONLY: a second label for the same ticker
    and ``scan_id`` is appended as its own row rather than replacing the first, so
    a changed mind is visible as a change of mind. Nothing is ever rewritten.

    ``scan_id`` is the join key back to the ``scan_rejection_log`` record this
    label is about (that log stamps every row with the sweep's ``as_of``). It is
    optional — a label with no scan_id still carries its date — but supplying it
    is what lets a calibration pass line the label up against the exact metric
    values that were computed at the time.

    ``verdict`` / ``structure_score`` / ``structure_score_of`` are copied in as
    provenance so the label row is self-describing even if the scan log has since
    rolled past its retention window.

    Returns {ok, ...} and never raises: a telemetry append must not sink its
    caller."""
    canon = normalize_label(label)
    if canon is None:
        return {"ok": False, "error": f"unknown label {label!r}; expected one of {list(LABELS)}"}
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ok": False, "error": "ticker is required"}
    entry = {
        "date": day or _today(),
        "at": _now_iso(),
        "label": canon,
        "scan_id": scan_id,
        "verdict": verdict,
        "structure_score": structure_score,
        "structure_score_of": structure_score_of,
        "note": (str(note)[:_NOTE_MAX] if note else None),
    }
    try:
        with _lock:
            data = _load()
            rows = data["tickers"].setdefault(ticker, [])
            rows.append(entry)
            data["tickers"][ticker] = rows[-_MAX_PER_TICKER:]
            _save(data)
        return {"ok": True, "ticker": ticker, "recorded": entry}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}


def series(ticker: str) -> list[dict]:
    """Every label recorded for one ticker, oldest first."""
    return list(_load()["tickers"].get((ticker or "").strip().upper(), []))


def recent(limit: int = 100) -> list[dict]:
    """The newest labels across all tickers, newest first."""
    rows = [{"ticker": t, **row}
            for t, rows_ in _load()["tickers"].items() for row in rows_]
    rows.sort(key=lambda r: (r.get("at") or "", r.get("ticker") or ""), reverse=True)
    return rows[:max(0, limit)]


def summary() -> dict:
    """The calibration read: labels crossed against the ``structure_score`` that
    was computed for the same row. This is the whole point of the store — it is
    the table a human reads before deciding whether structure has earned any
    authority, and it is EVIDENCE, not a decision.

    ``by_score`` is keyed "n/k" so a partial metric read is never pooled with a
    full one, matching ``scan_rejection_log.summary``."""
    data = _load()["tickers"]
    label_counts: dict[str, int] = {}
    by_score: dict[str, dict] = {}
    total = 0
    for rows in data.values():
        for row in rows:
            total += 1
            lab = row.get("label") or "UNKNOWN"
            label_counts[lab] = label_counts.get(lab, 0) + 1
            score, of = row.get("structure_score"), row.get("structure_score_of")
            if score is not None and of:
                agg = by_score.setdefault(f"{score}/{of}", {})
                agg[lab] = agg.get(lab, 0) + 1
    return {
        "labels": total,
        "tickers": len(data),
        "label_counts": label_counts,
        "by_score": dict(sorted(by_score.items())),
        "vocabulary": list(LABELS),
    }
