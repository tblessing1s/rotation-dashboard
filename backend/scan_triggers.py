"""Forward-looking TRIGGER conditions + the gate-complete scan verdict.

``scan_verdict.compose_verdict`` composes the three SIGNAL inputs (the invisible
market regime + Symbol Genius + structure entrability). This module closes the
Phase-0 verdict-completeness gap (the "AAPL READY + fails entry gate level 4"
bug): it folds the FULL entry gate onto that signal composition so **READY means
"will pass Execute"**, and it annotates the SAME already-computed gate evaluation
with forward-looking triggers — it never re-runs the gate or builds a parallel
rules engine.

Two products, both PURE (no I/O, no clock, no fetching):

  1. ``compose_row_verdict`` — the gate-complete verdict. Worst-signal-wins over
     the signal composition PLUS every failing gate block. A block that is a
     *safety* "no" (regime RED, SYM RED, topping/declining/distributing structure,
     a broken-trend or sector-under-distribution veto) forces BLOCKED; a block that
     is merely *waiting* (a calendar / conditional / estimated trigger) degrades to
     WATCH. This is NOT a fifth verdict value — BENCH is a derived VIEW over this
     output (see ``is_bench``); the vocabulary stays READY/CAUTION/WATCH/BLOCKED.

  2. ``triggers_for_blocks`` — one machine-readable TRIGGER per non-READY block,
     of kind CALENDAR (deterministic date), CONDITIONAL (a predicate on observable
     state), ESTIMATED (a crude days-to-trigger, always labelled EST), or SAFETY
     (a "no", carries no trigger). Triggers derive from the binding-constraint
     read, so the "path to READY" line and the binding constraint can never disagree.

The blocks themselves are EXTRACTED from the already-computed gate dicts
(``screening.entry_gate`` levels + the optional ``account_gate.evaluate`` L5) — a
READ of ``levels[].checks[]`` / ``checks[].detail``, never a re-evaluation.

Every estimation constant here is ``PROPOSED_DEFAULT``.
"""
from __future__ import annotations

from datetime import date, timedelta

import scan_verdict as sv

# Trigger kinds.
CALENDAR = "calendar"        # deterministic date (earnings + buffer, dwell transition)
CONDITIONAL = "conditional"  # a predicate on observable state (slot free, RS > 0, ...)
ESTIMATED = "estimated"      # a crude days-to-trigger (labelled EST)
SAFETY = "safety"            # a "no" — carries no trigger, excludes from bench

# PROPOSED_DEFAULT — the post-earnings settle buffer for the earnings CALENDAR
# trigger. The Level-5 gate blocks the WHOLE planned cycle (0..CYCLE_WEEKS_MAX*7
# days), so the block clears the trading day AFTER the report; this buffer is the
# only knob and is NOT an existing HARD_CFM_RULE (there is no earnings-buffer
# constant in config today — see AUDIT_SCAN_PIPELINE_PHASE0.md Q3).
EARNINGS_TRIGGER_BUFFER_DAYS = 1        # PROPOSED_DEFAULT

# PROPOSED_DEFAULT — an ESTIMATED days-to-trigger is only emitted when it is
# non-negative and below this ceiling; a wilder extrapolation is reported as an
# unbounded EST (days=None) rather than a false-precise number.
MAX_ESTIMATED_DAYS = 60                 # PROPOSED_DEFAULT

# The gate level each signal input maps onto, for stable binding-constraint order
# (stop-on-first-fail is level-ascending: L1 < L2 < L3 < L3.5 < L4 < L5).
_SIGNAL_LEVEL = {"regime": 1, "symbol": 3, "structure": 3.5}


