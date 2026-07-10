"""Coded vocabularies for the recommendation trust layer — the machine-readable
enums stamped on every Recommendation, resolution, override, and order-fidelity
record. Mirrors the exit_reasons.py pattern: plain string constants in a
namespace class, frozensets for validation, zero imports beyond the stdlib so
every other module (pure engine, derivations, executor, alerts) can depend on
this one without cycles.

Nothing here decides anything. The engine (recommendation_engine.py) emits the
records; the derivations (trust_derive.py) resolve and grade them; this module
only names the states so no free-text ever leaks into the trust evidence.
"""
from __future__ import annotations


class ActionType:
    """What the recommendation proposes the operator DO."""

    ENTER = "ENTER"
    ROLL_OUT = "ROLL_OUT"
    ROLL_DOWN = "ROLL_DOWN"   # reserved: not emitted in this iteration (see note)
    DEFEND = "DEFEND"
    EXIT = "EXIT"
    NO_ACTION = "NO_ACTION"


# NOTE on ROLL_DOWN vs DEFEND: executions carry a single roll_reason "defend" for
# every defensive roll-down, so the two would be indistinguishable at matching
# time. This iteration emits DEFEND for the defensive roll-down and never emits
# ROLL_DOWN; the constant exists so the graduation config (which names both) has
# a stable key if a future change splits them.
ACTION_TYPES = frozenset({
    ActionType.ENTER, ActionType.ROLL_OUT, ActionType.ROLL_DOWN,
    ActionType.DEFEND, ActionType.EXIT, ActionType.NO_ACTION,
})

# Action types that carry a proposed ticket and expect an operator action.
ACTIONABLE = frozenset(ACTION_TYPES - {ActionType.NO_ACTION})


class TriggerRule:
    """WHY the recommendation fired — same style as exit_reasons.ExitReason."""

    KILL_RS_SECTOR = "KILL_RS_SECTOR"                  # RS3M vs Sector negative -> EXIT now
    KILL_RS_SPY_CONFIRMED = "KILL_RS_SPY_CONFIRMED"    # RS3M vs SPY negative on close -> EXIT 1-2d
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"                # line-in-the-sand condition tripped -> EXIT
    WHIPSAW_GUARD = "WHIPSAW_GUARD"                    # defend whipsaw -> EXIT, not another defend
    DELTA_COVERAGE_FLOOR = "DELTA_COVERAGE_FLOOR"      # LEAP no longer covers -> EXIT
    DEFEND_BELOW_STRIKE = "DEFEND_BELOW_STRIKE"        # closed below short strike -> DEFEND roll-down
    ROLL_75PCT = "ROLL_75PCT"                          # >=75% decayed, >2 DTE -> ROLL_OUT early
    ROLL_SCHEDULED_WEEKLY = "ROLL_SCHEDULED_WEEKLY"    # expiry imminent -> ROLL_OUT (weekly cadence)
    JUICE_HURDLE_FAIL = "JUICE_HURDLE_FAIL"            # trailing juice under target/burn -> EXIT (redeploy)
    DTE_PLANNED_EXIT = "DTE_PLANNED_EXIT"              # LEAP at/below planned-exit DTE -> EXIT/roll long
    EARNINGS_WINDOW = "EARNINGS_WINDOW"                # earnings inside window -> ROLL_OUT deep-ITM
    DIVIDEND_ASSIGNMENT_RISK = "DIVIDEND_ASSIGNMENT_RISK"  # extrinsic collapse (div escalation) -> ROLL_OUT
    GATE_ALL_PASS = "GATE_ALL_PASS"                    # every entry gate clear -> ENTER
    ALL_CLEAR = "ALL_CLEAR"                            # explicit no-action claim for the pass


TRIGGER_RULES = frozenset({
    TriggerRule.KILL_RS_SECTOR, TriggerRule.KILL_RS_SPY_CONFIRMED,
    TriggerRule.CIRCUIT_BREAKER, TriggerRule.WHIPSAW_GUARD,
    TriggerRule.DELTA_COVERAGE_FLOOR, TriggerRule.DEFEND_BELOW_STRIKE,
    TriggerRule.ROLL_75PCT, TriggerRule.ROLL_SCHEDULED_WEEKLY,
    TriggerRule.JUICE_HURDLE_FAIL, TriggerRule.DTE_PLANNED_EXIT,
    TriggerRule.EARNINGS_WINDOW, TriggerRule.DIVIDEND_ASSIGNMENT_RISK,
    TriggerRule.GATE_ALL_PASS, TriggerRule.ALL_CLEAR,
})


