"""Per-candidate, per-gate evaluation telemetry — the calibration instrument.

The Genius regime has been GREEN for extended stretches while effectively zero
candidates cleared the full gate stack. Two hypotheses are indistinguishable
without data:

  * H1 — the gates are correctly rejecting a narrow-leadership market;
  * H2 — one or more gates are over-tight and the stack has drifted into
    permanent-veto territory.

The discriminating statistic is the **SOLE-BLOCKER RATE**: for each gate, the
fraction of evaluated candidates where that gate failed and every OTHER
veto-authority gate passed. A high block rate with a LOW sole-blocker rate means
the gate co-fires with genuinely bad setups (H1). A high SOLE-blocker rate means
the gate is the binding constraint on the whole system (H2, and the first place
to look).

ZERO AUTHORITY
--------------
This module is READ-ONLY OBSERVABILITY, on the same contract
``scan_triggers.shadow_floor`` / ``chart_structure`` / ``juice_capacity`` carry.
It adds no blocking authority, alters no threshold, and changes no gate logic or
ordering. Nothing here is ever appended to the ``blocks`` list that feeds
``scan_triggers.compose_row_verdict`` — that list is what carries verdict
authority. ``build_results`` is a pure READ of values the gate already computed;
it never re-evaluates a gate.

Emission is a pure SIDE CHANNEL: ``record_scan`` swallows everything, so a
telemetry failure can never alter, block, or fail a scan or a recommendation.

STORAGE — one file per scan DAY
-------------------------------
``DATA_DIR/gate_telemetry_log/YYYY-MM-DD.json``, append-only within a day, one
object per scan RUN. Deliberately NOT the single-JSON shape its siblings
(``scan_rejection_log``, ``juice_capacity``) use: in a single file every nightly
append would load, re-serialize and rewrite the entire retention window, every
night, to add one day. Per-day files keep each write proportional to ONE day and
let a range read stream day by day without ever holding the window in memory.
Retention is a file unlink (``prune``), not a rewrite.

COMPACTED (schema 2). The gate identity — ``gate_id``, ``level``, ``authority``,
``label``, ``direction``, and ``threshold`` where it is constant across the run —
is written ONCE per run in the ``gates`` manifest, and each candidate's results
are a POSITIONAL row against it:

    gates:      [ {gate_id: "L4:extension", authority: "veto", threshold: 1.5,
                   direction: "lower", ...}, ... ]          # once per run
    candidates: [ {s: "GDDY", a: 0, r: [[1, 2.71], [0, 1.94], null, ...]} ]

Each cell is ``[passed, value]``, or ``[passed, value, threshold]`` for a gate
whose threshold varies per candidate (``L3:veto:close_below_ma200``, whose bar IS
that name's MA200). A ``null`` cell means the gate was ABSENT for that candidate
— distinct from a present gate that produced no verdict, which is
``[null, value]``. The manifest is the UNION of gates seen across the run, in
canonical (level, gate_id) order, so a run whose candidates carry different gate
sets still aligns.

Measured at the current 522-name universe, on high-entropy values (real
indicator floats, not round test numbers): **~0.16 MB per run**, one run/day —
~0.06 GB/yr and **~20 MB at the 120-day retention window**, against ~1.23 MB/run
and ~147 MB for the same information written inline. An 86% cut, and retention
is now cheap enough that widening the window is a config edit rather than a
capacity decision (180 days is ~30 MB).

Nothing reads the positional form directly. ``decoded_candidates`` and ``events``
reconstruct the fully-typed schema-1 shape, so the aggregation, the endpoint and
every test work against typed results and never against array offsets. Schema-1
runs written before the compaction are still read transparently.

Never in ``state.json``; never rebuilt by ``recompute_derived`` (which keys off
the executions ledger). Absence of history is a FACT the reader reports, never a
gap to fill — nothing here backfills, imputes, or synthesizes a period that was
not recorded.

AUTHORITY IS CARRIED, NOT LOOKED UP
-----------------------------------
Each run writes its own ``gates`` manifest with the authority and threshold in
force AT WRITE TIME. A gate that graduates out of shadow mode later must not
retroactively rewrite the rows that were recorded while it had none, so the
aggregation reads authority off the record and never off live ``config``.

SHORT-CIRCUITING
----------------
``screening.entry_gate`` evaluates Levels 1-4 unconditionally — stop-on-first-
fail governs only the ``cleared_level`` derivation — so the sole-blocker rate is
computable for every L1-L4 gate. **Level 5 is different.** It is a separate
account overlay (``account_gate.evaluate``) that ``/api/scan/ready`` runs ONLY
over rows already verdict-READY, so an L5 gate is never evaluated for a
candidate that failed anything earlier. Its sole-blocker rate is not merely
unknown, it is undefined. Those gates are recorded in ``unevaluated_gates`` and
the aggregation returns an explicit ``indeterminate`` marker for them. It never
imputes a pass, and never silently drops them from the table.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta, timezone

import config

# Named `_log` so the store DIRECTORY can never collide with this MODULE:
# DATA_DIR is `backend/` locally, and a `gate_telemetry/` directory beside
# `gate_telemetry.py` would sit there as a namespace-package candidate. The
# module wins that race today, but relying on import precedence for a data
# path is not worth the ambiguity. Matches the sibling naming
# (scan_rejection_log, scan_diff_log).
STORE_DIR = os.path.join(config.DATA_DIR, "gate_telemetry_log")
_lock = threading.RLock()

# Record schema version. Bumped when the persisted per-candidate event gains or
# changes fields, so a calibration pass can tell which rows carry which columns
# rather than inferring from absence (the discipline scan_rejection_log sets).
#   1 — initial: per-candidate results for the L1-L4 veto stack + the shadow
#       income floor, as fully-typed dicts inline on every candidate, plus the
#       Level-5 unevaluated-gate registry.
#   2 — COMPACTED. The repeated gate_id / level / authority / label / direction
#       (and threshold, where it is constant across the run) are hoisted into a
#       per-run `gates` MANIFEST, and each candidate's results become a
#       positional row against it. ~60% smaller on disk for identical
#       information; `decoded_candidates` / `events` reconstruct the schema-1
#       typed shape, and both schemas are read transparently.
SCHEMA_VERSION = 2

# Authority vocabulary. `veto` gates are the ONLY ones counted in the
# "every other gate passed" test that defines the sole-blocker rate — a shadow
# metric flagging is not a block, and getting that wrong inverts the diagnostic.
VETO = "veto"
RANK = "rank"
SHADOW = "shadow"

# Comparison direction for a numeric gate, for near-miss sign handling:
#   LOWER  — the value must stay at or below the threshold (fails when higher)
#   HIGHER — the value must stay at or above the threshold (fails when lower)
LOWER = "lower"
HIGHER = "higher"


# ---------------------------------------------------------------------------
# Level-5 registry — gates that exist but are NEVER evaluated in a scan sweep.
#
# `screening.entry_gate` records L5 as {"pass": None, "note": "not_evaluated"}
# because evaluating it per swept name would mean a state load + live cash
# resolution per name; `/api/scan/ready` then runs it only over already-READY
# rows. Both cut-offs are real and neither is a bug, so these gates are named
# here with the authority they carry, and the aggregation reports them as
# `indeterminate` rather than pretending they passed.
# ---------------------------------------------------------------------------
UNEVALUATED_GATES = (
    {"gate_id": "L5:cash_reserve", "level": 5, "authority": VETO,
     "label": "Post-trade cash >= ATR reserve", "reason": "not_evaluated_in_scan"},
    {"gate_id": "L5:position_limit", "level": 5, "authority": VETO,
     "label": "Concurrent CFM position cap", "reason": "not_evaluated_in_scan"},
    {"gate_id": "L5:capital_limit", "level": 5, "authority": VETO,
     "label": "Deployed-capital cap", "reason": "not_evaluated_in_scan"},
    {"gate_id": "L5:sector_concentration", "level": 5, "authority": VETO,
     "label": "One position per sector", "reason": "not_evaluated_in_scan"},
    {"gate_id": "L5:earnings_in_cycle", "level": 5, "authority": VETO,
     "label": "No earnings inside the planned cycle", "reason": "not_evaluated_in_scan"},
    # Appended only when position_type == SHARES (account_gate.evaluate); the
    # bulk scan path (evaluate_many) never passes one, so it is absent from every
    # scan-path L5 evaluation, not merely unevaluated.
    {"gate_id": "L5:round_lot_size", "level": 5, "authority": VETO,
     "label": "100-share lot within the per-position cap",
     "reason": "absent_from_scan_path"},
    {"gate_id": "L5:juice_rich", "level": 5, "authority": RANK,
     "label": "Juice not far above history-implied", "reason": "not_evaluated_in_scan"},
    {"gate_id": "L5:ex_div_in_cycle", "level": 5, "authority": RANK,
     "label": "No ex-dividend inside the planned cycle", "reason": "not_evaluated_in_scan"},
)


def _juice_adequacy_entry() -> dict:
    """The Level-5 juice-adequacy gate's authority AT WRITE TIME.

    `account_gate.evaluate` sets its blocking flag to `not shares_mode`, and
    shares mode follows `config.LEGACY_LEAP_READONLY` — so today it is SHADOW
    (the LEAP-cost yield bar it was calibrated on is several-fold above a
    covered-call yield). Read here rather than hardcoded precisely because that
    is the kind of authority that changes, and a row must retain the authority in
    force when it was written."""
    return {"gate_id": "L5:juice_adequacy", "level": 5,
            "authority": SHADOW if config.LEGACY_LEAP_READONLY else VETO,
            "label": "Weekly juice vs the profile bar",
            "reason": "not_evaluated_in_scan"}


def _juice_floor_entry() -> dict:
    """The canonical NET juice-viability veto's state AT WRITE TIME.

    `scan_triggers.juice_floor_block` returns None unconditionally under
    `config.LEGACY_LEAP_READONLY`: both its tiers are LEAP-denominated and shares
    have no LEAP burn. It is a declared veto that cannot fire, which is a very
    different fact from a veto that never binds — recorded as such so a 0% block
    rate is never read as evidence that juice is not a constraint. The live
    share-denominated income constraint is `shadow:income_floor` below, which has
    zero authority."""
    return {"gate_id": "L5:juice_floor", "level": 5, "authority": VETO,
            "label": "NET juice/wk viability floor (LEAP-denominated)",
            "reason": "inactive_shares_mode" if config.LEGACY_LEAP_READONLY
                      else "not_evaluated_in_scan"}


def unevaluated_gates() -> list[dict]:
    """The full not-evaluated-in-scan registry for THIS run, authority included."""
    return [dict(g) for g in UNEVALUATED_GATES] + [_juice_adequacy_entry(),
                                                   _juice_floor_entry()]


# ---------------------------------------------------------------------------
# Result construction — a pure READ of the gate's already-computed values.
# ---------------------------------------------------------------------------
def _result(gate_id: str, level, authority: str, passed, *, value=None,
            threshold=None, direction=None, label: str = "") -> dict:
    """One gate's evaluation. `passed` is None ONLY when the gate could not be
    evaluated at all; a gate that ran and failed is False."""
    return {"gate_id": gate_id, "level": level, "authority": authority,
            "label": label, "passed": None if passed is None else bool(passed),
            "value": value, "threshold": threshold, "direction": direction}


def _num(x):
    """A finite float, or None. Guards against NaN/inf reaching the store."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def build_results(gate: dict | None, row: dict | None = None,
                  ruleset: str | None = None) -> list[dict]:
    """Every evaluated gate for one candidate, as structured results. PURE.

    A READ of ``screening.entry_gate``'s already-computed level details plus the
    row's shadow income floor — it never re-runs a gate, never fetches, and never
    reads a clock. The one derivation is the Level-3 SYM vote in ISOLATION (see
    below), which is a pure function call over values the gate already produced.

    Levels 1-4 are evaluated unconditionally by ``entry_gate``, so every gate
    below carries a real pass/fail even for a candidate that failed at Level 1 —
    which is exactly what makes the sole-blocker rate computable.
    """
    if not gate:
        return []
    ruleset = ruleset or gate.get("ruleset") or config.GATE_RULESET
    by_level = {lv.get("level"): lv for lv in (gate.get("levels") or [])}
    out: list[dict] = []

    # ---- Level 1 — market regime ------------------------------------------
    # The level's pass is the dwell-adjusted PUBLISHED regime, NOT the four-light
    # vote (screening.py: "the level does NOT require all four green"). The four
    # lights are therefore deliberately NOT emitted as gates: their block rate is
    # not this gate's block rate, and emitting them would invite exactly that
    # misreading. Non-numeric — a colour has no near-miss distance.
    l1 = by_level.get(1)
    if l1 is not None:
        reg = l1.get("detail") or {}
        published = reg.get("published_regime") or reg.get("status")
        out.append(_result("L1:regime_green", 1, VETO, l1.get("pass"),
                           value=published, threshold="green",
                           label="Market regime green"))

    # ---- Level 2 — sector not deteriorating -------------------------------
    # Three independent vetoes; the level fails if ANY fires. `detail` is the
    # sector row itself, so the raw numerics are in hand.
    l2 = by_level.get(2)
    if l2 is not None:
        det = l2.get("detail") or {}
        reasons = set(det.get("deteriorating_reasons") or [])
        rs1m, breadth = _num(det.get("rs1m")), _num(det.get("breadth"))
        out.append(_result("L2:rs1m_negative", 2, VETO,
                           "rs1m_negative" not in reasons,
                           value=rs1m, threshold=0.0, direction=HIGHER,
                           label="Sector RS1M vs SPY not negative"))
        out.append(_result("L2:breadth_collapsing", 2, VETO,
                           "breadth_collapsing" not in reasons,
                           value=breadth, threshold=config.SECTOR_BREADTH_COLLAPSE,
                           direction=HIGHER, label="Sector breadth not collapsing"))
        out.append(_result("L2:under_distribution", 2, VETO,
                           "under_distribution" not in reasons,
                           value=det.get("inst_flow"), threshold="DISTRIBUTING",
                           label="Sector not under distribution"))

    # ---- Level 3 — stock lights -------------------------------------------
    # The level's pass folds THREE independent things together: the light vote,
    # the two entry vetoes, and insufficient history. Emitted separately, because
    # a sole-blocker rate over a gate that is really three gates is meaningless —
    # and because the two vetoes are not in `checks` at all (only in `detail`),
    # so nothing downstream could otherwise see them.
    l3 = by_level.get(3)
    if l3 is not None:
        det = l3.get("detail") or {}
        greens = det.get("greens")
        insufficient = bool(det.get("insufficient"))
        # The vote IN ISOLATION — vetoes held out so the vote gate and the veto
        # gates are independent and a sole-block is attributable. A pure call
        # into the same dispatch the gate used (stock_lights.verdict_for), over
        # values the gate already computed: a READ, not a re-evaluation.
        vote_pass = None
        if greens is not None:
            import stock_lights
            vote_pass = stock_lights.verdict_for(
                int(greens), insufficient, False,
                core_green=bool(det.get("core_green")),
                ruleset=ruleset) == stock_lights.GREEN
        # Threshold is the ruleset's green bar: legacy 4/4, proposed the
        # mandatory-core N-of-4 count.
        vote_floor = (config.SYM_MIN_GREEN_LIGHTS
                      if ruleset == config.RULESET_PROPOSED else 4)
        out.append(_result("L3:sym_vote", 3, VETO, vote_pass,
                           value=None if greens is None else float(greens),
                           threshold=float(vote_floor), direction=HIGHER,
                           label="Symbol Genius light vote"))

        vetoes = {v.get("id"): v for v in (det.get("vetoes") or [])}
        v1 = vetoes.get("atr_expanding_high_ivr")
        if v1 is not None:
            val = v1.get("value") or {}
            # Only trips when ATR is expanding AND IVR percentile is at/above the
            # bar, so the IVR-vs-bar comparison is the meaningful one over
            # FAILURES (which is the only population near-miss is computed over).
            out.append(_result("L3:veto:atr_expanding_high_ivr", 3, VETO,
                               not v1.get("tripped"),
                               value=_num(val.get("ivr_percentile")),
                               threshold=_num(val.get("ivr_min")),
                               direction=LOWER,
                               label="ATR expanding into rich IV"))
        v2 = vetoes.get("close_below_ma200")
        if v2 is not None:
            val = v2.get("value") or {}
            out.append(_result("L3:veto:close_below_ma200", 3, VETO,
                               not v2.get("tripped"),
                               value=_num(val.get("close")),
                               threshold=_num(val.get("ma200")),
                               direction=HIGHER, label="Close above MA200"))

    # ---- Level 3.5 — structure entrability --------------------------------
    l35 = by_level.get(3.5)
    if l35 is not None:
        det = l35.get("detail") or {}
        out.append(_result("L3.5:structure_entrable", 3.5, VETO, l35.get("pass"),
                           value=det.get("entrability"),
                           threshold="READY|CAUTION",
                           label="Structure entrable"))

    # ---- Level 4 — right spot ---------------------------------------------
    # Read from the RULESET's own right-spot replay when one is named, matching
    # scan_triggers.gate_blocks: only the ATR-momentum ceiling differs, and both
    # rulesets were already computed upstream in stock_lights.compute.
    l4 = by_level.get(4)
    if l4 is not None:
        det = l4.get("detail") or {}
        alt = (det.get("right_spot_by_ruleset") or {}).get(ruleset)
        spot = alt if alt is not None else (det.get("right_spot") or {})
        checks = {c.get("id"): c for c in (spot.get("checks") or [])}
        import stock_lights
        l4_thresholds = {
            "atr_pct": config.CONSOLIDATION_ATR_PCT_MAX,
            "atr_5d_ema": stock_lights.atr_momentum_max(ruleset),
            "extension": config.SPOT_ATR_EXTENSION_MAX,
        }
        l4_labels = {"atr_pct": "ATR% of price within range",
                     "atr_5d_ema": "ATR contracting or flat",
                     "extension": "Not extended above MA21"}
        for cid, thr in l4_thresholds.items():
            c = checks.get(cid)
            if c is None:
                continue
            out.append(_result(f"L4:{cid}", 4, VETO, c.get("pass"),
                               value=_num(c.get("value")), threshold=float(thr),
                               direction=LOWER, label=l4_labels[cid]))

    # ---- Shadow income floor ----------------------------------------------
    # The share-denominated replacement for the (inactive) LEAP juice floor. It
    # is the live income constraint in shares mode and carries ZERO authority, so
    # it is recorded with authority=shadow and is excluded from the sole-blocker
    # test by construction. `pass: None` means unpriceable, never a failure.
    floor = (row or {}).get("shadow_floor") or {}
    if floor:
        out.append(_result("shadow:income_floor", 5, SHADOW, floor.get("pass"),
                           value=_num(floor.get("measured_pct")),
                           threshold=_num(floor.get("floor_pct")),
                           direction=HIGHER,
                           label=f"Income floor ({floor.get('basis') or 'juice'})"))
    return out


