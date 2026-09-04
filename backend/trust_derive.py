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

Scope: matchable operator actions are ENTER (buy_shares opening a fresh base, or
the legacy buy_leap / atomic open, excluding scale-ins), ROLL_OUT (roll pairs
with reason scheduled / 75%-rule / earnings), DEFEND (roll pairs with reason
defend), and EXIT (a sell_shares that closes the whole base, or the legacy
close_leap, excluding LEAP rolls). Mechanical LEAP rolls, kill-switch-exit roll
legs (part of an exit), scale-in adds and partial trims, called-away deliveries
and put assignments, standalone leg repairs, and reconciliation adjustments are
out of scope by rule and never synthesize misses — the operator doc lists them.

Shares are scoped by the BASE-LEG BALANCE, not by the action name: a buy into an
existing share base is a scale-in and a sale that leaves shares standing is a
trim, and neither is a graded ENTER/EXIT. The balance is replayed from the whole
execution log (including records before trust_layer_since, and including the
mechanical put_assigned / close_shares_assigned / EQUITY-leg adjustment that move
shares without being operator actions), so a position opened before the trust
layer still exits to zero correctly, and a buy voided the same day by a
reconciliation adjustment does not leave a phantom balance that mis-scopes the
next real trade as a scale-in.
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
    "extrinsic-captured": ActionType.ROLL_OUT,   # ROLL_EXTRINSIC_CAPTURED early roll
    "earnings": ActionType.ROLL_OUT,
    "defend": ActionType.DEFEND,
    # A roll adopted from the broker feed (done by hand at Schwab) carries no
    # sub-type — the operator never picked "defend" vs "scheduled" in a modal.
    # It is graded as the generic roll and matches ANY open roll-family rec on
    # the position (see _family); the resolution records what it really was.
    "broker_manual_roll": ActionType.ROLL_OUT,
    # kill-switch-exit rolls are part of an exit in progress — the close_leap
    # carries the EXIT; grading the roll leg separately would double-count.
    "kill-switch-exit": None,
}

# Action FAMILY — the move the operator actually made, coarser than the graded
# action type. A roll is a roll whether the engine called it DEFEND, ROLL_OUT or
# ROLL_DOWN; the operator choosing a different roll reason (or rolling by hand
# at Schwab, where there is no reason to choose) is still the engine's roll
# being taken, and the resolution carries the difference as ``action_delta``
# rather than leaving the rec open AND synthesizing a coverage miss.
_FAMILY = {
    ActionType.ROLL_OUT: "ROLL", ActionType.ROLL_DOWN: "ROLL", ActionType.DEFEND: "ROLL",
    ActionType.EXIT: "EXIT", ActionType.ENTER: "ENTER",
}

# The derived override reason: the engine had committed to one move on a
# position and the operator made a DIFFERENT one (rolled when told to exit,
# exited when told to roll). Never operator-supplied — the dismiss endpoint
# rejects it — and never a coverage miss (the engine did commit; the operator
# disagreed with their hands instead of the Dismiss button).
ACTED_DIFFERENTLY = "ACTED_DIFFERENTLY"


def _family(action_type) -> str | None:
    return _FAMILY.get(action_type)


def _action_source(inst: dict, executions_by_id: dict) -> str:
    """Where the move came from: the engine's card (source_rec_id), the app's
    own modal/ticket by hand, or adopted from the broker feed (done at Schwab)."""
    if inst.get("source_rec_id"):
        return "engine_card"
    for eid in inst.get("execution_ids") or []:
        e = executions_by_id.get(eid) or {}
        if e.get("source") == "broker_manual":
            return "broker_manual"
    return "app_manual"