class OverrideReason:
    """Coded reason the operator taps when dismissing a recommendation.
    OTHER requires a typed note (mirrors exit_reasons.NOTE_REQUIRED)."""

    DISAGREE_TIMING = "DISAGREE_TIMING"
    DISAGREE_STRIKE = "DISAGREE_STRIKE"
    DISAGREE_ACTION = "DISAGREE_ACTION"
    EXTERNAL_INFO = "EXTERNAL_INFO"
    DISCIPLINE_LAPSE = "DISCIPLINE_LAPSE"
    OTHER = "OTHER"


OVERRIDE_REASONS = frozenset({
    OverrideReason.DISAGREE_TIMING, OverrideReason.DISAGREE_STRIKE,
    OverrideReason.DISAGREE_ACTION, OverrideReason.EXTERNAL_INFO,
    OverrideReason.DISCIPLINE_LAPSE, OverrideReason.OTHER,
})

OVERRIDE_NOTE_REQUIRED = frozenset({OverrideReason.OTHER})


class Resolution:
    """How a recommendation (or an unmatched execution) resolved. Derived only —
    never written by hand. SUPERSEDED is internal bookkeeping so a replaced
    recommendation neither matches nor counts as an expiry."""

    EXECUTED_MATCHED = "EXECUTED_MATCHED"
    OVERRIDDEN = "OVERRIDDEN"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    COVERAGE_MISS = "COVERAGE_MISS"   # execution with no matching open recommendation


RESOLUTIONS = frozenset({
    Resolution.EXECUTED_MATCHED, Resolution.OVERRIDDEN, Resolution.EXPIRED,
    Resolution.SUPERSEDED, Resolution.COVERAGE_MISS,
})


class FidelityCheck:
    """The per-ticket lifecycle grades in the order_fidelity ledger."""

    LIFECYCLE_LEGAL = "LIFECYCLE_LEGAL"
    SLIPPAGE_IN_BOUND = "SLIPPAGE_IN_BOUND"
    NO_ORPHAN_LEG = "NO_ORPHAN_LEG"
    CANCEL_CONFIRMED_DEAD = "CANCEL_CONFIRMED_DEAD"
    RECONCILED_CLEAN = "RECONCILED_CLEAN"


FIDELITY_CHECKS = (
    FidelityCheck.LIFECYCLE_LEGAL, FidelityCheck.SLIPPAGE_IN_BOUND,
    FidelityCheck.NO_ORPHAN_LEG, FidelityCheck.CANCEL_CONFIRMED_DEAD,
    FidelityCheck.RECONCILED_CLEAN,
)


class CheckStatus:
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"                      # order not yet terminal
    NOT_YET_IMPLEMENTED = "NOT_YET_IMPLEMENTED"  # never a silent pass


class FidelityDefect:
    """Coded defect stamped on a failed fidelity check."""

    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"        # observed transition not in the legal graph
    EVENT_CHAIN_GAP = "EVENT_CHAIN_GAP"              # event's prior_state != previous event's new_state
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"          # adverse fill beyond the ticket's bound
    ORPHAN_LEG = "ORPHAN_LEG"                        # one leg of a multi-leg ticket live without the other
    PARTIAL_FILL = "PARTIAL_FILL"                    # partial quantity terminal (unbalanced)
    CANCEL_NOT_CONFIRMED_DEAD = "CANCEL_NOT_CONFIRMED_DEAD"  # cancel requested, never confirmed terminal
    HARD_LOCKED = "HARD_LOCKED"                      # lifecycle ended in LOCKED_UNKNOWN


def is_action_type(v: str | None) -> bool:
    return v in ACTION_TYPES


def is_trigger(v: str | None) -> bool:
    return v in TRIGGER_RULES


def is_override_reason(v: str | None) -> bool:
    return v in OVERRIDE_REASONS


def override_requires_note(v: str | None) -> bool:
    return v in OVERRIDE_NOTE_REQUIRED