def admitted(results: list[dict]) -> bool:
    """Did the candidate clear every EVALUATED veto gate? Shadow and rank results
    are excluded by construction; an unevaluated veto (`passed is None`) is not
    treated as a pass, so admission never rests on a gate that did not run."""
    veto = [r for r in results if r.get("authority") == VETO]
    return bool(veto) and all(r.get("passed") is True for r in veto)


# ---------------------------------------------------------------------------
# Wire codec (schema 2) — a per-run gate MANIFEST plus positional candidate rows.
#
# The gate identity repeats identically for every candidate in a run, so writing
# it ~520 times per file was most of the store. Hoisting it cuts the day file by
# ~60% for byte-identical information. Nothing downstream sees the positional
# form: `decoded_candidates` reconstructs the typed schema-1 result dicts, so the
# aggregation and every test work against names, never offsets.
#
# Authority still travels ON THE RECORD — it just lives in the run's own manifest
# instead of being repeated per candidate, which satisfies the same requirement:
# a gate that graduates out of shadow mode cannot rewrite the runs recorded while
# it had none, because each run carries the authority in force when it was
# written.
# ---------------------------------------------------------------------------
def _level_key(level) -> float:
    """Sort key for a gate level (1, 2, 3, 3.5, 4, 5 — or None, which sorts last)."""
    try:
        return float(level)
    except (TypeError, ValueError):
        return 99.0