# ---------------------------------------------------------------------------
# Trigger classification — one registry, keyed by the check id. calendar /
# conditional / estimated / safety per AUDIT_SCAN_PIPELINE_PHASE0.md Q2.
# ---------------------------------------------------------------------------
# Static (context-free) classification. Ids that need observed values to phrase
# their predicate (earnings date, extension distance) are refined in ``classify``.
_KIND = {
    # Signals (worst-input reasons from compose_verdict).
    "regime": {"BLOCKED": SAFETY, "WATCH": CONDITIONAL, "CAUTION": CONDITIONAL},
    "symbol": {"BLOCKED": SAFETY, "WATCH": CONDITIONAL, "CAUTION": CONDITIONAL},
    "structure": {"BLOCKED": SAFETY, "WATCH": CONDITIONAL, "CAUTION": CONDITIONAL},
    # Level 2 — sector veto (screening._compute_sectors deteriorating_reasons).
    "rs1m_negative": CONDITIONAL,
    "breadth_collapsing": CONDITIONAL,
    "under_distribution": SAFETY,
    # Level 3 — stock-lights vetoes (stock_lights.evaluate_vetoes ids).
    "veto:rs3m_vs_sector": CONDITIONAL,
    "veto:atr_expanding_high_ivr": CONDITIONAL,
    "veto:close_below_ma200": SAFETY,
    # Level 4 — right spot (stock_lights.right_spot check ids).
    "atr_pct": CONDITIONAL,
    "atr_5d_ema": ESTIMATED,
    "extension": ESTIMATED,
    # Level 5 — account & juice (account_gate.evaluate check ids).
    "earnings_in_cycle": CALENDAR,
    "sector_concentration": CONDITIONAL,
    "position_limit": CONDITIONAL,
    "capital_limit": CONDITIONAL,
    "cash_reserve": CONDITIONAL,
    "juice_adequacy": CONDITIONAL,
}

# Human-readable "clears when" phrasing per id (the path-to-READY leg).
_CLEARS = {
    "regime": "market regime GREEN",
    "symbol": "SYM GREEN",
    "structure": "structure entrable (EARLY_ADVANCE)",
    "rs1m_negative": "sector RS1M vs SPY > 0",
    "breadth_collapsing": "sector breadth recovers",
    "under_distribution": "sector no longer under distribution",
    "veto:rs3m_vs_sector": "RS3M vs sector > 0",
    "veto:atr_expanding_high_ivr": "ATR contracting or IVR cools",
    "veto:close_below_ma200": "reclaim MA200",
    "atr_pct": "ATR% contracts into range",
    "atr_5d_ema": "ATR contracting (≤ 5d-EMA)",
    "extension": "pull back within 1 ATR of MA21",
    "earnings_in_cycle": "earnings passes",
    "sector_concentration": "sector slot opens",
    "position_limit": "a position slot frees",
    "capital_limit": "deployed-capital headroom frees",
    "cash_reserve": "free cash ≥ reserve",
    "juice_adequacy": "weekly juice ≥ target",
}


def _kind_for(check_id: str, level_str: str | None) -> str:
    entry = _KIND.get(check_id)
    if isinstance(entry, dict):
        return entry.get(level_str or "", SAFETY)
    return entry or CONDITIONAL


def _add_days(iso: str | None, days: int) -> str | None:
    """ISO date string + ``days`` calendar days (pure — no clock)."""
    if not iso:
        return None
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None
    return (d + timedelta(days=days)).isoformat()


def classify(block: dict) -> dict:
    """Classify one gate block into a forward-looking trigger. PURE.

    ``block`` = {level, id, label?, observed}. Returns the block enriched with
    ``kind`` and a ``trigger`` dict {kind, clears_when, eligible_date?,
    days_estimate?, estimated?} — never None for a real block.
    """
    cid = block.get("id", "")
    level_str = block.get("level_str")           # the signal severity, for signal ids
    kind = _kind_for(cid, level_str)
    obs = block.get("observed") or {}
    clears = _CLEARS.get(cid, cid)
    trig: dict = {"kind": kind, "clears_when": clears}

    if kind == CALENDAR and cid == "earnings_in_cycle":
        earn = obs.get("earnings") or {}
        eligible = _add_days(earn.get("date"), EARNINGS_TRIGGER_BUFFER_DAYS)
        days = earn.get("days_until")
        trig["eligible_date"] = eligible
        trig["days_estimate"] = (int(days) + EARNINGS_TRIGGER_BUFFER_DAYS
                                 if isinstance(days, (int, float)) else None)
        trig["clears_when"] = (f"earnings {earn.get('date')} clears"
                               if earn.get("date") else clears)

    elif kind == ESTIMATED:
        days = _estimate_days(cid, obs)
        trig["estimated"] = True
        trig["days_estimate"] = days

    return {**block, "kind": kind, "trigger": trig}


