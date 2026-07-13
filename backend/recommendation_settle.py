"""PENDING_SETTLE lifecycle for recommendations deferred by the market-settle gate.

A recommendation emitted inside a blocked execution window still ALERTS
immediately — only its *order* is deferred. It carries a ``settle`` block
(``executable_at`` + a status + an append-only event log) so the UI can render a
countdown and the operator can *pre-approve* it. A release pass (in
``recommendation_runner``) re-validates the trigger at ``executable_at``: a
pre-approved defense whose gap has filled SELF-CANCELS rather than firing on a
stale trigger. Every transition appends to the record, so a future automation
switch inherits the identical lifecycle.

The core record stays immutable — only the additive ``settle`` block is written,
never the emitted claim fields (``emitted_at`` / ``action_type`` /
``proposed_ticket`` / ``valid_until``). These helpers are pure given ``state`` and
``now``; the impure release orchestration lives in the runner.
"""
from __future__ import annotations

from datetime import datetime, timezone

from execution_gate import GateAction
from rec_types import ActionType


class SettleStatus:
    """Lifecycle of a settle-deferred recommendation. PENDING_SETTLE is the only
    non-terminal state; the rest are terminal."""
    PENDING = "PENDING_SETTLE"
    RELEASED = "RELEASED"            # window opened, trigger re-validated, executable now
    SELF_CANCELED = "SELF_CANCELED"  # trigger no longer valid at release (e.g. gap filled)
    EXPIRED = "EXPIRED"             # validity window elapsed before release
    EXECUTED = "EXECUTED"           # auto-submitted on release (pre-approved)


TERMINAL = frozenset({SettleStatus.RELEASED, SettleStatus.SELF_CANCELED,
                      SettleStatus.EXPIRED, SettleStatus.EXECUTED})

# Recommendation ActionType -> the gate's action vocabulary.
ACTION_TO_GATE = {
    ActionType.ENTER: GateAction.ENTRY,
    ActionType.ROLL_OUT: GateAction.ROLL_SHORT,
    ActionType.ROLL_DOWN: GateAction.DEFENSE,
    ActionType.DEFEND: GateAction.DEFENSE,
    ActionType.EXIT: GateAction.EXIT_KILL,
}


def gate_action_for(rec: dict) -> str | None:
    """The gate action for a recommendation, or ``None`` for a non-actionable one."""
    return ACTION_TO_GATE.get(rec.get("action_type"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        txt = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _append_event(rec: dict, status: str, now: datetime, note: str | None) -> None:
    events = rec["settle"].setdefault("events", [])
    events.append({"at": _iso(now), "status": status, "note": note})


def stage(rec: dict, verdict, now: datetime) -> bool:
    """If ``verdict`` blocks the order, attach a PENDING_SETTLE ``settle`` block to
    ``rec`` (created + pending events) and return True. An allowed verdict stages
    nothing (the recommendation is executable now). Idempotent — an already-staged
    rec is left untouched."""
    if verdict is None or verdict.allowed:
        return False
    if rec.get("settle"):
        return False
    rec["settle"] = {
        "status": SettleStatus.PENDING,
        "executable_at": _iso(verdict.executable_at) if verdict.executable_at else None,
        "reason": verdict.reason,
        "gate_action": gate_action_for(rec),
        "pre_approved": False,
        "events": [],
    }
    _append_event(rec, SettleStatus.PENDING, now,
                  f"deferred by {verdict.reason}; executable at "
                  f"{rec['settle']['executable_at']}")
    return True


def mark(rec: dict, status: str, now: datetime, note: str | None = None) -> None:
    """Transition a staged rec to ``status`` and append the lifecycle event."""
    if not rec.get("settle"):
        return
    rec["settle"]["status"] = status
    _append_event(rec, status, now, note)


def is_pending(rec: dict) -> bool:
    return (rec.get("settle") or {}).get("status") == SettleStatus.PENDING


def pending(state: dict) -> list[dict]:
    return [r for r in state.get("recommendations", []) if is_pending(r)]


def executable_at(rec: dict) -> datetime | None:
    return parse_ts((rec.get("settle") or {}).get("executable_at"))


def due(state: dict, now: datetime) -> list[dict]:
    """Pending recs whose ``executable_at`` has arrived (release candidates)."""
    out = []
    for r in pending(state):
        ea = executable_at(r)
        if ea is not None and now >= ea:
            out.append(r)
    return out


def is_expired(rec: dict, now: datetime) -> bool:
    """True when the rec's validity window elapsed (stale before it could release)."""
    valid = parse_ts(rec.get("valid_until"))
    return valid is not None and now > valid


def find(state: dict, rec_id: str) -> dict | None:
    for r in state.get("recommendations", []):
        if r.get("rec_id") == rec_id:
            return r
    return None


def set_pre_approved(state: dict, rec_id: str, on: bool, now: datetime) -> dict | None:
    """Operator pre-approval toggle: a pre-approved pending rec auto-submits at
    release IF its trigger re-validates. Only a PENDING rec can be (un)pre-approved."""
    rec = find(state, rec_id)
    if rec is None or not is_pending(rec):
        return None
    rec["settle"]["pre_approved"] = bool(on)
    _append_event(rec, SettleStatus.PENDING, now,
                  "pre-approved for auto-release" if on else "pre-approval removed")
    return rec