def _encode_run(per_candidate: list[tuple]) -> tuple[list[dict], list[dict]]:
    """(gates manifest, positional candidate rows) for one run.

    ``per_candidate`` is [(symbol, admitted, results)]. The manifest is the UNION
    of gate ids across the run in canonical (level, gate_id) order, so candidates
    carrying different gate sets still align — a candidate simply has a ``null``
    cell where a gate was absent for it.
    """
    meta: dict[str, dict] = {}
    seen_thresholds: dict[str, list] = {}
    for _sym, _adm, results in per_candidate:
        for r in results:
            gid = r.get("gate_id")
            if not gid:
                continue
            if gid not in meta:
                meta[gid] = {"gate_id": gid, "level": r.get("level"),
                             "authority": r.get("authority"),
                             "label": r.get("label") or gid,
                             "direction": r.get("direction")}
            seen_thresholds.setdefault(gid, []).append(r.get("threshold"))

    order = sorted(meta, key=lambda g: (_level_key(meta[g]["level"]), g))
    gates: list[dict] = []
    varies: dict[str, bool] = {}
    for gid in order:
        vals = seen_thresholds[gid]
        first = vals[0]
        # A threshold that is the same for every candidate (a config constant)
        # lives in the manifest; one that is per-name (close_below_ma200's bar IS
        # that name's MA200) stays on the cell.
        constant = all(v == first for v in vals)
        varies[gid] = not constant
        gates.append({**meta[gid],
                      "threshold": first if constant else None,
                      "threshold_varies": not constant})

    idx = {gid: i for i, gid in enumerate(order)}
    rows: list[dict] = []
    for symbol, adm, results in per_candidate:
        cells: list = [None] * len(order)
        for r in results:
            gid = r.get("gate_id")
            i = idx.get(gid)
            if i is None:
                continue
            passed = r.get("passed")
            cell = [None if passed is None else (1 if passed else 0), r.get("value")]
            if varies[gid]:
                cell.append(r.get("threshold"))
            cells[i] = cell
        rows.append({"s": symbol, "a": 1 if adm else 0, "r": cells})
    return gates, rows


