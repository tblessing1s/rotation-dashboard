"""Daily scan TRANSITION diff — the scan as a pipeline, not a snapshot.

Given yesterday's per-symbol scan state and today's, emit machine-readable
transition EVENTS: a bench name that went READY, any fresh READY, a watched name
that degraded, a new pipeline entrant, and a sector slot that opened with a
bench/ready name waiting for it. The events fan out through the EXISTING notifier
(``alerts.record_event``) — this module builds NO notification infrastructure and
mutates NO state; it is a PURE fold over two record maps.

Event kinds (also the new ``alerts.ALERT_TYPES`` ids):

  * ``SCAN_BENCH_READY``      — a BENCH name is now READY (highest priority: it was
    one trigger away and the trigger cleared).
  * ``SCAN_NEW_READY``        — a non-bench name became READY (was not READY before).
  * ``SCAN_DEGRADED``         — a watched name's structure/flow/RS rolled over
    (BASE→TOPPING/DECLINING, INST→DISTRIBUTING, RS→FADING/FALLING).
  * ``SCAN_PIPELINE_ENTRANT`` — a BASING + EARLY_INTEREST name newly appeared.
  * ``SCAN_SECTOR_SLOT_OPEN`` — a sector position exited AND a bench/ready name
    exists in that now-free sector.

Records carry: verdict, bench, base_stage, inst_flow, rs_state, sector (today rows
also carry path_to_ready/eligible_days for the alert payload). PURE — no I/O, no
clock; the caller supplies today, yesterday, and the occupied-sector snapshots.
"""
from __future__ import annotations

# Event type ids (mirror ALERT_TYPES).
BENCH_READY = "SCAN_BENCH_READY"
NEW_READY = "SCAN_NEW_READY"
DEGRADED = "SCAN_DEGRADED"
PIPELINE_ENTRANT = "SCAN_PIPELINE_ENTRANT"
SECTOR_SLOT_OPEN = "SCAN_SECTOR_SLOT_OPEN"

_DEGRADE_BASE = {"TOPPING", "DECLINING"}
_DEGRADE_RS = {"FADING", "FALLING"}


def _event(type_: str, ticker: str | None, message: str, data: dict) -> dict:
    return {"type": type_, "ticker": ticker, "message": message, "data": data}


def diff_symbol(prev: dict | None, today: dict | None) -> list[dict]:
    """Transition events for ONE symbol between its prior and current scan record.
    PURE. ``prev`` is None for a symbol never scanned before (only READY / entrant
    events can fire — nothing to degrade FROM)."""
    if not today:
        return []
    tkr = (today.get("ticker") or "").upper() or None
    disp = tkr or "?"
    events: list[dict] = []
    tv = today.get("verdict")
    pv = (prev or {}).get("verdict")
    was_bench = bool((prev or {}).get("bench"))

    # READY transitions (bench-cleared is the headline).
    if tv == "READY" and pv != "READY":
        if was_bench:
            events.append(_event(BENCH_READY, tkr,
                                 f"{disp} cleared its trigger — now READY",
                                 {"from": "BENCH", "sector": today.get("sector")}))
        else:
            events.append(_event(NEW_READY, tkr,
                                 f"{disp} is now READY",
                                 {"from": pv, "sector": today.get("sector")}))

    if prev:
        # Degradations — only for a name we were WATCHING (prev not BLOCKED).
        watched = pv != "BLOCKED"
        if watched:
            pb, tb = prev.get("base_stage"), today.get("base_stage")
            if tb in _DEGRADE_BASE and pb not in _DEGRADE_BASE:
                events.append(_event(DEGRADED, tkr,
                                     f"{disp} base rolled over: {pb} → {tb}",
                                     {"axis": "base", "from": pb, "to": tb}))
            pi, ti = prev.get("inst_flow"), today.get("inst_flow")
            if ti == "DISTRIBUTING" and pi != "DISTRIBUTING":
                events.append(_event(DEGRADED, tkr,
                                     f"{disp} under distribution: {pi} → {ti}",
                                     {"axis": "inst", "from": pi, "to": ti}))
            pr, tr = prev.get("rs_state"), today.get("rs_state")
            if tr in _DEGRADE_RS and pr not in _DEGRADE_RS:
                events.append(_event(DEGRADED, tkr,
                                     f"{disp} relative strength {pr} → {tr}",
                                     {"axis": "rs", "from": pr, "to": tr}))

    # Pipeline entrant — a fresh BASING + EARLY_INTEREST that wasn't that yesterday.
    entrant_now = (today.get("base_stage") == "BASING"
                   and today.get("inst_flow") == "EARLY_INTEREST")
    entrant_prev = bool(prev) and (prev.get("base_stage") == "BASING"
                                   and prev.get("inst_flow") == "EARLY_INTEREST")
    if entrant_now and not entrant_prev:
        events.append(_event(PIPELINE_ENTRANT, tkr,
                             f"{disp} entered the pipeline (BASING + early interest)",
                             {"sector": today.get("sector")}))
    return events


def sector_slot_events(prev_occupied, occupied_now, today_by_sym) -> list[dict]:
    """A SECTOR_SLOT_OPEN event for every sector that was occupied yesterday and is
    free today AND has a bench-or-ready candidate waiting in it. PURE."""
    prev_occupied = set(prev_occupied or [])
    occupied_now = set(occupied_now or [])
    freed = prev_occupied - occupied_now
    if not freed:
        return []
    # Candidates waiting per now-free sector: a READY or bench name in that sector.
    events = []
    for sector in sorted(freed):
        waiting = [r for r in today_by_sym.values()
                   if r.get("sector") == sector and (r.get("verdict") == "READY" or r.get("bench"))]
        if not waiting:
            continue
        names = ", ".join(sorted((r.get("ticker") or "").upper() for r in waiting)[:5])
        lead = sorted(waiting, key=lambda r: 0 if r.get("verdict") == "READY" else 1)[0]
        events.append(_event(SECTOR_SLOT_OPEN, (lead.get("ticker") or "").upper() or None,
                             f"{sector} slot opened — {names} waiting",
                             {"sector": sector, "candidates": names}))
    return events


def diff(prev_by_sym: dict, today_by_sym: dict, *,
         prev_occupied=None, occupied_now=None) -> list[dict]:
    """All transition events across the universe. PURE. ``*_by_sym`` map upper-case
    ticker -> record; occupied-sector sets drive the slot-open events."""
    events: list[dict] = []
    for ticker, today in today_by_sym.items():
        events.extend(diff_symbol(prev_by_sym.get(ticker), today))
    events.extend(sector_slot_events(prev_occupied, occupied_now, today_by_sym))
    return events