# Order ACTION -> graded action type. Keyed on the bare action (see
# _order_action): an order event's ``intent`` is the per-position lock KEY,
# "TICKER:action", never a bare action, so looking a raw intent up here matches
# nothing.
_INTENT_ACTION = {
    "open": ActionType.ENTER,
    "open_position_atomic": ActionType.ENTER,
    "buy_leap": ActionType.ENTER,
    "buy_shares": ActionType.ENTER,      # the shares-primary entry order
    "exit": ActionType.EXIT,
    "close_position_atomic": ActionType.EXIT,
    "close_leap": ActionType.EXIT,
    # The shares-primary exit order. Unlike map_actions — which grades only a
    # sale that closes the whole base — this grades the ORDER, so a trim's
    # ticket is graded too: its lifecycle has to be legal either way, and a
    # cleanly filled trim passes.
    "sell_shares": ActionType.EXIT,
    "roll_short": ActionType.ROLL_OUT,   # refined to DEFEND via the roll_reason
    "roll_leap": None,                   # mechanical LEAP roll — out of scope
    "sell_short": None,
    "close_short": None,
    # Mechanical or out-of-scope, listed so they read as decided rather than
    # forgotten (an unlisted action already grades as None).
    "close_shares_assigned": None,       # called-away delivery, not an operator act
    "put_assigned": None,
    "put_opened": None,
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
# Executions that move the owned-share base, and the field each carries the size
# in. put_assigned / close_shares_assigned are MECHANICAL — they move shares but
# are never graded as operator actions; they appear here only so the balance the
# ENTER/EXIT scoping reads stays true. `adjustment` is the same kind of
# mechanical mover (see _share_delta below) but carries its OWN signed delta
# rather than a fixed-sign field, so it does not fit this table.
_SHARE_DELTA = {
    "buy_shares": ("qty", +1),
    "put_assigned": ("shares_received", +1),
    "sell_shares": ("qty", -1),
    "close_shares_assigned": ("qty", -1),
}


def _share_delta(e: dict) -> int | None:
    """The signed share-count change one execution makes, or None when it does
    not move the base at all.

    `adjustment` (executor._adjustment, the reconciliation-correction path — "the
    operator committing truth forward") is handled separately from the table
    above because its EQUITY leg carries an already-signed `quantity_delta`
    rather than a fixed-sign qty field; an OPTION-leg adjustment (correcting a
    short/LEAP count) never touches shares and returns None here.

    Excluding `adjustment` was a real gap, not a hypothetical: a buy_shares
    entered in error and voided the same day by `quantity_delta: -100, reason:
    "Trade never happened"` left the replayed balance permanently 100 shares
    ahead of the real book. The NEXT buy_shares then read as a scale-in into a
    position that no longer existed and was silently dropped from ENTER grading
    — invisible to the trust layer, on the position that was still open and had
    just been rolled — while the voided original kept surfacing as the only
    graded coverage miss."""
    action = e.get("action")
    if action == "adjustment":
        if (e.get("instrument_type") or "").upper() != "EQUITY":
            return None
        try:
            return int(e.get("quantity_delta") or 0)
        except (TypeError, ValueError):
            return 0
    spec = _SHARE_DELTA.get(action)
    if spec is None:
        return None
    field, sign = spec
    try:
        return sign * int(e.get(field) or 0)
    except (TypeError, ValueError):
        return 0


def _share_balances(executions: list[dict]) -> dict[int, tuple[int, int]]:
    """Replay the owned-share base per ticker over the WHOLE log. Returns
    {execution index: (before, after)} for every share-moving execution — the
    balance context that decides whether a buy is a fresh entry and a sale is a
    full exit.

    Replayed in DATE order, not list order: the log is append-only, but
    ``append_execution`` only ``setdefault``s the timestamp, so a backdated
    record (an assignment booked on its assignment_date, a seeded history) can
    land after a newer one. Log position breaks ties, keeping the legs of one
    order in the order they were written. A record whose date will not parse is
    left out of the balance exactly as ``map_actions`` leaves it out of grading —
    guessing its place would silently mis-scope a real entry or exit."""
    running: dict[str, int] = {}
    out: dict[int, tuple[int, int]] = {}
    order = sorted((i for i, e in enumerate(executions)
                    if _share_delta(e) is not None and _parse_ts(e.get("date"))),
                   key=lambda i: (_parse_ts(executions[i].get("date")), i))
    for idx in order:
        e = executions[idx]
        delta = _share_delta(e)
        t = (e.get("ticker") or "").upper()
        before = running.get(t, 0)
        after = max(before + delta, 0)
        running[t] = after
        out[idx] = (before, after)
    return out


def map_actions(state: dict) -> list[dict]:
    """Classify the immutable executions into matchable operator-action
    instances: {action_type, ticker, at, execution_ids, strike, net, live}."""
    since = _parse_ts((state.get("metadata") or {}).get("trust_layer_since"))
    out: list[dict] = []
    rolls: dict[str, dict] = {}
    exits: dict[tuple, dict] = {}
    executions = state.get("executions", [])
    balances = _share_balances(executions)
    for idx, e in enumerate(executions):
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
        elif action == "buy_shares":
            # The shares-primary ENTER. A builder/lot add carries the lot_add
            # stamp, and any buy into a standing base is a scale-in whether or not
            # it is stamped — both are out of scope, exactly like leap_add.
            before, _after = balances.get(idx, (0, 0))
            if e.get("lot_add") or before > 0:
                continue
            out.append({
                "action_type": ActionType.ENTER, "ticker": t, "at": at,
                "execution_ids": [e.get("id")], "strike": None,
                "net": None, "live": e.get("live_transmitted") is True,
                "open_id": e.get("open_id"), "qty": e.get("qty"),
            })
        elif action == "sell_shares":
            # The shares-primary EXIT — but ONLY when the DAY closes the whole
            # base. Same-day sales collect into one instance (an exit split across
            # fills is one action), and the day is kept only if the last of them
            # leaves zero shares: a trim that leaves shares standing is a
            # scale-out, and grading it as an unrecommended exit would synthesize
            # a coverage miss for a position the operator never exited.
            _before, after = balances.get(idx, (0, 0))
            key = (t, str(e.get("date"))[:10])
            inst = exits.setdefault(key, {
                "action_type": ActionType.EXIT, "ticker": t, "at": at,
                "execution_ids": [], "strike": None, "net": None,
                "live": False, "exit_reason": e.get("exit_reason"),
            })
            inst["execution_ids"].append(e.get("id"))
            inst["at"] = max(inst["at"], at)
            inst["live"] = inst["live"] or e.get("live_transmitted") is True
            inst["_shares_after"] = after
        elif action == "close_leap":
            # Same-day close_leap legs on one ticker are ONE exit action (a
            # multi-tranche close writes one record per leg).
            key = (t, str(e.get("date"))[:10])
            inst = exits.setdefault(key, {
                "action_type": ActionType.EXIT, "ticker": t, "at": at,
                "execution_ids": [], "strike": e.get("strike"), "net": None,
                "live": False, "exit_reason": e.get("exit_reason"),
            })
            # The bucket may have been opened by a same-day share sale, which
            # carries neither field — fill them from the LEAP leg.
            inst["_leap_close"] = True
            if inst.get("strike") is None:
                inst["strike"] = e.get("strike")
            if inst.get("exit_reason") is None:
                inst["exit_reason"] = e.get("exit_reason")
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
    for inst in exits.values():
        # A shares day that ended with shares still standing was a trim, not an
        # exit — unless the same bucket also holds a legacy close_leap, which is
        # an exit on its own terms.
        shares_after = inst.pop("_shares_after", None)
        if (shares_after is not None and shares_after > 0
                and not inst.pop("_leap_close", False)):
            continue
        inst.pop("_leap_close", None)
        out.append(inst)
    by_id = {e.get("id"): e for e in state.get("executions", [])}
    for inst in out:
        # source_rec_id passthrough: an execution staged from a recommendation
        # card carries the rec id; the anchor exec's value wins.
        for eid in inst["execution_ids"]:
            e = by_id.get(eid) or {}
            if e.get("source_rec_id"):
                inst["source_rec_id"] = e["source_rec_id"]
                break
        inst["source"] = _action_source(inst, by_id)
    out.sort(key=lambda i: i["at"])
    return out


def miss_key(execution_ids) -> str:
    """The stable identity of one coverage miss: its execution ids, sorted and
    joined. A miss has no rec_id (that is what makes it a miss), so this is what
    an acknowledgement is keyed on, and it is order-independent so the UI can
    hand back the ids in any order."""
    return ",".join(sorted(str(i) for i in (execution_ids or []) if i))


def _acks_by_key(state: dict) -> dict[str, dict]:
    """First acknowledgement per miss wins (mirrors overrides)."""
    out: dict[str, dict] = {}
    for ack in state.get("coverage_miss_acks", []) or []:
        out.setdefault(miss_key(ack.get("execution_ids")), ack)
    return out


# ---------------------------------------------------------------------------
# 2) Resolution matching
# ---------------------------------------------------------------------------
def resolve(state: dict, now: datetime) -> list[dict]:
    """Derive recommendation_resolutions from recs + overrides + executions."""
    recs = state.get("recommendations", []) or []
    acks = _acks_by_key(state)
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
    acted_differently: dict[str, dict] = {}  # rec_id -> derived override detail
    matched_actions: set[int] = set()

    def _still_claimable(r: dict, inst: dict) -> bool:
        """The rec is a live claim on this ticker at the instant of the action:
        same ticker, not already resolved, inside its validity window. A rec
        superseded by an ALL_CLEAR stays claimable for an action that happened
        BEFORE that all-clear was emitted — the engine cleared because the
        operator had already acted (a roll done at Schwab is only adopted after
        the next pass has seen the new short), and that is a match, not a miss.
        A rec replaced by another ACTION rec never matches (the successor is
        the claim)."""
        if (r.get("ticker") or "").upper() != inst["ticker"]:
            return False
        rid = str(r.get("rec_id"))
        if r.get("rec_id") in matched or rid in acted_differently or rid in overrides:
            return False
        if rid in superseded_by:
            successor = by_id.get(superseded_by[rid]) or {}
            succ_at = _parse_ts(successor.get("emitted_at"))
            if (successor.get("action_type") != ActionType.NO_ACTION
                    or succ_at is None or inst["at"] > succ_at):
                return False
        emitted = _parse_ts(r.get("emitted_at"))
        valid = _parse_ts(r.get("valid_until"))
        return (emitted is not None and valid is not None
                and emitted <= inst["at"] <= valid)

    def _latest(rs):
        return max(rs, key=lambda r: r.get("emitted_at") or "") if rs else None

    for idx, inst in enumerate(actions):
        fam = _family(inst["action_type"])
        live = [r for r in recs if r.get("action_type") != ActionType.NO_ACTION
                and _still_claimable(r, inst)]
        exact = [r for r in live if r.get("action_type") == inst["action_type"]]
        same_family = [r for r in live if r not in exact and _family(r.get("action_type")) == fam]
        chosen = None
        src = inst.get("source_rec_id")
        if src and any(r.get("rec_id") == src for r in live):
            chosen = by_id[src]           # the card the operator tapped, exactly
        elif exact:
            chosen = _latest(exact)
        elif same_family:
            chosen = _latest(same_family)  # a roll is a roll — noted as action_delta
        if chosen is None:
            # The engine had committed to a DIFFERENT move on this position (an
            # EXIT while the operator rolled, or vice versa): the rec resolves as
            # an override the operator made with their hands, and the action is
            # covered — the engine did not stay silent.
            other = _latest([r for r in live
                             if fam in ("ROLL", "EXIT") and _family(r.get("action_type")) in ("ROLL", "EXIT")])
            if other is not None:
                acted_differently[other["rec_id"]] = {
                    "rec_id": other["rec_id"], "status": Resolution.OVERRIDDEN,
                    "action_type": other.get("action_type"), "ticker": inst["ticker"],
                    "reason": ACTED_DIFFERENTLY, "derived": True,
                    "note": (f"operator executed {inst['action_type']} instead of "
                             f"{other.get('action_type')}"),
                    "executed_action_type": inst["action_type"],
                    "execution_ids": inst["execution_ids"], "source": inst.get("source"),
                    "live": inst["live"], "executed_at": _iso(inst["at"]),
                    "at": _iso(inst["at"]),
                }
                matched_actions.add(idx)
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
        # The resolution is keyed on the REC's action type (that is what the
        # scoreboard grades); what the operator actually did rides alongside.
        action_delta = (None if chosen.get("action_type") == inst["action_type"]
                        else f"{chosen.get('action_type')}->{inst['action_type']}")
        matched[chosen["rec_id"]] = {
            "rec_id": chosen["rec_id"], "status": Resolution.EXECUTED_MATCHED,
            "action_type": chosen.get("action_type"), "ticker": inst["ticker"],
            "executed_action_type": inst["action_type"],
            "roll_reason": inst.get("roll_reason"),
            "source": inst.get("source"),
            "execution_ids": inst["execution_ids"], "live": inst["live"],
            "executed_at": _iso(inst["at"]),
            "deltas": {
                "strike_delta": strike_delta,
                "credit_delta_vs_min": credit_delta,
                "action_delta": action_delta,
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
        if rid in acted_differently:
            resolutions.append(acted_differently[rid])
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
        key = miss_key(inst["execution_ids"])
        miss = {
            "rec_id": None, "status": Resolution.COVERAGE_MISS,
            "action_type": inst["action_type"], "ticker": inst["ticker"],
            "execution_ids": inst["execution_ids"], "live": inst["live"],
            "at": _iso(inst["at"]),
            "miss_key": key,
            "snapshot": {"strike": inst.get("strike"), "net": inst.get("net"),
                         "roll_reason": inst.get("roll_reason"),
                         "exit_reason": inst.get("exit_reason")},
        }
        ack = acks.get(key)
        if ack:
            # Classified, not excused: the miss stays a miss for coverage and
            # graduation; only the read (and the alert) change.
            miss["acknowledged"] = {"id": ack.get("id"), "reason": ack.get("reason"),
                                    "note": ack.get("note"), "at": ack.get("at")}
        resolutions.append(miss)
    return resolutions


def recent_resolutions(state: dict, now: datetime, days: int = 14) -> list[dict]:
    """Resolutions that connect an operator MOVE to an engine call (matched, or
    overridden by acting differently), newest first, within ``days`` — joined
    with the rec's trigger rule and proposed strike so a position card can say
    "engine called X on Tuesday; you did Y" without a second lookup."""
    by_id = {r.get("rec_id"): r for r in state.get("recommendations", []) or []}
    cutoff = now - timedelta(days=days)
    out = []
    for res in state.get("recommendation_resolutions", []) or []:
        if res.get("status") not in (Resolution.EXECUTED_MATCHED, Resolution.OVERRIDDEN):
            continue
        at = _parse_ts(res.get("at"))
        if at is None or at < cutoff:
            continue
        rec = by_id.get(res.get("rec_id")) or {}
        proposed_strike = None
        for leg in (rec.get("proposed_ticket") or {}).get("legs") or []:
            if leg.get("instruction") in ("SELL_TO_OPEN", "BUY_TO_OPEN"):
                proposed_strike = leg.get("strike")
                break
        out.append({**res, "trigger_rule": rec.get("trigger_rule"),
                    "emitted_at": rec.get("emitted_at"),
                    "proposed_strike": proposed_strike})
    out.sort(key=lambda r: r.get("at") or "", reverse=True)
    return out


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


_MULTI_LEG_ACTIONS = {"open", "open_position_atomic", "exit",
                      "close_position_atomic", "roll_short", "roll_leap"}


def _grade_orphan(action: str | None, final_state: str | None,
                  exec_ids: list) -> dict:
    if action not in _MULTI_LEG_ACTIONS:
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


def _order_action(events: list[dict]) -> str | None:
    """The bare order action for one order's events.

    An order event carries BOTH its action ("roll_short") and its ``intent`` —
    the per-position resubmission-lock key, ``executor._intent_key``, which is
    "TICKER:action". Grading keys off the action, so reading ``intent`` raw
    matched nothing for any live ticket: every one graded action_type None and,
    worse, skipped NO_ORPHAN_LEG as NOT_APPLICABLE, so an orphaned leg on a live
    atomic roll passed as clean.

    Prefers the event's own ``action`` and falls back to the intent key's
    suffix, so events written without one (older records, hand-built fixtures
    carrying a bare intent) still resolve. Ticker and action never contain ":",
    so the split is exact, and it is a no-op on an already-bare value."""
    for ev in reversed(events):
        action = ev.get("action")
        if action:
            return str(action)
    for ev in reversed(events):
        intent = ev.get("intent")
        if intent:
            return str(intent).rsplit(":", 1)[-1]
    return None


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
        action = _order_action(events)
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
            FidelityCheck.NO_ORPHAN_LEG: _grade_orphan(action, final_state, exec_ids),
            FidelityCheck.CANCEL_CONFIRMED_DEAD: _grade_cancel(events, final_state, now),
            # Post-fill reconciliation (positions + buying-power diff) is a
            # separate work item; NEVER silently pass in its absence.
            FidelityCheck.RECONCILED_CLEAN: _check(CheckStatus.NOT_YET_IMPLEMENTED),
        }
        out[oid] = {
            "order_id": oid, "paper": False,
            # ``intent`` stays the raw lock key (it is how the order is found in
            # the order-lock / journal records); ``action`` is what grading used.
            "ticker": events[-1].get("ticker"), "intent": intent, "action": action,
            "action_type": _INTENT_ACTION.get(action or ""),
            "state": final_state, "terminal": olc.is_terminal(final_state),
            "checks": checks, "pass": _ticket_pass(checks),
            "graded_at": _iso(now),
        }
        # Refine roll tickets to DEFEND when the committed legs say so.
        if action == "roll_short":
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
            "intent": g["kind"], "action": g["kind"], "action_type": action_type,
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
        # Where the matched moves came from (the card, the app by hand, or
        # Schwab by hand) and how many took a different roll than proposed —
        # "did I agree" split by how the agreement was expressed.
        matched_by_source: dict[str, int] = {}
        for r in matched:
            src = r.get("source") or "app_manual"
            matched_by_source[src] = matched_by_source.get(src, 0) + 1
        matched_diverged = sum(1 for r in matched if (r.get("deltas") or {}).get("action_delta"))
        fid_t = [f for f in fidelity if f.get("action_type") == at]
        fid_graded = [f for f in fid_t if f.get("pass") is not None]
        fid_pass = [f for f in fid_graded if f["pass"]]
        by_type[at] = {
            "coverage": {
                "matched": len(matched), "total_manual_actions": total_manual,
                "rate": round(len(matched) / total_manual, 3) if total_manual else None,
                "misses": misses,
                # Acknowledged misses are still misses (the rate above and the
                # graduation gate both count them); this only says how many the
                # operator has classified.
                "misses_acknowledged": sum(1 for m in misses if m.get("acknowledged")),
            },
            "precision": {
                "executed_matched": len(matched), "overridden": len(overridden),
                "rate": round(len(matched) / decided, 3) if decided else None,
                "override_breakdown": override_breakdown,
                "matched_by_source": matched_by_source,
                "matched_diverged": matched_diverged,
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
            "coverage_misses_acknowledged": sum(
                1 for r in resolutions
                if r["status"] == Resolution.COVERAGE_MISS and r.get("acknowledged")),
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