def decoded_candidates(run: dict):
    """Stream (symbol, overall_admitted, results) for one run, results as fully
    typed dicts — the schema-1 shape, whichever schema is on disk.

    This is the ONLY place the positional encoding is understood. Schema-1 runs
    (typed dicts inline, written before the compaction) are yielded unchanged, so
    a store spanning the change reads cleanly rather than erroring.
    """
    gates = run.get("gates")
    if gates is None:                       # schema 1 — typed dicts inline
        for c in run.get("candidates") or []:
            yield (c.get("symbol"), bool(c.get("overall_admitted")),
                   list(c.get("results") or []))
        return
    for c in run.get("candidates") or []:
        cells = c.get("r") or []
        results = []
        for i, g in enumerate(gates):
            cell = cells[i] if i < len(cells) else None
            if cell is None:
                continue                    # gate ABSENT for this candidate
            passed = cell[0] if cell else None
            results.append({
                "gate_id": g.get("gate_id"),
                "level": g.get("level"),
                "authority": g.get("authority"),
                "label": g.get("label") or g.get("gate_id"),
                "passed": None if passed is None else bool(passed),
                "value": cell[1] if len(cell) > 1 else None,
                # A per-candidate threshold wins; otherwise the run's constant.
                "threshold": cell[2] if len(cell) > 2 else g.get("threshold"),
                "direction": g.get("direction"),
            })
        yield c.get("s"), bool(c.get("a")), results