def _estimate_days(cid: str, obs: dict) -> int | None:
    """A CRUDE days-to-trigger for the two Level-4 ESTIMATED blocks — always EST,
    None when it can't be computed or extrapolates past MAX_ESTIMATED_DAYS.

      * extension — bars for MA21 (rising at its recent daily rate) to catch up to
        within SPOT_ATR_EXTENSION_MAX ATR of price, at today's price/MA gap.
      * atr_5d_ema — bars for ATR to contract to its 5d-EMA at the recent
        contraction rate.
    All PROPOSED_DEFAULT; a positive finite estimate only.
    """
    if cid == "extension":
        gap_atr = obs.get("excess_atr")             # ATR beyond the max, >0 when blocking
        ma21_rise = obs.get("ma21_rise_per_day")    # $/day, from the recent MA21 slope
        atr = obs.get("atr")
        if (gap_atr is None or ma21_rise is None or atr is None
                or ma21_rise <= 0 or atr <= 0):
            return None
        days = (gap_atr * atr) / ma21_rise
    elif cid == "atr_5d_ema":
        excess = obs.get("momentum_excess")         # atr/atr_5ema - max, >0 when blocking
        rate = obs.get("contraction_per_day")       # recent daily drop in that ratio
        if excess is None or rate is None or rate <= 0:
            return None
        days = excess / rate
    else:
        return None
    if days < 0 or days > MAX_ESTIMATED_DAYS:
        return None
    return int(round(days)) or 1


# ---------------------------------------------------------------------------
# Block extraction — a READ of the already-computed gate dicts (no re-eval).
# ---------------------------------------------------------------------------
def _level(gate: dict | None, level) -> dict | None:
    for lv in (gate or {}).get("levels") or []:
        if lv.get("level") == level:
            return lv
    return None


def gate_blocks(gate: dict | None, account_gate: dict | None = None,
                *, ext_context: dict | None = None) -> list[dict]:
    """Every FAILING gate check the three signal inputs don't already own, as
    structured blocks {level, id, label, observed}. Extracted from the gate levels
    (L2 sector veto, L3 tripped vetoes, L4 right-spot) and the optional L5 account
    gate — a READ, never a re-evaluation. L1/L3-light-vote/L3.5 are owned by the
    signal composition, so they are NOT re-pulled here.

    ``ext_context`` supplies the extra observed values the two Level-4 ESTIMATED
    triggers need for a days estimate (ma21 rise/day, atr, contraction rate).
    """
    blocks: list[dict] = []
    ext = ext_context or {}

    # Level 2 — sector deterioration (the veto reasons are already the ids).
    l2 = _level(gate, 2)
    if l2 is not None and not l2.get("pass", True):
        det = l2.get("detail") or {}
        for reason in det.get("deteriorating_reasons") or []:
            blocks.append({"level": 2, "id": reason, "label": "sector deteriorating",
                           "observed": {"rs1m": det.get("rs1m"), "breadth": det.get("breadth"),
                                        "inst_flow": det.get("inst_flow")}})

    # Level 3 — tripped stock-lights vetoes (SYM cannot represent these).
    l3 = _level(gate, 3)
    if l3 is not None:
        det = l3.get("detail") or {}
        for v in det.get("vetoes") or []:
            if v.get("tripped"):
                blocks.append({"level": 3, "id": f"veto:{v.get('id')}",
                               "label": "entry veto", "observed": {"value": v.get("value")}})

    # Level 4 — right spot (blocking; the check ids carry the observed values).
    l4 = _level(gate, 4)
    if l4 is not None and not l4.get("pass", True):
        rs = (l4.get("detail") or {}).get("right_spot") or {}
        for c in rs.get("checks") or []:
            if not c.get("pass"):
                observed = {"value": c.get("value")}
                if c.get("id") == "extension":
                    observed.update(ext.get("extension") or {})
                if c.get("id") == "atr_5d_ema":
                    observed.update(ext.get("atr_5d_ema") or {})
                blocks.append({"level": 4, "id": c.get("id"), "label": "right spot",
                               "observed": observed})

    # Level 5 — account & juice (only the blocking failures).
    if account_gate:
        by_id = {c.get("id"): c for c in account_gate.get("checks") or []}
        for cid in account_gate.get("blocking_failures") or []:
            c = by_id.get(cid) or {}
            blocks.append({"level": 5, "id": cid, "label": c.get("label"),
                           "observed": c.get("detail") or {}})

    return blocks


def triggers_for_blocks(blocks: list[dict]) -> list[dict]:
    """Classify every block into its trigger, in gate-level order (binding first)."""
    ordered = sorted(blocks, key=lambda b: (b.get("level", 99), str(b.get("id"))))
    return [classify(b) for b in ordered]


