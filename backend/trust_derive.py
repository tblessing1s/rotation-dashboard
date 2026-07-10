"""Trust-layer derivations — resolution matching, the trust scoreboard, and the
order-fidelity ledger. Everything here is a PURE derivation over the immutable
records (executions, recommendations, recommendation_overrides, order_events,
order_receipts) plus an injected clock; recompute_derived() calls
``recompute(state, now)`` after every append, so no scoreboard number is ever
hand-entered and a full rebuild from the raw records is always byte-stable.

Matching semantics (the trust contract):

- An execution matches the LATEST open recommendation of the same action type
  on the same position whose validity window contains the execution instant.
  A superseded, overridden, or expired recommendation never matches.
- An execution with NO matching recommendation synthesizes a COVERAGE_MISS —
  the failure mode that matters most (the engine failed to commit before the
  operator acted); an open ALL_CLEAR on the position does not excuse it.
- Executions BEFORE metadata.trust_layer_since predate the engine and are
  excluded (they would otherwise all read as misses).

Scope: matchable operator actions are ENTER (buy_leap / atomic open, excluding
scale-ins), ROLL_OUT (roll pairs with reason scheduled / 75%-rule / earnings),
DEFEND (roll pairs with reason defend), and EXIT (close_leap, excluding LEAP
rolls). Mechanical LEAP rolls, kill-switch-exit roll legs (part of an exit),
scale-in adds, standalone leg repairs, and reconciliation adjustments are out
of scope by rule and never synthesize misses — the operator doc lists them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
import order_lifecycle as olc
import slippage
from rec_types import (ActionType, CheckStatus, FidelityCheck, FidelityDefect,
                       Resolution, TriggerRule)

_ROLL_REASON_ACTION = {
    "scheduled": ActionType.ROLL_OUT,
    "75%-rule": ActionType.ROLL_OUT,
    "earnings": ActionType.ROLL_OUT,
    "defend": ActionType.DEFEND,
    # kill-switch-exit rolls are part of an exit in progress — the close_leap
    # carries the EXIT; grading the roll leg separately would double-count.
    "kill-switch-exit": None,
}

_INTENT_ACTION = {
    "open": ActionType.ENTER,
    "open_position_atomic": ActionType.ENTER,
    "buy_leap": ActionType.ENTER,
    "exit": ActionType.EXIT,
    "close_position_atomic": ActionType.EXIT,
    "close_leap": ActionType.EXIT,
    "roll_short": ActionType.ROLL_OUT,   # refined to DEFEND via the roll_reason
    "roll_leap": None,                   # mechanical LEAP roll — out of scope
    "sell_short": None,
    "close_short": None,
}


def _parse_ts(value) -> datetime | None:
    try:
        s = str(value)[:19].replace("T", " ")
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 1) Executions -> matchable operator-action instances
# ---------------------------------------------------------------------------
def map_actions(state: dict) -> list[dict]:
    """Classify the immutable executions into matchable operator-action
    instances: {action_type, ticker, at, execution_ids, strike, net, live}."""
    since = _parse_ts((state.get("metadata") or {}).get("trust_layer_since"))
    out: list[dict] = []
    rolls: dict[str, dict] = {}
    exits: dict[tuple, dict] = {}
    for e in state.get("executions", []):
        at = _parse_ts(e.get("date"))
        if at is None or (since is not None and at < since):
            continue
        if e.get("leap_roll_id"):
            continue  # mechanical LEAP roll boundary — not a graded action
        action = e.get("action")
        t = (e.get("ticker") or "").upper()
        gid = e.get("roll_group_id") or e.get("roll_id")
        if gid and action in ("sell_short", "close_short"):
            action_type = _ROLL_REASON_ACTION.get(e.get("roll_reason"))
            if action_type is None:
                continue
            inst = rolls.setdefault(str(gid), {
                "action_type": action_type, "ticker": t, "at": at,
                "execution_ids": [], "strike": None, "net": None, "live": False,
                "roll_reason": e.get("roll_reason"),
                "_premium": None, "_buyback": None, "_net_fill": None,
            })
            inst["execution_ids"].append(e.get("id"))
            inst["at"] = max(inst["at"], at)
            inst["live"] = inst["live"] or e.get("live_transmitted") is True
            if e.get("roll_net_fill") is not None:
                inst["_net_fill"] = e.get("roll_net_fill")
            if action == "sell_short":  # the new short — the roll's primary leg
                inst["strike"] = e.get("strike")
                inst["_premium"] = e.get("premium_per_share")
            else:
                inst["_buyback"] = e.get("close_price_per_share")
        elif action == "buy_leap":
            if e.get("leap_add"):
                continue  # scale-in — out of scope
            out.append({
                "action_type": ActionType.ENTER, "ticker": t, "at": at,
                "execution_ids": [e.get("id")], "strike": e.get("strike"),
                "net": None, "live": e.get("live_transmitted") is True,
                "open_id": e.get("open_id"),
            })
        elif action == "close_leap":
            # Same-day close_leap legs on one ticker are ONE exit action (a
            # multi-tranche close writes one record per leg).
            key = (t, str(e.get("date"))[:10])
            inst = exits.setdefault(key, {
                "action_type": ActionType.EXIT, "ticker": t, "at": at,
                "execution_ids": [], "strike": e.get("strike"), "net": None,
                "live": False, "exit_reason": e.get("exit_reason"),
            })
            inst["execution_ids"].append(e.get("id"))
            inst["at"] = max(inst["at"], at)
            inst["live"] = inst["live"] or e.get("live_transmitted") is True
        # sell_short / close_short outside a roll pair: leg repair or part of an
        # atomic open/exit — covered by the anchor record or out of scope.
    for inst in rolls.values():
        # Realized net: the atomic fill's own net when present, else the
        # per-leg pair (new premium − buyback) — leg order must not matter.
        if inst["_net_fill"] is not None:
            inst["net"] = inst["_net_fill"]
        elif inst["_premium"] is not None and inst["_buyback"] is not None:
            try:
                inst["net"] = round(float(inst["_premium"]) - float(inst["_buyback"]), 2)
            except (TypeError, ValueError):
                inst["net"] = None
        for k in ("_premium", "_buyback", "_net_fill"):
            inst.pop(k, None)
    out.extend(rolls.values())
    out.extend(exits.values())
    for inst in out:
        # source_rec_id passthrough: an execution staged from a recommendation
        # card carries the rec id; the anchor exec's value wins.
        ids = set(inst["execution_ids"])
        for e in state.get("executions", []):
            if e.get("id") in ids and e.get("source_rec_id"):
                inst["source_rec_id"] = e["source_rec_id"]
                break
    out.sort(key=lambda i: i["at"])
    return out


# ---------------------------------------------------------------------------
# 2) Resolution matching
# ---------------------------------------------------------------------------
def resolve(state: dict, now: datetime) -> list[dict]:
    """Derive recommendation_resolutions from recs + overrides + executions."""
    recs = state.get("recommendations", []) or []
    by_id = {r.get("rec_id"): r for r in recs}
    overrides: dict[str, dict] = {}
    for ov in state.get("recommendation_overrides", []) or []:
        overrides.setdefault(str(ov.get("rec_id")), ov)  # first override wins
    superseded_by: dict[str, str] = {}
    for r in recs:
        if r.get("supersedes"):
            superseded_by.setdefault(str(r["supersedes"]), r.get("rec_id"))

    actions = map_actions(state)
    matched: dict[str, dict] = {}     # rec_id -> match detail
    matched_actions: set[int] = set()

    def _matchable(r: dict, inst: dict) -> bool:
        if r.get("action_type") != inst["action_type"]:
            return False
        if (r.get("ticker") or "").upper() != inst["ticker"]:
            return False
        if r.get("rec_id") in matched:
            return False
        if str(r.get("rec_id")) in superseded_by or str(r.get("rec_id")) in overrides:
            return False
        emitted = _parse_ts(r.get("emitted_at"))
        valid = _parse_ts(r.get("valid_until"))
        return (emitted is not None and valid is not None
                and emitted <= inst["at"] <= valid)

    for idx, inst in enumerate(actions):
        candidates = [r for r in recs if _matchable(r, inst)]
        chosen = None
        src = inst.get("source_rec_id")
        if src and any(r.get("rec_id") == src for r in candidates):
            chosen = by_id[src]
        elif candidates:
            chosen = max(candidates, key=lambda r: r.get("emitted_at") or "")
        if chosen is None:
            continue
        emitted = _parse_ts(chosen.get("emitted_at"))
        ticket = chosen.get("proposed_ticket") or {}
        proposed_strike = None
        for leg in ticket.get("legs") or []:
            if leg.get("instruction") in ("SELL_TO_OPEN", "BUY_TO_OPEN"):
                proposed_strike = leg.get("strike")
                break
        strike_delta = None
        if proposed_strike is not None and inst.get("strike") is not None:
            try:
                strike_delta = round(float(inst["strike"]) - float(proposed_strike), 2)
            except (TypeError, ValueError):
                strike_delta = None
        credit_delta = None
        floor = ticket.get("min_acceptable_net_credit")
        if floor is not None and inst.get("net") is not None:
            try:
                credit_delta = round(float(inst["net"]) - float(floor), 2)
            except (TypeError, ValueError):
                credit_delta = None
        matched[chosen["rec_id"]] = {
            "rec_id": chosen["rec_id"], "status": Resolution.EXECUTED_MATCHED,
            "action_type": inst["action_type"], "ticker": inst["ticker"],
            "execution_ids": inst["execution_ids"], "live": inst["live"],
            "executed_at": _iso(inst["at"]),
            "deltas": {
                "strike_delta": strike_delta,
                "credit_delta_vs_min": credit_delta,
                "hours_from_emission": (round((inst["at"] - emitted).total_seconds() / 3600, 2)
                                        if emitted else None),
            },
            "at": _iso(inst["at"]),
        }
        matched_actions.add(idx)

    resolutions: list[dict] = []
    for r in recs:
        rid = r.get("rec_id")
        if rid in matched:
            resolutions.append(matched[rid])
            continue
        if str(rid) in overrides:
            ov = overrides[str(rid)]
            resolutions.append({
                "rec_id": rid, "status": Resolution.OVERRIDDEN,
                "action_type": r.get("action_type"), "ticker": r.get("ticker"),
                "reason": ov.get("reason"), "note": ov.get("note"),
                "live": None, "at": ov.get("at"),
            })
            continue
        if str(rid) in superseded_by:
            successor = by_id.get(superseded_by[str(rid)]) or {}
            resolutions.append({
                "rec_id": rid, "status": Resolution.SUPERSEDED,
                "action_type": r.get("action_type"), "ticker": r.get("ticker"),
                "superseded_by": superseded_by[str(rid)],
                "at": successor.get("emitted_at"),
            })
            continue
        valid = _parse_ts(r.get("valid_until"))
        if valid is not None and now > valid:
            resolutions.append({
                "rec_id": rid, "status": Resolution.EXPIRED,
                "action_type": r.get("action_type"), "ticker": r.get("ticker"),
                "at": r.get("valid_until"),
            })
        # else: still open — open recommendations carry no resolution record.

    for idx, inst in enumerate(actions):
        if idx in matched_actions:
            continue
        resolutions.append({
            "rec_id": None, "status": Resolution.COVERAGE_MISS,
            "action_type": inst["action_type"], "ticker": inst["ticker"],
            "execution_ids": inst["execution_ids"], "live": inst["live"],
            "at": _iso(inst["at"]),
            "snapshot": {"strike": inst.get("strike"), "net": inst.get("net"),
                         "roll_reason": inst.get("roll_reason"),
                         "exit_reason": inst.get("exit_reason")},
        })
    return resolutions


def open_recommendations(state: dict, now: datetime) -> list[dict]:
    """Recommendations with no resolution: unmatched, unoverridden,
    unsuperseded, and still inside their validity window."""
    resolved = {res.get("rec_id") for res in state.get("recommendation_resolutions", [])
                if res.get("rec_id")}
    out = []
    for r in state.get("recommendations", []) or []:
        if r.get("rec_id") in resolved:
            continue
        valid = _parse_ts(r.get("valid_until"))
        if valid is not None and now > valid:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# 3) Order-fidelity ledger
# ---------------------------------------------------------------------------
def _check(status: str, defect: str | None = None, **detail) -> dict:
    out = {"status": status}
    if defect:
        out["defect"] = defect
    if detail:
        out["detail"] = detail
    return out


def _grade_lifecycle(events: list[dict]) -> dict:
    prior_seen = None
    for ev in events:
        prior, new = ev.get("prior_state"), ev.get("new_state")
        if prior_seen is not None and prior is not None and prior != prior_seen:
            return _check(CheckStatus.FAIL, FidelityDefect.EVENT_CHAIN_GAP,
                          expected_prior=prior_seen, event_prior=prior, at=ev.get("at"))
        if not olc.is_legal_transition(prior, new):
            return _check(CheckStatus.FAIL, FidelityDefect.ILLEGAL_TRANSITION,
                          prior=prior, new=new, at=ev.get("at"))
        prior_seen = new
    if prior_seen == olc.LOCKED_UNKNOWN:
        return _check(CheckStatus.FAIL, FidelityDefect.HARD_LOCKED)
    return _check(CheckStatus.PASS)


def _grade_slippage(exec_ids: list, executions_by_id: dict, state: dict,
                    bound_pct: float) -> dict:
    """Adverse fill vs the reference mid, reusing slippage.py's exact math (the
    same figures the History tab reports). Bound is a fraction of mid."""
    worst = None
    gid = None
    for eid in exec_ids:
        e = executions_by_id.get(eid)
        if not e:
            continue
        gid = gid or e.get("roll_group_id")
        s = slippage._fill_slippage(e)
        if s is not None:
            frac = s["slippage_pct"] / 100.0
            worst = frac if worst is None else max(worst, frac)
    if gid:
        for r in slippage._roll_net_slippage(state):
            if r.get("roll_group_id") == gid:
                frac = r["net_slippage_pct"] / 100.0
                worst = frac if worst is None else max(worst, frac)
    if worst is None:
        return _check(CheckStatus.NOT_APPLICABLE)
    if worst > bound_pct + 1e-9:
        return _check(CheckStatus.FAIL, FidelityDefect.SLIPPAGE_EXCEEDED,
                      worst_adverse_pct=round(worst * 100, 3),
                      bound_pct=round(bound_pct * 100, 3))
    return _check(CheckStatus.PASS, worst_adverse_pct=round(worst * 100, 3),
                  bound_pct=round(bound_pct * 100, 3))


_MULTI_LEG_INTENTS = {"open", "open_position_atomic", "exit",
                      "close_position_atomic", "roll_short", "roll_leap"}


def _grade_orphan(intent: str | None, final_state: str | None,
                  exec_ids: list) -> dict:
    if intent not in _MULTI_LEG_INTENTS:
        return _check(CheckStatus.NOT_APPLICABLE)
    if final_state in (olc.PARTIAL_FILL_CANCELED,):
        return _check(CheckStatus.FAIL, FidelityDefect.PARTIAL_FILL,
                      final_state=final_state)
    if final_state in (olc.FILLED, olc.FILLED_DURING_CANCEL):
        if len(exec_ids) < 2:
            # A two-leg ticket that terminal-filled must have committed BOTH
            # legs; one committed execution means a naked/orphan leg. The
            # fill-during-cancel race is the canonical producer.
            return _check(CheckStatus.FAIL, FidelityDefect.ORPHAN_LEG,
                          final_state=final_state, committed_legs=len(exec_ids))
        return _check(CheckStatus.PASS, committed_legs=len(exec_ids))
    if final_state in (olc.CANCELED, olc.REJECTED, olc.EXPIRED):
        return _check(CheckStatus.PASS, committed_legs=0)
    return _check(CheckStatus.PENDING)


def _grade_cancel(events: list[dict], final_state: str | None,
                  now: datetime) -> dict:
    requested = [ev for ev in events if ev.get("new_state") == olc.CANCEL_REQUESTED]
    if not requested:
        return _check(CheckStatus.NOT_APPLICABLE)
    if final_state is not None and olc.is_terminal(final_state):
        return _check(CheckStatus.PASS, confirmed_state=final_state)
    last_at = _parse_ts(events[-1].get("at"))
    stale_after = timedelta(minutes=config.FIDELITY_CANCEL_CONFIRM_STALE_MIN)
    if last_at is not None and now - last_at > stale_after:
        # Cancel requested, never confirmed terminal at the broker — the
        # pending_cancel escape path. Requested is not dead (rule 2).
        return _check(CheckStatus.FAIL, FidelityDefect.CANCEL_NOT_CONFIRMED_DEAD,
                      last_state=final_state, last_event_at=events[-1].get("at"))
    return _check(CheckStatus.PENDING)


def _ticket_pass(checks: dict) -> bool | None:
    """All applicable checks passing. NOT_YET_IMPLEMENTED is excluded from the
    ticket verdict (it blocks graduation globally instead — never a silent
    pass, never a spurious per-ticket fail). PENDING => verdict not yet in."""
    statuses = [c["status"] for c in checks.values()]
    if CheckStatus.FAIL in statuses:
        return False
    if CheckStatus.PENDING in statuses:
        return None
    return True


def derive_order_fidelity(state: dict, now: datetime) -> dict:
    """Grade every order lifecycle. Live tickets replay order_events; paper
    tickets (flagged paper) grade what a paper fill can express. Verdicts are
    MERGE-RETAINED: order_events caps at 1000, so a graded ticket must outlive
    its events rolling off the log — re-derivation only overwrites records whose
    source events are still present."""
    existing = dict(state.get("order_fidelity") or {})
    receipts_by_order: dict[str, list] = {}
    for r in state.get("order_receipts", []) or []:
        oid = str(r.get("order_id") or "")
        if oid:
            receipts_by_order.setdefault(oid, []).extend(r.get("execution_ids") or [])
    executions_by_id = {e.get("id"): e for e in state.get("executions", [])}
    since = _parse_ts((state.get("metadata") or {}).get("trust_layer_since"))

    events_by_order: dict[str, list[dict]] = {}
    for ev in state.get("order_events", []) or []:
        oid = str(ev.get("order_id") or "")
        if oid:
            events_by_order.setdefault(oid, []).append(ev)

    out = existing
    for oid, events in events_by_order.items():
        events = sorted(events, key=lambda ev: ev.get("seq") or 0)
        final_state = events[-1].get("new_state")
        intent = events[-1].get("intent") or events[0].get("intent")
        exec_ids = receipts_by_order.get(oid, [])
        bound = config.REC_MAX_SLIPPAGE_PCT_OF_MID
        # A ticket staged from a recommendation carries its own bound.
        for eid in exec_ids:
            e = executions_by_id.get(eid) or {}
            rid = e.get("source_rec_id")
            if rid:
                for rec in state.get("recommendations", []) or []:
                    if rec.get("rec_id") == rid and rec.get("proposed_ticket"):
                        bound = rec["proposed_ticket"].get("max_slippage_pct_of_mid", bound)
                        break
                break
        checks = {
            FidelityCheck.LIFECYCLE_LEGAL: _grade_lifecycle(events),
            FidelityCheck.SLIPPAGE_IN_BOUND: _grade_slippage(exec_ids, executions_by_id,
                                                             state, bound),
            FidelityCheck.NO_ORPHAN_LEG: _grade_orphan(intent, final_state, exec_ids),
            FidelityCheck.CANCEL_CONFIRMED_DEAD: _grade_cancel(events, final_state, now),
            # Post-fill reconciliation (positions + buying-power diff) is a
            # separate work item; NEVER silently pass in its absence.
            FidelityCheck.RECONCILED_CLEAN: _check(CheckStatus.NOT_YET_IMPLEMENTED),
        }
        out[oid] = {
            "order_id": oid, "paper": False,
            "ticker": events[-1].get("ticker"), "intent": intent,
            "action_type": _INTENT_ACTION.get(intent or ""),
            "state": final_state, "terminal": olc.is_terminal(final_state),
            "checks": checks, "pass": _ticket_pass(checks),
            "graded_at": _iso(now),
        }
        # Refine roll tickets to DEFEND when the committed legs say so.
        if intent == "roll_short":
            for eid in exec_ids:
                e = executions_by_id.get(eid) or {}
                mapped = _ROLL_REASON_ACTION.get(e.get("roll_reason"))
                if mapped:
                    out[oid]["action_type"] = mapped
                    break

    # Paper tickets: grade multi-leg completeness on execution groups.
    groups: dict[str, dict] = {}
    for e in state.get("executions", []) or []:
        if e.get("live_transmitted") is True:
            continue
        at = _parse_ts(e.get("date"))
        if at is None or (since is not None and at < since):
            continue
        gid = e.get("open_id") or e.get("roll_group_id")
        if not gid:
            continue
        g = groups.setdefault(str(gid), {"execution_ids": [], "ticker": e.get("ticker"),
                                         "kind": "open" if e.get("open_id") else "roll_short",
                                         "roll_reason": e.get("roll_reason"), "at": at})
        g["execution_ids"].append(e.get("id"))
    for gid, g in groups.items():
        oid = f"paper:{gid}"
        legs = len(g["execution_ids"])
        orphan = (_check(CheckStatus.PASS, committed_legs=legs) if legs >= 2 else
                  _check(CheckStatus.FAIL, FidelityDefect.ORPHAN_LEG, committed_legs=legs))
        checks = {
            FidelityCheck.LIFECYCLE_LEGAL: _check(CheckStatus.NOT_APPLICABLE),
            FidelityCheck.SLIPPAGE_IN_BOUND: _check(CheckStatus.NOT_APPLICABLE),
            FidelityCheck.NO_ORPHAN_LEG: orphan,
            FidelityCheck.CANCEL_CONFIRMED_DEAD: _check(CheckStatus.NOT_APPLICABLE),
            FidelityCheck.RECONCILED_CLEAN: _check(CheckStatus.NOT_YET_IMPLEMENTED),
        }
        action_type = (ActionType.ENTER if g["kind"] == "open"
                       else _ROLL_REASON_ACTION.get(g.get("roll_reason")))
        out[oid] = {
            "order_id": oid, "paper": True, "ticker": g["ticker"],
            "intent": g["kind"], "action_type": action_type,
            "state": "PAPER_FILLED", "terminal": True,
            "checks": checks, "pass": _ticket_pass(checks),
            "graded_at": _iso(now),
        }
    return out


# ---------------------------------------------------------------------------
# 4) Trust scoreboard + graduation
# ---------------------------------------------------------------------------
def _timeliness(recs: list[dict], actions: list[dict]) -> dict:
    """Per emitted actionable rec: lag from condition-first-true to emission,
    plus the late-after-action flag (the operator acted between the condition
    turning true and the engine committing — the engine was chasing, not
    leading)."""
    rows = []
    for r in recs:
        if r.get("action_type") == ActionType.NO_ACTION:
            continue
        snap = r.get("input_snapshot") or {}
        first = _parse_ts(snap.get("condition_first_true_at"))
        emitted = _parse_ts(r.get("emitted_at"))
        lag_days = (round((emitted - first).total_seconds() / 86400, 2)
                    if first is not None and emitted is not None else None)
        late_after_action = False
        if emitted is not None:
            window_start = first if first is not None else emitted - timedelta(days=7)
            for inst in actions:
                if (inst["action_type"] == r.get("action_type")
                        and inst["ticker"] == (r.get("ticker") or "").upper()
                        and window_start <= inst["at"] < emitted):
                    late_after_action = True
                    break
        rows.append({"rec_id": r.get("rec_id"), "action_type": r.get("action_type"),
                     "ticker": r.get("ticker"), "emission_lag_days": lag_days,
                     "late_after_action": late_after_action})
    lags = [x["emission_lag_days"] for x in rows if x["emission_lag_days"] is not None]
    return {
        "rows": rows[-50:],
        "avg_emission_lag_days": round(sum(lags) / len(lags), 2) if lags else None,
        "max_emission_lag_days": max(lags) if lags else None,
        "late_after_action_count": sum(1 for x in rows if x["late_after_action"]),
    }


_GRADABLE = (ActionType.ROLL_OUT, ActionType.ROLL_DOWN, ActionType.DEFEND,
             ActionType.EXIT, ActionType.ENTER)


def _graduation(action_type: str, window_res: list[dict], fidelity: list[dict],
                reconciliation_ok: bool) -> dict:
    """Automation eligibility for one action type over its trailing window.
    Display-only: nothing anywhere consumes this to place an order."""
    weeks = config.GRAD_MIN_WEEKS.get(action_type)
    failing: list[str] = []
    if weeks is None:
        failing.append("action type is never auto-eligible in this iteration"
                       if action_type == ActionType.ENTER else "not a gradable action type")
    matched = [r for r in window_res if r["status"] == Resolution.EXECUTED_MATCHED]
    live_matched = [r for r in matched if r.get("live")]
    overridden = [r for r in window_res if r["status"] == Resolution.OVERRIDDEN]
    misses = [r for r in window_res if r["status"] == Resolution.COVERAGE_MISS]
    if len(live_matched) < config.GRAD_MIN_LIVE_CYCLES:
        failing.append(f"live matched cycles {len(live_matched)} < "
                       f"GRAD_MIN_LIVE_CYCLES {config.GRAD_MIN_LIVE_CYCLES}")
    if misses:
        failing.append(f"{len(misses)} coverage miss(es) in window (HARD: must be 0)")
    decided = len(matched) + len(overridden)
    override_rate = (len(overridden) / decided) if decided else 0.0
    if override_rate > config.GRAD_MAX_OVERRIDE_RATE + 1e-9:
        failing.append(f"override rate {override_rate:.2f} > "
                       f"GRAD_MAX_OVERRIDE_RATE {config.GRAD_MAX_OVERRIDE_RATE}")
    if any(r.get("reason") == "DISAGREE_ACTION" for r in overridden):
        failing.append("unresolved DISAGREE_ACTION override(s) in window")
    live_fidelity = [f for f in fidelity if not f.get("paper") and f.get("pass") is not None]
    if any(f["pass"] is False for f in live_fidelity):
        failing.append("fidelity failures in window (HARD: pass rate must be 100%)")
    if not reconciliation_ok:
        failing.append("reconciliation NOT_YET_IMPLEMENTED — no action type may "
                       "graduate until the post-fill reconciliation layer ships")
    return {
        "action_type": action_type,
        "eligible": not failing,
        "failing": failing,
        "window_weeks": weeks,
        "live_matched": len(live_matched),
        "matched": len(matched),
        "overridden": len(overridden),
        "coverage_misses": len(misses),
        "override_rate": round(override_rate, 3),
    }


def scoreboard(state: dict, resolutions: list[dict], fidelity_map: dict,
               now: datetime) -> dict:
    recs = state.get("recommendations", []) or []
    actions = map_actions(state)
    by_type: dict[str, dict] = {}
    fidelity = list(fidelity_map.values())
    # RECONCILED_CLEAN is NOT_YET_IMPLEMENTED for every ticket in this version.
    reconciliation_ok = bool(fidelity) and all(
        f["checks"][FidelityCheck.RECONCILED_CLEAN]["status"] == CheckStatus.PASS
        for f in fidelity)
    if not fidelity:
        reconciliation_ok = False

    for at in _GRADABLE:
        res_t = [r for r in resolutions
                 if r.get("action_type") == at
                 and r["status"] in (Resolution.EXECUTED_MATCHED, Resolution.OVERRIDDEN,
                                     Resolution.COVERAGE_MISS)]
        weeks = config.GRAD_MIN_WEEKS.get(at)
        cutoff = now - timedelta(weeks=weeks) if weeks else None
        window_res = [r for r in res_t
                      if cutoff is None or (_parse_ts(r.get("at")) or now) >= cutoff]
        matched = [r for r in res_t if r["status"] == Resolution.EXECUTED_MATCHED]
        overridden = [r for r in res_t if r["status"] == Resolution.OVERRIDDEN]
        misses = [r for r in res_t if r["status"] == Resolution.COVERAGE_MISS]
        total_manual = len(matched) + len(misses)
        decided = len(matched) + len(overridden)
        override_breakdown: dict[str, int] = {}
        for r in overridden:
            override_breakdown[r.get("reason") or "?"] = \
                override_breakdown.get(r.get("reason") or "?", 0) + 1
        fid_t = [f for f in fidelity if f.get("action_type") == at]
        fid_graded = [f for f in fid_t if f.get("pass") is not None]
        fid_pass = [f for f in fid_graded if f["pass"]]
        by_type[at] = {
            "coverage": {
                "matched": len(matched), "total_manual_actions": total_manual,
                "rate": round(len(matched) / total_manual, 3) if total_manual else None,
                "misses": misses,
            },
            "precision": {
                "executed_matched": len(matched), "overridden": len(overridden),
                "rate": round(len(matched) / decided, 3) if decided else None,
                "override_breakdown": override_breakdown,
            },
            "fidelity": {
                "graded": len(fid_graded), "passed": len(fid_pass),
                "rate": round(len(fid_pass) / len(fid_graded), 3) if fid_graded else None,
            },
            "graduation": _graduation(at, window_res, fid_t, reconciliation_ok),
        }

    open_recs = open_recommendations(state, now)
    return {
        "as_of": _iso(now),
        "since": (state.get("metadata") or {}).get("trust_layer_since"),
        "by_action_type": by_type,
        "timeliness": _timeliness(recs, actions),
        "open_recommendations": len(open_recs),
        "open_actionable": sum(1 for r in open_recs
                               if r.get("action_type") != ActionType.NO_ACTION),
        "totals": {
            "recommendations": len(recs),
            "all_clear": sum(1 for r in recs
                             if r.get("trigger_rule") == TriggerRule.ALL_CLEAR),
            "coverage_misses": sum(1 for r in resolutions
                                   if r["status"] == Resolution.COVERAGE_MISS),
            "fidelity_failures": sum(1 for f in fidelity if f.get("pass") is False),
        },
        "reconciliation_status": ("NOT_YET_IMPLEMENTED"),
        "automation_note": ("Display-only. No automation switch exists; while "
                            "reconciliation is NOT_YET_IMPLEMENTED no action "
                            "type may graduate."),
    }


# ---------------------------------------------------------------------------
# recompute_derived hook
# ---------------------------------------------------------------------------
def recompute(state: dict, now: datetime) -> None:
    """Called by logging_handler.recompute_derived after every append. Rebuilds
    recommendation_resolutions + trust_scoreboard and refreshes order_fidelity
    (merge-retain). Purely derived — safe to run any number of times."""
    resolutions = resolve(state, now)
    fidelity = derive_order_fidelity(state, now)
    state["recommendation_resolutions"] = resolutions
    state["order_fidelity"] = fidelity
    state["trust_scoreboard"] = scoreboard(state, resolutions, fidelity, now)