def events(start: str | None = None, end: str | None = None,
           gate_ruleset: str | None = None):
    """Stream the fully-typed gate-evaluation EVENT per candidate per scan —
    ``{scan_run_id, evaluated_at, symbol, gate_ruleset, results, overall_admitted,
    schema_version}`` — regardless of how it is packed on disk.

    The typed event is the contract; the positional encoding is an implementation
    detail of the store. Exposed for offline calibration work that wants the raw
    stream rather than the rollup."""
    for _day, run in iter_runs(start, end, gate_ruleset):
        for symbol, adm, results in decoded_candidates(run):
            yield {
                "scan_run_id": run.get("scan_run_id"),
                "evaluated_at": run.get("evaluated_at"),
                "symbol": symbol,
                "gate_ruleset": run.get("gate_ruleset"),
                "results": results,
                "overall_admitted": adm,
                "schema_version": run.get("schema_version"),
            }


# ---------------------------------------------------------------------------
# Storage — one file per scan DAY, one object per scan RUN inside it.
# ---------------------------------------------------------------------------
def _day_path(day: str) -> str:
    return os.path.join(STORE_DIR, f"{day}.json")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_day(day: str) -> dict:
    try:
        with open(_day_path(day), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("runs"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"date": day, "schema": SCHEMA_VERSION, "runs": []}


def _save_day(day: str, data: dict) -> None:
    tmp = f"{_day_path(day)}.tmp.{os.getpid()}"
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _day_path(day))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def stored_days() -> list[str]:
    """Every recorded scan day, ascending. Empty when nothing has been recorded —
    which the reader reports as absence, never as zero."""
    try:
        names = os.listdir(STORE_DIR)
    except OSError:
        return []
    days = []
    for n in names:
        if not n.endswith(".json") or ".tmp." in n:
            continue
        stem = n[:-5]
        try:
            date.fromisoformat(stem)
        except ValueError:
            continue
        days.append(stem)
    return sorted(days)


def prune(max_days: int | None = None) -> int:
    """Drop day files outside the retention window. Retention is a file unlink,
    never a rewrite — nothing inside a retained day is ever mutated. Returns the
    number of days removed."""
    max_days = max_days or config.GATE_TELEMETRY_RETENTION_DAYS
    days = stored_days()
    if len(days) <= max_days:
        return 0
    removed = 0
    for day in days[:-max_days]:
        try:
            os.remove(_day_path(day))
            removed += 1
        except OSError:
            pass
    return removed