# ---------------------------------------------------------------------------
# Gate-complete verdict — worst of the signal composition + every block.
# ---------------------------------------------------------------------------
_BLOCK_SEVERITY = {SAFETY: sv.BLOCKED, CALENDAR: sv.WATCH,
                   CONDITIONAL: sv.WATCH, ESTIMATED: sv.WATCH}
_SEV = {sv.READY: 0, sv.CAUTION: 1, sv.WATCH: 2, sv.BLOCKED: 3}


def _signal_blocks(composed: dict) -> list[dict]:
    """The non-READY signal inputs (regime/symbol/structure) as level-tagged blocks,
    so they order and classify alongside the gate blocks."""
    out = []
    for name, level_str in (composed.get("inputs") or {}).items():
        if level_str != sv.READY:
            out.append({"level": _SIGNAL_LEVEL.get(name, 3), "id": name,
                        "level_str": level_str, "label": "signal",
                        "observed": {}, "signal": True})
    return out


def compose_row_verdict(composed: dict, blocks: list[dict]) -> dict:
    """Fold the gate ``blocks`` onto the signal ``composed`` verdict. PURE.

    Returns {verdict, reasons, binding, triggers}:
      * ``verdict`` — worst severity of the signal verdict and every block
        (safety→BLOCKED, calendar/conditional/estimated→WATCH). READY only when
        the signals clear AND no block fails.
      * ``reasons`` — legacy string list (gate-level ordered), the binding first,
        so ``scan_rejection_log.binding_constraint`` keeps working unchanged.
      * ``binding`` — the structured first-fail {level, id, kind, ...} (Q9 capture).
      * ``triggers`` — every block classified (the path-to-READY source).
    """
    all_blocks = _signal_blocks(composed) + list(blocks or [])
    triggers = triggers_for_blocks(all_blocks)

    worst = _SEV[composed.get("verdict", sv.READY)]
    for t in triggers:
        worst = max(worst, _SEV[_BLOCK_SEVERITY.get(t["kind"], sv.WATCH)])
    verdict = next(v for v, s in _SEV.items() if s == worst)

    # Legacy reason strings, gate-level ordered. Signal reasons keep the
    # "name:LEVEL" shape compose_verdict used; gate reasons are "L<n>:<id>".
    reasons = []
    for t in triggers:
        if t.get("signal"):
            reasons.append(f"{t['id']}:{t['level_str']}")
        else:
            lvl = t.get("level")
            reasons.append(f"L{lvl:g}:{t['id']}")
    binding = triggers[0] if triggers else None
    return {"verdict": verdict, "reasons": reasons, "binding": binding,
            "triggers": triggers}


# ---------------------------------------------------------------------------
# Bench membership — a derived VIEW, not a verdict value.
# ---------------------------------------------------------------------------
def is_bench(verdict: str | None, triggers: list[dict] | None) -> bool:
    """A row is BENCH when it is not READY, would otherwise be READY, and every
    blocking trigger is a WAIT (calendar / conditional / estimated) — i.e. NO safety
    block. Safety blocks (regime RED, SYM RED, topping/declining/distributing
    structure, broken-trend / sector-distribution veto) are "no", never "waiting".
    """
    if verdict == sv.READY or not triggers:
        return False
    return all(t.get("kind") != SAFETY for t in triggers)


def path_to_ready(triggers: list[dict] | None) -> str | None:
    """The rendered "path to READY" line — the trigger legs joined, calendar dates
    and EST days inline. None for a READY row (no triggers)."""
    if not triggers:
        return None
    legs = []
    for t in triggers:
        tr = t.get("trigger") or {}
        leg = tr.get("clears_when") or t.get("id")
        if tr.get("eligible_date"):
            leg += f" (eligible ~{tr['eligible_date']})"
        elif tr.get("kind") == ESTIMATED and tr.get("days_estimate") is not None:
            leg += f" (~{tr['days_estimate']}d EST)"
        legs.append(leg)
    return " · ".join(legs)


def earliest_eligible_days(triggers: list[dict] | None) -> int | None:
    """The smallest concrete days-to-eligible across a row's triggers (calendar or
    EST), for the throughput header's ≤14d bucket and the bench sort. None when no
    trigger carries a day count (all purely conditional)."""
    if not triggers:
        return None
    days = [t["trigger"]["days_estimate"] for t in triggers
            if (t.get("trigger") or {}).get("days_estimate") is not None]
    return min(days) if days else None
