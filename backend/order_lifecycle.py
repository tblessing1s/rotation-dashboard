"""Explicit order-lifecycle state machine (pure functions, no I/O).

The live-order path (place -> poll -> fill/cancel) previously spoke in ad-hoc
status strings scattered across executor.py. This module is the single source of
truth for what a Schwab order's state IS, which states are terminal, and — the
load-bearing CFM invariant — whether a NEW order for the same position intent may
be submitted yet (``NO_RESUBMIT_BEFORE_TERMINAL``).

Everything here is a pure function over plain dicts/strings so the whole lifecycle
is exercisable offline with a mocked broker and a mocked clock. Broker I/O lives
in executor.py; state persistence (append-only event log + per-position locks)
lives in logging_handler.py. This module knows nothing about either.

State graph (maps Schwab statuses to CFM-coded states):

    SUBMITTED -> WORKING -> { FILLED
                           | CANCEL_REQUESTED -> PENDING_CANCEL ->
                               { CANCELED | FILLED_DURING_CANCEL | PARTIAL_FILL_CANCELED }
                           | REJECTED | EXPIRED }

Plus one non-terminal HARD lock, LOCKED_UNKNOWN: the broker is unreachable and the
true order state is unknown. Resubmission is forbidden while unknown — a working
order might still be live at Schwab, and re-sending risks a double fill. Only an
operator (reconciliation / adjustment) clears it.
"""
from __future__ import annotations

# ---- Coded states ----------------------------------------------------------
SUBMITTED = "SUBMITTED"
WORKING = "WORKING"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
PENDING_CANCEL = "PENDING_CANCEL"
FILLED = "FILLED"
CANCELED = "CANCELED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
FILLED_DURING_CANCEL = "FILLED_DURING_CANCEL"
PARTIAL_FILL_CANCELED = "PARTIAL_FILL_CANCELED"
LOCKED_UNKNOWN = "LOCKED_UNKNOWN"

# Terminal at the broker — no further fills can happen.
TERMINAL = frozenset({FILLED, CANCELED, REJECTED, EXPIRED,
                      FILLED_DURING_CANCEL, PARTIAL_FILL_CANCELED})

# Terminal states that leave the position in a CLEAN, resubmittable place: nothing
# (or everything) filled and the fill is reconciled. A fresh order for the same
# intent is allowed once the prior order lands in one of these.
RESUBMIT_OK_STATES = frozenset({CANCELED, REJECTED, EXPIRED, FILLED})

# States that BLOCK resubmission even though (some of) them are terminal: the
# position is either live-and-under-review or its true state is unknown. The app
# flags and alerts; it never auto-resubmits or auto-fixes out of these.
REVIEW_BLOCKING = frozenset({FILLED_DURING_CANCEL, PARTIAL_FILL_CANCELED, LOCKED_UNKNOWN})


# The legal transition graph, as data — the docstring picture above, verbatim.
# Consumed by the order-fidelity grader (trust_derive.py) to check that every
# OBSERVED transition in the append-only order_events log was legal. SUBMITTED
# may settle terminal directly (a marketable order can fill before the first
# poll ever sees WORKING), and LOCKED_UNKNOWN is reachable from any live state
# (rule 5's hard lock) and exits only via reconciliation to a terminal state.
_LIVE = frozenset({WORKING, CANCEL_REQUESTED, PENDING_CANCEL})
LEGAL_TRANSITIONS = {
    SUBMITTED: frozenset({WORKING, FILLED, REJECTED, EXPIRED, CANCELED,
                          CANCEL_REQUESTED, PENDING_CANCEL, LOCKED_UNKNOWN}),
    # WORKING may settle PENDING_CANCEL (a broker-side cancel first seen by a
    # poll) or FILLED_DURING_CANCEL (a restart lost the local cancel-requested
    # event but the pending record remembers) — both real, rule-abiding paths.
    WORKING: frozenset({FILLED, CANCEL_REQUESTED, PENDING_CANCEL, REJECTED,
                        EXPIRED, CANCELED, FILLED_DURING_CANCEL,
                        PARTIAL_FILL_CANCELED, LOCKED_UNKNOWN}),
    # A fill that races the cancel is USUALLY coded FILLED_DURING_CANCEL, but
    # the plain-poll discovery path (order_status during the cancel retry loop
    # / startup reconciliation) books it as a plain FILLED — same rules, same
    # outcome, different discovery route. Both edges are legal.
    CANCEL_REQUESTED: frozenset({PENDING_CANCEL, CANCELED, FILLED,
                                 FILLED_DURING_CANCEL, PARTIAL_FILL_CANCELED,
                                 REJECTED, EXPIRED, LOCKED_UNKNOWN}),
    PENDING_CANCEL: frozenset({CANCELED, FILLED, FILLED_DURING_CANCEL,
                               PARTIAL_FILL_CANCELED, REJECTED, EXPIRED,
                               LOCKED_UNKNOWN}),
    LOCKED_UNKNOWN: TERMINAL,  # operator/reconciliation resolves it to terminal
    # Terminal states have no legal successors.
    FILLED: frozenset(), CANCELED: frozenset(), REJECTED: frozenset(),
    EXPIRED: frozenset(), FILLED_DURING_CANCEL: frozenset(),
    PARTIAL_FILL_CANCELED: frozenset(),
}