def record_scan(rows: list[dict], scan_id: str | None = None,
                day: str | None = None, ruleset: str | None = None,
                max_days: int | None = None) -> dict:
    """Append this scan RUN's per-candidate gate evaluation. APPEND-ONLY.

    One run object per ``scan_id``; re-writing the SAME ``scan_id`` (a genuine
    retry of one run) replaces that run's object, which is what keeps a
    partially-failed sweep from double-counting. No prior run is ever mutated and
    no value is ever recomputed in place.

    Rows are the sweep's already-computed scorecard rows: each must carry
    ``ticker`` and ``gate_results`` (attached by ``metrics.scorecard.score_ticker``
    from ``screening.entry_gate``). A row without them is skipped — a name whose
    gate could not be built is UNRECORDED, never recorded as a failure.

    Best-effort by contract: every exception is swallowed and returned, so a
    telemetry write can never alter, block, or fail the scan that called it.
    Returns {ok, recorded, scan_run_id} or {ok: False, error}.
    """
    try:
        day = day or _today()
        # Omitting scan_id means "this call IS its own run" — microsecond
        # precision so two runs in the same second cannot collide. The nightly
        # sweep passes its memoized as_of, which is the real run identity and
        # makes a retry of that sweep genuinely idempotent.
        scan_id = scan_id or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        ruleset = ruleset or config.GATE_RULESET
        per_candidate = []
        for row in rows or []:
            ticker = (row.get("ticker") or "").upper()
            results = row.get("gate_results")
            if not ticker or not results:
                continue
            per_candidate.append((ticker, admitted(results), results))
        gates, candidates = _encode_run(per_candidate)
        run = {
            "scan_run_id": scan_id,
            "evaluated_at": _now_iso(),
            "gate_ruleset": ruleset,
            "schema_version": SCHEMA_VERSION,
            # The gate manifest: identity, authority and (where constant) the
            # threshold in force AT WRITE TIME, written once for the whole run.
            # Carried on the record, never looked up at read time.
            "gates": gates,
            # Authority in force AT WRITE TIME for the gates that never run in a
            # sweep — same discipline.
            "unevaluated_gates": unevaluated_gates(),
            "candidates": candidates,
        }
        with _lock:
            data = _load_day(day)
            same = next((i for i, r in enumerate(data["runs"])
                         if r.get("scan_run_id") == scan_id), None)
            if same is not None:
                data["runs"][same] = run       # retry of THIS run — idempotent
            else:
                data["runs"].append(run)
            data["schema"] = SCHEMA_VERSION
            _save_day(day, data)
            pruned = prune(max_days)
        return {"ok": True, "recorded": len(candidates),
                "scan_run_id": scan_id, "pruned_days": pruned}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Aggregation — a PURE function over the event stream. Recomputes from ground
# truth on every call; caches nothing durable.
# ---------------------------------------------------------------------------
def _week_of(day: str) -> str:
    """The ISO week key (Monday's date) a scan day falls in."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return (d - timedelta(days=d.weekday())).isoformat()


def _near_miss(result: dict) -> float | None:
    """How far PAST its threshold a FAILING numeric gate's value sat, as a
    fraction of the threshold. Always >= 0 on a failure, whichever way the
    comparison runs, so gates with opposite directions are comparable.

    Returns None when the gate is non-numeric, passed, or has a zero threshold —
    a fractional distance from zero is undefined, and a fabricated one would be
    worse than an absent one. (``L2:rs1m_negative`` has threshold 0.0 and is the
    live case; its raw distance is reported separately as ``raw``.)
    """
    if result.get("passed") is not False:
        return None
    v, t = result.get("value"), result.get("threshold")
    if not isinstance(v, (int, float)) or not isinstance(t, (int, float)):
        return None
    if isinstance(v, bool) or isinstance(t, bool) or t == 0:
        return None
    raw = (v - t) if result.get("direction") == LOWER else (t - v)
    return raw / abs(t)


def _raw_miss(result: dict) -> float | None:
    """The UNNORMALIZED distance past the threshold, for gates whose threshold is
    zero (normalization undefined) — reported so those gates are not silently
    blank in the near-miss column."""
    if result.get("passed") is not False:
        return None
    v, t = result.get("value"), result.get("threshold")
    if not isinstance(v, (int, float)) or not isinstance(t, (int, float)):
        return None
    if isinstance(v, bool) or isinstance(t, bool):
        return None
    return (v - t) if result.get("direction") == LOWER else (t - v)


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile over a pre-sorted list. Deterministic and
    dependency-free; the sample sizes here never justify interpolation."""
    if not sorted_vals:
        return None
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]


def _bucket(x: float, edges) -> str:
    for e in edges:
        if x <= e:
            return f"<={e:g}"
    return f">{edges[-1]:g}"


def iter_runs(start: str | None = None, end: str | None = None,
              gate_ruleset: str | None = None):
    """Stream (day, run) for every recorded run in range, oldest first.

    Streams DAY BY DAY and never holds more than one day's file in memory, which
    is the whole point of the per-day layout. ``gate_ruleset`` filters on the
    ruleset the run was RECORDED under — segmentation happens here so no caller
    can accidentally pool across rulesets.
    """
    for day in stored_days():
        if start and day < start:
            continue
        if end and day > end:
            continue
        for run in _load_day(day).get("runs") or []:
            if gate_ruleset and run.get("gate_ruleset") != gate_ruleset:
                continue
            yield day, run


def rulesets_in_range(start: str | None = None, end: str | None = None) -> dict:
    """{ruleset: evaluated_candidate_count} over the range — what the UI needs to
    show ruleset segmentation as a visible choice rather than a hidden default."""
    out: dict[str, int] = {}
    for _day, run in iter_runs(start, end):
        rs = run.get("gate_ruleset") or "unknown"
        out[rs] = out.get(rs, 0) + len(run.get("candidates") or [])
    return out


def default_range(lookback_days: int | None = None,
                  today: str | None = None) -> tuple[str, str]:
    """The default (start, end) window. ``today`` is injectable so the aggregation
    can be exercised without a clock."""
    days = lookback_days or config.GATE_TELEMETRY_LOOKBACK_DAYS
    end = today or _today()
    try:
        start = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    except ValueError:
        return end, end
    return start, end


