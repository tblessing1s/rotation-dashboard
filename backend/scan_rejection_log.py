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

# Record schema version. Bumped when the persisted per-symbol record gains or
# changes fields, so a calibration pass can tell which records carry which
# columns rather than inferring from absence.
#   1 — verdict + binding + score/RS/juice + shadow floor (schema v21)
#   2 — gate-recalibration shadow record: scan_id, both rulesets' verdicts and
#       their divergence, the full per-level results, the SYM light breakdown and
#       the raw ATR ratio. Writes become append-per-SCAN-RUN (see record_scan).
#   3 — Level-4 CHART-STRUCTURE shadow metrics (chart_structure): the four raw
#       values, their constructive verdicts, structure_score / _of, the
#       insufficient list, the tightness basis AND the per-basis ceiling it was
#       judged against, and the consolidation-phase flag the volume check reads. This is the calibration dataset that a future
#       "should structure block?" decision would rest on — which is exactly why
#       it is persisted per candidate per scan rather than only rendered.
SCHEMA_VERSION = 3


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
    floor = row.get("shadow_floor") or {}
    struct = row.get("structure") or {}
    return {
        "verdict": row.get("verdict"),
        "bench": bool(row.get("bench")),
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
        # --- Income profile + SHADOW floor (schema v21) -----------------------
        # The calibration dataset for the floors. They have ZERO blocking authority
        # today (scan_triggers.shadow_floor), and graduating one to blocking is a
        # future work item CONTINGENT on this logged record — which is exactly why
        # the pass/fail is persisted per candidate per day here rather than only
        # rendered. Recording changes no behavior: these are a READ of the row.
        "income_profile": row.get("income_profile"),
        "annual_dividend_yield_pct": row.get("annual_dividend_yield_pct"),
        "combined_weekly_yield_pct": row.get("combined_weekly_yield_pct"),
        "dividend_weekly_pct": row.get("dividend_weekly_pct"),
        "juice_weekly_pct": row.get("juice_weekly_pct"),
        "shadow_floor_pass": floor.get("pass"),
        "shadow_floor_pct": floor.get("floor_pct"),
        "shadow_floor_measured_pct": floor.get("measured_pct"),
        "shadow_floor_basis": floor.get("basis"),
        "shadow_floor_reasons": floor.get("reasons"),
        # --- Gate recalibration shadow record (schema 2) ----------------------
        # BOTH rulesets' verdicts and whether they diverged. The authoritative one
        # is named by `gate_ruleset`; `verdict` above always follows it. This is
        # the dataset a human reads before deciding whether to flip authority —
        # recording it grants none.
        "gate_ruleset": row.get("gate_ruleset"),
        "legacy_verdict": row.get("legacy_verdict"),
        "proposed_verdict": row.get("proposed_verdict"),
        "ruleset_divergence": bool(row.get("ruleset_divergence")),
        # The FULL conditional gate picture — every level's result, recorded even
        # past the first failure, so a later pass can ask which levels were
        # redundant. `first_failing_level` is the lowest failing level in gate
        # order; it is NOT `binding_level` above (the most DECISIVE block).
        "first_failing_level": row.get("first_failing_level"),
        "all_level_results": row.get("all_level_results"),
        # Inputs needed to re-derive the symbol vote offline, so a rule change can
        # be replayed against history without refetching bars.
        "sym_lights": row.get("sym_lights"),
        "sym_core_green": row.get("sym_core_green"),
        "sym_greens": row.get("sym_greens"),
        "atr_momentum": row.get("atr_momentum"),
        # --- Level-4 chart structure, SHADOW (schema 3) ------------------------
        # ZERO blocking authority today (chart_structure). Graduating any of these
        # to a gate check is a future work item CONTINGENT on joining these rows
        # against Travis's manual compelling / not-compelling labels
        # (structure_labels), so the raw values are persisted alongside the
        # verdict they did NOT influence. `structure_score_of` and
        # `structure_insufficient` are recorded so a partial read is never
        # mistaken for a low score. `tightness_basis` names which denominator was
        # used and `tightness_max` the ceiling it was judged against: the
        # "advance" and "atr_sum" bases are NOT on one scale and carry SEPARATE
        # thresholds, so a calibration pass must never pool or cross-compare
        # their ratios (see chart_structure.tightness).
        "structure_score": struct.get("structure_score"),
        "structure_score_of": struct.get("structure_score_of"),
        "structure_insufficient": struct.get("insufficient"),
        "dist_from_high_pct": struct.get("dist_from_high_pct"),
        "dist_from_high_252_pct": struct.get("dist_from_high_252_pct"),
        "ma21_slope": struct.get("ma21_slope"),
        "ma21_slope_state": struct.get("ma21_slope_state"),
        "tightness": struct.get("tightness"),
        "tightness_basis": struct.get("tightness_basis"),
        "tightness_max": struct.get("tightness_max"),
        "higher_lows": struct.get("higher_lows"),
        "structure_constructive": struct.get("constructive"),
        # The phase flag that decided whether the thin-volume CAUTION applied —
        # persisted with the ratio so the suppression is auditable after the fact.
        "consolidation_phase": bool(row.get("consolidation_phase")),
        "volume_ratio": row.get("volume_ratio"),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def series(ticker: str) -> list[dict]:
    """All stored scan records for one symbol, chronological (oldest first)."""
    return list(_load()["symbols"].get((ticker or "").upper(), []))


def latest_before(day: str | None = None) -> dict:
    """The newest stored record per symbol whose date is BEFORE ``day`` (default
    today) — i.e. "yesterday's scan state" for the nightly transition diff, robust
    to today's point already being appended (idempotent re-runs). {ticker: record}."""
    day = day or _today()
    out: dict[str, dict] = {}
    for ticker, recs in _load()["symbols"].items():
        prior = [r for r in recs if r.get("date", "") < day]
        if prior:
            out[ticker] = prior[-1]
    return out


def summary(window: int | None = None) -> dict:
    """A calibration-oriented rollup over the retained records: how often each
    binding constraint bound, and the READY rate — the empirical read on whether
    the gate is too strict. ``window`` bounds each symbol to its newest N records."""
    from metrics import thresholds as _T
    vol_floor = _T.VOLUME_RATIO_MIN            # read, never written — see chart_structure
    data = _load()["symbols"]
    binding_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    # Shadow-floor calibration tallies, per profile (schema v21).
    floor: dict[str, dict] = {}
    floor_reasons: dict[str, int] = {}
    # Level-4 structure calibration tallies (schema 3). The headline question the
    # shadow period exists to answer is "does structure_score separate the charts
    # Travis calls compelling from the ones he doesn't", so the rollup is a
    # CROSSTAB of score against the verdict the gate reached WITHOUT it — not a
    # bare histogram. Partial reads are counted separately; a 2-of-3 is not a 2/4.
    struct_by_score: dict[str, dict] = {}
    struct_partial = 0
    struct_measured = 0
    phase_suppressed = 0
    total = 0
    for recs in data.values():
        for rec in (recs[-window:] if window else recs):
            total += 1
            v = rec.get("verdict") or "UNKNOWN"
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
            bc = rec.get("binding_constraint")
            if bc is not None:
                binding_counts[bc] = binding_counts.get(bc, 0) + 1
            prof = rec.get("income_profile")
            passed = rec.get("shadow_floor_pass")
            if prof and passed is not None:
                agg = floor.setdefault(prof, {"pass": 0, "fail": 0})
                agg["pass" if passed else "fail"] += 1
            for reason in rec.get("shadow_floor_reasons") or []:
                floor_reasons[reason] = floor_reasons.get(reason, 0) + 1
            # Structure crosstab. Keyed "n/k" so a partial read stays visibly
            # partial instead of being pooled with a full one at the same n.
            score, of = rec.get("structure_score"), rec.get("structure_score_of")
            if score is not None and of:
                struct_measured += 1
                if of < 4:
                    struct_partial += 1
                agg = struct_by_score.setdefault(f"{score}/{of}", {})
                agg[v] = agg.get(v, 0) + 1
            # How often the phase flag actually suppressed a thin-volume CAUTION —
            # the empirical size of the volume change, per scan.
            if (rec.get("consolidation_phase") and rec.get("volume_ratio") is not None
                    and rec["volume_ratio"] < vol_floor):
                phase_suppressed += 1
    ready = verdict_counts.get("READY", 0)
    # Ruleset-divergence tally (schema 2): how often the proposed rules would have
    # reached a different verdict than the shipped ones, and in which direction.
    # The empirical basis for a human decision to flip GATE_RULESET — no more.
    diverged = 0
    divergence_pairs: dict[str, int] = {}
    for recs in data.values():
        for rec in (recs[-window:] if window else recs):
            if rec.get("legacy_verdict") is None and rec.get("proposed_verdict") is None:
                continue                       # schema 1 record — nothing to compare
            if rec.get("ruleset_divergence"):
                diverged += 1
                pair = f"{rec.get('legacy_verdict')}->{rec.get('proposed_verdict')}"
                divergence_pairs[pair] = divergence_pairs.get(pair, 0) + 1
    for agg in floor.values():
        seen = agg["pass"] + agg["fail"]
        agg["evaluated"] = seen
        agg["pass_rate"] = round(agg["pass"] / seen * 100, 1) if seen else None
    return {
        "records": total,
        "symbols": len(data),
        # SHADOW structure calibration read (schema 3): structure_score crossed
        # against the verdict reached WITHOUT it, plus how many thin-volume
        # CAUTIONs the consolidation-phase flag suppressed. Evidence for a future
        # decision; it grants nothing.
        "structure": {
            "measured": struct_measured,
            "partial_reads": struct_partial,
            "by_score": dict(sorted(struct_by_score.items())),
            "phase_suppressed_volume_cautions": phase_suppressed,
        },
        "verdict_counts": verdict_counts,
        "binding_counts": dict(sorted(binding_counts.items(),
                                      key=lambda kv: kv[1], reverse=True)),
        "ready_rate": round(ready / total * 100, 1) if total else None,
        # SHADOW-floor calibration read: how often each profile's floor would have
        # bound had it any authority. This is the evidence a future graduation
        # decision needs; it does not itself grant any.
        "shadow_floor": floor,
        "shadow_floor_reasons": dict(sorted(floor_reasons.items(),
                                            key=lambda kv: kv[1], reverse=True)),
        # Gate-recalibration divergence read (schema 2).
        "ruleset": config.GATE_RULESET,
        "ruleset_divergences": diverged,
        "ruleset_divergence_rate": (round(diverged / total * 100, 1) if total else None),
        "ruleset_divergence_pairs": dict(sorted(divergence_pairs.items(),
                                                key=lambda kv: kv[1], reverse=True)),
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _trim_to_days(recs: list[dict], max_days: int) -> list[dict]:
    """Keep only the records whose date falls in the newest ``max_days`` DISTINCT
    dates. Retention is by date, not by record count — a day may now carry more
    than one record (one per scan run), so a count-based trim would silently
    shorten the retained window whenever a day was scanned twice."""
    days = sorted({r.get("date", "") for r in recs})
    if len(days) <= max_days:
        return recs
    keep = set(days[-max_days:])
    return [r for r in recs if r.get("date", "") in keep]


def record_scan(rows: list[dict], day: str | None = None,
                max_days: int | None = None, scan_id: str | None = None) -> dict:
    """Append this scan RUN's record for every row in one load/save.

    APPEND-ONLY per scan run (schema 2): each run carries a distinct ``scan_id``
    and appends its own record, so re-running a scan never rewrites a prior one —
    the log grows monotonically within the retention window. Re-writing the SAME
    ``scan_id`` (a genuine retry of one run) is still idempotent and replaces that
    run's point, which is what keeps a partially-failed sweep from double-counting.

    This supersedes schema 1's per-DAY last-write-wins rule. Reads are unaffected:
    ``latest_before`` filters strictly on ``date <`` and still returns the newest
    prior-day record per symbol, and ``summary`` counts records either way.

    Best-effort: a malformed row is skipped, never raised, so a telemetry append
    can't sink the sweep that called it. Returns {ok, recorded, scan_id}."""
    max_days = max_days or config.SCAN_REJECTION_LOG_DAYS
    day = day or _today()
    # Omitting scan_id means "this call IS its own run" — microsecond precision so
    # two runs in the same second can never collide into one another's record.
    # The nightly sweep passes its memoized as_of instead, which is the real run
    # identity and makes a retry of that sweep genuinely idempotent.
    scan_id = scan_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        with _lock:
            data = _load()
            n = 0
            for row in rows or []:
                ticker = (row.get("ticker") or "").upper()
                if not ticker:
                    continue
                point = {"date": day, "scan_id": scan_id, "schema": SCHEMA_VERSION,
                         **_record_from_row(row)}
                recs = data["symbols"].setdefault(ticker, [])
                same_run = next((i for i, r in enumerate(recs)
                                 if r.get("scan_id") == scan_id), None)
                if same_run is not None:
                    recs[same_run] = point        # retry of THIS run — idempotent
                else:
                    recs.append(point)
                    recs.sort(key=lambda r: (r.get("date", ""), r.get("scan_id") or ""))
                data["symbols"][ticker] = _trim_to_days(recs, max_days)
                n += 1
            _save(data)
        return {"ok": True, "recorded": n, "scan_id": scan_id}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}