def is_legal_transition(prior: str | None, new: str | None) -> bool:
    """True when the (prior -> new) edge exists in the legal graph. A None prior
    is the placement itself: only SUBMITTED (or WORKING, for a placement that is
    recorded in one SUBMITTED->WORKING event) may open a lifecycle."""
    if prior is None:
        return new in (SUBMITTED, WORKING)
    if prior == new:
        # Idempotent re-record (e.g. a cancel retry re-stamping CANCEL_REQUESTED)
        # is not a lifecycle violation — only terminal states may not repeat.
        return prior not in TERMINAL
    return new in LEGAL_TRANSITIONS.get(prior, frozenset())


def is_terminal(state: str | None) -> bool:
    return state in TERMINAL


def map_broker_status(raw: str | None, *, filled_qty: float = 0, ordered_qty: float = 0,
                      cancel_requested: bool = False) -> str:
    """Map a raw Schwab order status (+ fill quantities + whether we've asked to
    cancel) to a CFM-coded state.

    ``cancel_requested`` disambiguates the post-DELETE world: a FILLED that lands
    after we asked to cancel is a fill-during-cancel (position is LIVE unexpectedly,
    high-priority alert), not a clean fill. A CANCELED with a partial fill is a
    PARTIAL_FILL_CANCELED (unbalanced position, defensive review), never a clean
    cancel. Quantities are LIVE-VERIFY fields — callers pass what Schwab reports;
    when both are 0 we treat the fill as all-or-nothing on the raw status alone.
    """
    raw = (raw or "").upper()
    filled = _num(filled_qty)
    ordered = _num(ordered_qty)
    fully_filled = filled > 0 and ordered > 0 and filled >= ordered
    partial = filled > 0 and (ordered <= 0 or filled < ordered)

    if raw == "FILLED":
        return FILLED_DURING_CANCEL if cancel_requested else FILLED
    if raw in ("CANCELED", "CANCELLED"):
        if fully_filled:
            # Reported canceled but the whole order actually filled — it's a fill.
            return FILLED_DURING_CANCEL if cancel_requested else FILLED
        if partial:
            return PARTIAL_FILL_CANCELED
        return CANCELED
    if raw == "REJECTED":
        return REJECTED
    if raw == "EXPIRED":
        # An expired DAY order that partially filled leaves an unbalanced position.
        return PARTIAL_FILL_CANCELED if partial else EXPIRED
    if raw == "PENDING_CANCEL":
        return PENDING_CANCEL
    # Still live at the broker (WORKING / QUEUED / ACCEPTED / PENDING_ACTIVATION /
    # PARTIALLY_FILLED / anything unrecognized-but-not-terminal).
    return PENDING_CANCEL if cancel_requested else WORKING


def check_resubmit(lock: dict | None, max_attempts: int) -> tuple[bool, str]:
    """Encode the NO_RESUBMIT_BEFORE_TERMINAL invariant + MAX_RESUBMIT_ATTEMPTS.

    Returns (allowed, reason). ``lock`` is the persisted per-position-intent record
    (or None for a first-ever order). This is the ONE place the resubmission gate
    is decided; executor.py calls it before any live placement and never open-codes
    the rule. It is IN ADDITION to (never a replacement for) the Level-5 account
    gate, the kill switch, and the reconciliation freeze.
    """
    if not lock:
        return True, ""
    state = lock.get("state")
    if state == LOCKED_UNKNOWN:
        return False, ("prior order state is UNKNOWN at the broker (it may still be "
                       "working) — resolve it manually before trading this position")
    if state in REVIEW_BLOCKING:
        return False, (f"prior order ended {state} — the position needs review; "
                       "the app will not auto-resubmit")
    if state is not None and not is_terminal(state):
        return False, f"prior order for this position is still {state} at the broker"
    if not lock.get("reconciled", True):
        return False, "prior order's fill is not yet reconciled into state"
    attempts = int(lock.get("attempts") or 0)
    if attempts >= int(max_attempts):
        return False, f"max resubmit attempts ({max_attempts}) reached this session"
    return True, ""


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