def aggregate(start: str | None = None, end: str | None = None,
              gate_ruleset: str | None = None,
              symbols=None, today: str | None = None) -> dict:
    """The calibration rollup. PURE over the event stream; caches nothing.

    ``sole_blocker_rate`` counts ONLY ``authority == veto`` gates in the
    "every other gate passed" test — a shadow metric flagging is not a block, and
    treating it as one would invert the entire diagnostic. Authority is read off
    each recorded result, never from live config, so a gate that graduated out of
    shadow mode does not retroactively rewrite the rows recorded before it did.

    Never pools across ``gate_ruleset``: an unfiltered call segments by the
    rulesets actually present and reports which one it aggregated. When more than
    one is present and none was named, the caller gets the counts and NO pooled
    table — silently merging two rule regimes would be the single easiest way to
    make this instrument lie.
    """
    if start is None and end is None:
        start, end = default_range(today=today)
    universe = {s.strip().upper() for s in symbols if s and s.strip()} if symbols else None

    edges = tuple(config.GATE_TELEMETRY_NEAR_MISS_BUCKETS)
    # Per-gate accumulators. `meta` holds the authority/level/label AS RECORDED.
    meta: dict[str, dict] = {}
    evaluated: dict[str, int] = {}
    failed: dict[str, int] = {}
    sole: dict[str, int] = {}
    misses: dict[str, list[float]] = {}
    raw_misses: dict[str, list[float]] = {}
    co_block: dict[str, dict[str, int]] = {}
    weekly: dict[str, dict[str, dict[str, int]]] = {}
    unevaluated: dict[str, dict] = {}

    evaluated_n = 0
    admitted_n = 0
    runs_n = 0
    days_seen: set[str] = set()
    seen_rulesets: set[str] = set()
    # Per-ruleset candidate counts, accumulated in the SAME pass rather than a
    # pre-scan: the window can be a hundred day files and reading it twice per
    # request bought nothing.
    present: dict[str, int] = {}

    for day, run in iter_runs(start, end, gate_ruleset):
        runs_n += 1
        days_seen.add(day)
        run_rs = run.get("gate_ruleset") or "unknown"
        seen_rulesets.add(run_rs)
        present[run_rs] = present.get(run_rs, 0) + len(run.get("candidates") or [])
        week = _week_of(day)
        for g in run.get("unevaluated_gates") or []:
            # First writer wins per gate id: the authority in force at the START
            # of the range, with any later change surfaced as a conflict rather
            # than silently overwritten.
            gid = g.get("gate_id")
            if gid and gid not in unevaluated:
                unevaluated[gid] = dict(g)
            elif gid and unevaluated[gid].get("authority") != g.get("authority"):
                unevaluated[gid]["authority_changed_in_range"] = True

        # Decoded to the typed schema-1 shape here, so everything below works
        # against gate NAMES and never against the store's array offsets.
        for symbol, cand_admitted, results in decoded_candidates(run):
            if universe is not None and symbol not in universe:
                continue
            evaluated_n += 1
            if cand_admitted:
                admitted_n += 1
            # The veto subset defines the sole-blocker test. An unevaluated veto
            # (passed is None) is NOT counted as a pass: it makes the candidate
            # unusable for attributing a sole block, which is tracked below.
            veto_failed = [r["gate_id"] for r in results
                           if r.get("authority") == VETO and r.get("passed") is False]
            veto_unknown = any(r.get("authority") == VETO and r.get("passed") is None
                               for r in results)
            for r in results:
                gid = r.get("gate_id")
                if not gid:
                    continue
                if gid not in meta:
                    meta[gid] = {"gate_id": gid, "level": r.get("level"),
                                 "authority": r.get("authority"),
                                 "label": r.get("label") or gid,
                                 "threshold": r.get("threshold"),
                                 "direction": r.get("direction")}
                elif meta[gid]["authority"] != r.get("authority"):
                    meta[gid]["authority_changed_in_range"] = True
                if r.get("passed") is None:
                    continue                      # ran but produced no verdict
                evaluated[gid] = evaluated.get(gid, 0) + 1
                wk = weekly.setdefault(gid, {}).setdefault(
                    week, {"evaluated": 0, "failed": 0, "sole": 0})
                wk["evaluated"] += 1
                if r.get("passed") is False:
                    failed[gid] = failed.get(gid, 0) + 1
                    wk["failed"] += 1
                    nm = _near_miss(r)
                    if nm is not None:
                        misses.setdefault(gid, []).append(nm)
                    else:
                        rm = _raw_miss(r)
                        if rm is not None:
                            raw_misses.setdefault(gid, []).append(rm)
                    if r.get("authority") == VETO:
                        # Sole block: this veto failed and no OTHER veto did.
                        # Never attributed when some veto did not run — that is
                        # an unknown, not a sole block.
                        if not veto_unknown and veto_failed == [gid]:
                            sole[gid] = sole.get(gid, 0) + 1
                            wk["sole"] += 1
                        for other in veto_failed:
                            if other != gid:
                                co_block.setdefault(gid, {})
                                co_block[gid][other] = co_block[gid].get(other, 0) + 1

    # Never pool across rulesets. An unfiltered range that turns out to contain
    # more than one returns the counts and NO table: silently merging two rule
    # regimes is the single easiest way to make this instrument lie, so the
    # accumulators above are discarded rather than reported.
    if gate_ruleset is None and len(present) > 1:
        return {
            "start": start, "end": end, "gate_ruleset": None,
            "rulesets_present": present, "pooled": False,
            "schema_version": SCHEMA_VERSION,
            "evaluated_n": 0, "admitted_n": 0, "admitted_rate": None,
            "runs": 0, "days": 0, "first_day": None, "last_day": None,
            "gates": [], "co_block_matrix": {}, "time_series": {},
            "low_confidence": True,
            "min_evaluated_n": config.GATE_TELEMETRY_MIN_EVALUATED_N,
            "retention_days": config.GATE_TELEMETRY_RETENTION_DAYS,
            "note": ("multiple gate rulesets recorded in this range — pick one; "
                     "pooling across rulesets is never done"),
        }

    gates = []
    for gid, m in meta.items():
        n = evaluated.get(gid, 0)
        f = failed.get(gid, 0)
        norm = sorted(misses.get(gid, []))
        raw = sorted(raw_misses.get(gid, []))
        hist: dict[str, int] = {}
        for x in norm:
            b = _bucket(x, edges)
            hist[b] = hist.get(b, 0) + 1
        is_veto = m["authority"] == VETO
        gates.append({
            **m,
            "evaluated_n": n,
            "failed_n": f,
            "block_rate": round(f / n, 4) if n else None,
            # Sole-blocker rate is defined only for veto gates. A shadow or rank
            # gate is reported as None, not 0.0 — "not applicable" and "never the
            # sole blocker" are different facts.
            "sole_blocker_n": sole.get(gid, 0) if is_veto else None,
            "sole_blocker_rate": (round(sole.get(gid, 0) / n, 4)
                                  if (is_veto and n) else None),
            "indeterminate": False,
            "co_block": dict(sorted(co_block.get(gid, {}).items(),
                                    key=lambda kv: kv[1], reverse=True)),
            "near_miss": {
                "n": len(norm),
                "normalized": bool(norm),
                "median": _percentile(norm, 0.5),
                "p75": _percentile(norm, 0.75),
                "buckets": hist,
                "bucket_edges": list(edges),
                # Threshold-is-zero gates cannot be normalized; their raw
                # distance is reported instead of a blank column.
                "raw_n": len(raw),
                "raw_median": _percentile(raw, 0.5),
                "raw_p75": _percentile(raw, 0.75),
            },
        })

    # Gates that never ran. Reported as INDETERMINATE — explicitly distinct from
    # a 0.0 rate, never imputed, never dropped from the table.
    for gid, g in unevaluated.items():
        gates.append({
            "gate_id": gid, "level": g.get("level"),
            "authority": g.get("authority"), "label": g.get("label") or gid,
            "threshold": None, "direction": None,
            "authority_changed_in_range": g.get("authority_changed_in_range", False),
            "evaluated_n": 0, "failed_n": 0,
            "block_rate": None, "sole_blocker_n": None, "sole_blocker_rate": None,
            "indeterminate": True, "indeterminate_reason": g.get("reason"),
            "co_block": {},
            "near_miss": {"n": 0, "normalized": False, "median": None, "p75": None,
                          "buckets": {}, "bucket_edges": list(edges),
                          "raw_n": 0, "raw_median": None, "raw_p75": None},
        })

    # Sorted by the headline metric, descending. Indeterminate rows sort last —
    # they carry no rate, and floating them to the top on a null would be exactly
    # the confusion the explicit marker exists to prevent.
    gates.sort(key=lambda g: (g["indeterminate"],
                              -(g["sole_blocker_rate"] or 0),
                              -(g["block_rate"] or 0), g["gate_id"]))

    # Pairwise co-failure counts across veto gates only. Symmetric by
    # construction; two gates failing together 95% of the time are one gate
    # wearing two hats.
    veto_ids = sorted(gid for gid, m in meta.items() if m["authority"] == VETO)
    matrix = {a: {b: co_block.get(a, {}).get(b, 0) for b in veto_ids if b != a}
              for a in veto_ids}

    series = {}
    for gid, weeks in weekly.items():
        series[gid] = [
            {"week": wk,
             "evaluated_n": v["evaluated"],
             "block_rate": round(v["failed"] / v["evaluated"], 4) if v["evaluated"] else None,
             "sole_blocker_rate": (round(v["sole"] / v["evaluated"], 4)
                                   if (v["evaluated"] and meta[gid]["authority"] == VETO)
                                   else None)}
            for wk, v in sorted(weeks.items())
        ]

    resolved_rs = gate_ruleset or (next(iter(seen_rulesets)) if len(seen_rulesets) == 1
                                   else None)
    return {
        "start": start, "end": end,
        "gate_ruleset": resolved_rs,
        "rulesets_present": present,
        "pooled": False,
        "schema_version": SCHEMA_VERSION,
        # The denominator. Every rate above is meaningless without it.
        "evaluated_n": evaluated_n,
        "admitted_n": admitted_n,
        "admitted_rate": round(admitted_n / evaluated_n, 4) if evaluated_n else None,
        "runs": runs_n,
        "days": len(days_seen),
        "first_day": min(days_seen) if days_seen else None,
        "last_day": max(days_seen) if days_seen else None,
        "low_confidence": evaluated_n < config.GATE_TELEMETRY_MIN_EVALUATED_N,
        "min_evaluated_n": config.GATE_TELEMETRY_MIN_EVALUATED_N,
        "gates": gates,
        "co_block_matrix": matrix,
        "time_series": series,
        "retention_days": config.GATE_TELEMETRY_RETENTION_DAYS,
    }
