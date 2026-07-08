"""Coded exit reasons — the machine-readable enum that lands on every closed
cycle so the calibration harness can bucket outcomes by why they ended.

Derived from the ACTUAL exit paths (see docs/entry-context-audit.md §3). No exit
is automated: kill_switch.py and circuit_breaker.py are advisory evaluators, and
every close is operator-driven through executor.execute. So the code is set at
the point the trigger fires — the advisory evaluators expose
``exit_reason_code`` mappers, the operator/UI carries that code onto the close,
and it is stored verbatim on the immutable close_leap execution (never inferred
after the fact).

``OPERATOR_DISCRETION`` REQUIRES a typed note, mirroring the account gate's
typed-override pattern (executor.py override_reason). ``LEGACY_UNRECORDED`` is
set ONLY by the migration/derivation for cycles closed before this feature
shipped — it is never a valid close-time reason.
"""
from __future__ import annotations


class ExitReason:
    """Namespace of the coded exit-reason string constants."""

    # Kill switch (kill_switch.py) — relative-strength exits.
    KILL_SWITCH_SECTOR = "KILL_SWITCH_SECTOR"   # RS3M vs Sector negative -> exit now
    KILL_SWITCH_SPY = "KILL_SWITCH_SPY"         # RS3M vs SPY negative -> exit 1-2 days

    # Position circuit breaker (circuit_breaker.py) — one member per condition.
    CB_DRAWDOWN_15 = "CB_DRAWDOWN_15"           # >= 15% drop from entry
    CB_MA50_3CLOSE = "CB_MA50_3CLOSE"           # 3 closes below the 50-day MA
    CB_MA200_CLOSE = "CB_MA200_CLOSE"           # 1 close below the 200-day MA
    CB_MANUAL_LINE = "CB_MANUAL_LINE"           # operator line-in-the-sand breached

    WHIPSAW_BREAKER = "WHIPSAW_BREAKER"         # defend-whipsaw guard -> exit, not defend
    DELTA_COVERAGE = "DELTA_COVERAGE"           # LEAP no longer covers the short
    EARNINGS_WINDOW = "EARNINGS_WINDOW"         # roll deep-ITM or exit before the report
    RECONCILIATION = "RECONCILIATION"           # broker divergence -> exit to resolve
    TARGET_REACHED = "TARGET_REACHED"           # cycle return target met
    OPERATOR_DISCRETION = "OPERATOR_DISCRETION"  # manual close; typed note REQUIRED

    # Mechanical LEAP roll: the long leg is rolled (close_leap + buy_leap sharing
    # a leap_roll_id), which the derivation treats as a cycle boundary. Not a
    # graded exit — set internally by the roll path, never by an operator.
    LEAP_ROLL = "LEAP_ROLL"

    # Migration / derivation only — never a valid close-time reason.
    LEGACY_UNRECORDED = "LEGACY_UNRECORDED"


# Every reason a close can legitimately set at trade time (excludes the
# migration-only LEGACY_UNRECORDED).
CLOSE_TIME = frozenset({
    ExitReason.KILL_SWITCH_SECTOR, ExitReason.KILL_SWITCH_SPY,
    ExitReason.CB_DRAWDOWN_15, ExitReason.CB_MA50_3CLOSE,
    ExitReason.CB_MA200_CLOSE, ExitReason.CB_MANUAL_LINE,
    ExitReason.WHIPSAW_BREAKER, ExitReason.DELTA_COVERAGE,
    ExitReason.EARNINGS_WINDOW, ExitReason.RECONCILIATION,
    ExitReason.TARGET_REACHED, ExitReason.OPERATOR_DISCRETION,
    ExitReason.LEAP_ROLL,
})

# Reasons an operator may set on a manual close (the menu the UI offers). The
# mechanical LEAP_ROLL is excluded — the roll path sets it, never a person.
OPERATOR_SELECTABLE = frozenset(CLOSE_TIME - {ExitReason.LEAP_ROLL})

# Every value the field may hold on a stored record (CLOSE_TIME + legacy).
ALL = frozenset(CLOSE_TIME | {ExitReason.LEGACY_UNRECORDED})

# The automated-trigger codes (everything a rule can raise on its own); the
# operator's discretionary close is the only human-authored member.
AUTOMATED = frozenset(CLOSE_TIME - {ExitReason.OPERATOR_DISCRETION})

# HARD_CFM_RULE — codes that REQUIRE a typed note (mirrors the typed-override
# pattern on the account gate). Kept as a set so config's EXIT_NOTE_REQUIRED_FOR
# provenance tag maps one-to-one.
NOTE_REQUIRED = frozenset({ExitReason.OPERATOR_DISCRETION})


def is_valid(code: str | None) -> bool:
    """True when ``code`` is a recognized coded reason (any stored value)."""
    return code in ALL


def is_close_time(code: str | None) -> bool:
    """True when ``code`` is a reason a live close may legitimately set."""
    return code in CLOSE_TIME


def requires_note(code: str | None) -> bool:
    """True when a stored ``exit_note`` is mandatory for this code."""
    return code in NOTE_REQUIRED


def normalize(code: str | None) -> str | None:
    """Upper-strip a raw reason to its canonical form, or None if unrecognized.
    Recognized coded reasons pass through; anything else (blank, legacy free
    text like ``"discretionary"``) returns None so the caller decides the
    fallback (reject a live close, or LEGACY_UNRECORDED at derivation)."""
    if not code:
        return None
    c = str(code).strip().upper().replace(" ", "_").replace("-", "_")
    return c if c in ALL else None
