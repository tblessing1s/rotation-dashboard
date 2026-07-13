"""Market-settle execution gate — the pure time-of-day order-discipline layer.

The first ~30 minutes after the open and the last ~15 before the close are
structurally hostile to CFM's order types (widest spreads on deep-ITM LEAPs,
unreliable market-maker IV marks, gap-distorted daily-bar signals, closing-auction
imbalances). ALERTS still fire immediately — only *order execution* is gated or
deferred by action type, with one narrow gap-emergency exception for DEFENSE /
EXIT_KILL.

Everything in this module is **pure and deterministic** given its inputs (an
injected ``now``, a :class:`session.SessionState`, and an optional
:class:`GapContext`). No I/O, no wall-clock read — so the whole gate is unit-
testable with a mocked clock. The impure wiring (reading the clock, building the
gap context from quotes, refusing the transmit) lives in ``executor`` and calls
:func:`execution_window` from the one shared placement path.

Design references (see AUDIT_MARKET_SETTLE_GATE.md):
  * The gate's ``action_type`` vocabulary is NOT ``rec_types.ActionType`` — it is
    derived from the executor's action string + roll/exit reason via
    :func:`classify_action` (DEFENSE is a ``roll_short`` with ``roll_reason ==
    "defend"``; CANCEL never reaches ``execute()``).
  * The gap-emergency's intraday inputs (opening-range low, two-sided-print
    duration) are not produced by the data layer yet; the pure rule models them
    faithfully, and the first-cut wiring is fail-closed / gap-size-only (see
    IMPLEMENTATION_NOTES.md).
  * No market order is ever built today; ``NO_MARKET_ORDERS_AT_OPEN`` is a forward
    invariant enforced here (the emergency path additionally requires a limit).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import config
from session import SessionState


# ---- Vocabularies ----------------------------------------------------------

class GateAction:
    """What is being executed, from the gate's point of view. Derived from the
    executor payload by :func:`classify_action` — NOT stored anywhere."""
    ENTRY = "ENTRY"
    ROLL_SHORT = "ROLL_SHORT"
    ROLL_LEAP = "ROLL_LEAP"
    DEFENSE = "DEFENSE"
    EXIT_KILL = "EXIT_KILL"
    CANCEL = "CANCEL"


GATE_ACTIONS = frozenset({
    GateAction.ENTRY, GateAction.ROLL_SHORT, GateAction.ROLL_LEAP,
    GateAction.DEFENSE, GateAction.EXIT_KILL, GateAction.CANCEL,
})

# The two families that may take the pre-settle gap-emergency path. Everything
# else is blocked through the settle window with no exception (HARD_CFM_RULE:
# emergency never for ENTRY or routine rolls).
_EMERGENCY_ELIGIBLE = frozenset({GateAction.DEFENSE, GateAction.EXIT_KILL})


class WindowReason:
    """Machine-readable reason on a :class:`WindowVerdict`."""
    OPEN = "OPEN"                                  # allowed pass-through
    MARKET_CLOSED = "MARKET_CLOSED"
    SETTLE_WINDOW = "SETTLE_WINDOW"
    ENTRY_WINDOW = "ENTRY_WINDOW"
    CLOSE_BLACKOUT = "CLOSE_BLACKOUT"
    GAP_EMERGENCY_UNLOCK = "GAP_EMERGENCY_UNLOCK"  # allowed via the emergency path


@dataclass(frozen=True)
class WindowVerdict:
    """The gate's decision for one (action, instant). ``executable_at`` is the next
    timestamp the action becomes allowed (``None`` when already allowed)."""
    allowed: bool
    executable_at: datetime | None
    reason: str
    emergency_path: bool = False


@dataclass(frozen=True)
class GapContext:
    """Optional gap/opening-range inputs for the emergency-unlock evaluation. All
    fields are supplied by the caller (the gate reads no market data). A ``None``
    for an intraday input means *unavailable*, which is treated as NOT satisfied
    (fail-closed) — a filling gap must never unlock."""
    # Magnitude of the overnight gap AGAINST the position, in ATR units (>= 0).
    # Caller signs it: for the long-biased LEAP the adverse direction is DOWN.
    adverse_gap_atr: float | None = None
    # Price broke below the opening-range low after gapping down (continuation
    # confirmation — the alternative to a large gap magnitude in leg 1).
    broke_opening_range_low: bool = False
    # Minutes the underlying has printed two-sided quotes (None = unknown).
    two_sided_print_minutes: float | None = None
    # The order is a limit order (marketable limits are fine; the point is a price
    # cap, not passivity). Market orders never take the emergency path.
    is_limit_order: bool = True


# ---- Action classification (executor payload -> GateAction) -----------------

def classify_action(action: str | None, payload: dict | None = None) -> str | None:
    """Map an ``executor.execute`` action string (+ its payload) to a
    :class:`GateAction`, or ``None`` for an ungated path (``adjustment``).

    DEFENSE has no order path of its own — it is a ``roll_short`` whose
    ``roll_reason`` is ``"defend"``. When the reason is absent or unrecognized the
    roll is treated as the *stricter* ROLL_SHORT (never emergency-eligible), so a
    routine roll can never borrow the emergency unlock (HARD_CFM_RULE)."""
    a = (action or "").strip()
    payload = payload or {}
    if a in ("open_position_atomic", "buy_leap"):
        return GateAction.ENTRY
    if a == "roll_short":
        reason = (payload.get("roll_reason") or "").strip().lower()
        return GateAction.DEFENSE if reason == "defend" else GateAction.ROLL_SHORT
    if a == "sell_short":
        # Re-establishing the income leg — blocked in settle, never emergency.
        return GateAction.ROLL_SHORT
    if a == "roll_leap":
        return GateAction.ROLL_LEAP
    if a in ("close_position_atomic", "close_leap", "close_short"):
        return GateAction.EXIT_KILL
    if a in ("cancel", "cancel_order"):
        return GateAction.CANCEL
    if a == "adjustment":
        return None
    return None


# ---- Timing helpers --------------------------------------------------------

def _earliest_minutes(action: str) -> int:
    """Minutes after the open at which ``action`` first becomes executable. Entries
    carry the longer entry-window minimum (they are never urgent by construction);
    everything else clears at the end of the settle window."""
    if action == GateAction.ENTRY:
        # ENTRY_EARLIEST_MINUTES is the binding minimum (>= the settle window).
        return max(config.ENTRY_EARLIEST_MINUTES, config.MARKET_SETTLE_MINUTES)
    return config.MARKET_SETTLE_MINUTES


def _executable_after_open(anchor_open: datetime, action: str) -> datetime:
    return anchor_open + timedelta(minutes=_earliest_minutes(action))


# ---- The gate --------------------------------------------------------------

def execution_window(action_type: str,
                     now: datetime,
                     session: SessionState,
                     gap_context: GapContext | None = None) -> WindowVerdict:
    """The single time-of-day gate. Pure: deterministic given its inputs.

    Precedence (first match wins):
      0. CANCEL is never gated — allowed whenever the broker accepts cancels
         (any session state). HARD_CFM_RULE CANCEL_NEVER_GATED.
      1. Market closed -> blocked (executable next session), except CANCEL.
      2. Close blackout (last CLOSE_BLACKOUT_MINUTES before the ACTUAL close) ->
         all blocked; resulting orders become executable next session after settle.
      3. Settle window (open + MARKET_SETTLE_MINUTES):
         ENTRY/ROLL_SHORT/ROLL_LEAP blocked to their minimums; DEFENSE/EXIT_KILL
         blocked unless the gap-emergency unlock applies.
      4. Entry window: ENTRY additionally blocked until open + ENTRY_EARLIEST_MINUTES.
      5. Otherwise allowed.
    """
    action = action_type
    gap = gap_context or GapContext()

    # 0. Cancels are never gated.
    if action == GateAction.CANCEL:
        return WindowVerdict(allowed=True, executable_at=None, reason=WindowReason.OPEN)

    # 1. Market closed.
    if not session.is_open:
        return WindowVerdict(
            allowed=False,
            executable_at=_executable_after_open(session.next_open_at, action),
            reason=WindowReason.MARKET_CLOSED,
        )

    # 2. Close blackout — keyed off the ACTUAL close (early-close aware via the
    #    session model). Deferred orders re-validate against the confirmed close
    #    next session and execute after that session's settle window.
    if (session.minutes_until_close is not None
            and session.minutes_until_close <= config.CLOSE_BLACKOUT_MINUTES):
        return WindowVerdict(
            allowed=False,
            executable_at=_executable_after_open(session.next_open_at, action),
            reason=WindowReason.CLOSE_BLACKOUT,
        )

    mins_since = session.minutes_since_open or 0.0
    in_settle = mins_since < config.MARKET_SETTLE_MINUTES

    # 3. Settle window.
    if in_settle:
        if action in _EMERGENCY_ELIGIBLE:
            if _gap_emergency_unlocked(gap):
                return WindowVerdict(
                    allowed=True, executable_at=None,
                    reason=WindowReason.GAP_EMERGENCY_UNLOCK, emergency_path=True)
            return WindowVerdict(
                allowed=False,
                executable_at=_executable_after_open(session.open_at, action),
                reason=WindowReason.SETTLE_WINDOW)
        # ENTRY / ROLL_SHORT / ROLL_LEAP — no exception inside the settle window.
        return WindowVerdict(
            allowed=False,
            executable_at=_executable_after_open(session.open_at, action),
            reason=WindowReason.SETTLE_WINDOW)

    # 4. Entry window — entries stay blocked past the settle window until their
    #    longer minimum (entries require multi-day persistence; never urgent).
    if action == GateAction.ENTRY and mins_since < config.ENTRY_EARLIEST_MINUTES:
        return WindowVerdict(
            allowed=False,
            executable_at=_executable_after_open(session.open_at, action),
            reason=WindowReason.ENTRY_WINDOW)

    # 5. Allowed.
    return WindowVerdict(allowed=True, executable_at=None, reason=WindowReason.OPEN)


def _gap_emergency_unlocked(gap: GapContext) -> bool:
    """The gap-emergency unlock (DEFENSE / EXIT_KILL only, inside the settle
    window). Requires ALL of, per Design §3:

      * an overnight gap against the position >= GAP_EMERGENCY_ATR_MULT * ATR,
        OR a break below the opening-range low after gapping down (continuation);
      * two-sided prints for >= EMERGENCY_MIN_PRINT_MINUTES;
      * the order is a limit order (HARD_CFM_RULE NO_MARKET_ORDERS_AT_OPEN — the
        emergency path is never a market order).

    Every intraday input that is unavailable (``None``) is treated as NOT
    satisfied (fail-closed): a filling gap must never unlock a maximum-slippage
    whipsaw."""
    # Leg 3 — limit order (market orders never take the emergency path).
    if not gap.is_limit_order:
        return False
    # Leg 2 — two-sided prints for long enough (unknown -> not satisfied).
    if gap.two_sided_print_minutes is None:
        return False
    if gap.two_sided_print_minutes < config.EMERGENCY_MIN_PRINT_MINUTES:
        return False
    # Leg 1 — a large adverse gap OR a confirmed break of the opening-range low.
    big_gap = (gap.adverse_gap_atr is not None
               and gap.adverse_gap_atr >= config.GAP_EMERGENCY_ATR_MULT)
    return bool(big_gap or gap.broke_opening_range_low)


# ---- Market-order forward invariant (Design §4 / §6) ------------------------

def market_order_blocked_now(session: SessionState) -> bool:
    """True inside the settle window, where NO_MARKET_ORDERS_AT_OPEN refuses a
    market order for EVERY action type (emergency included — emergencies execute
    as limit orders). No market order is constructed anywhere today, so this is a
    forward invariant asserted at the wiring boundary."""
    if not config.NO_MARKET_ORDERS_AT_OPEN:
        return False
    if not session.is_open:
        return False
    return (session.minutes_since_open or 0.0) < config.MARKET_SETTLE_MINUTES


# ---- Spread-quality gate (Design §5) — independent second gate --------------

class SpreadWarning:
    NONE = None
    NO_BASELINE = "NO_BASELINE"
    WIDE_SPREAD = "WIDE_SPREAD"


@dataclass(frozen=True)
class SpreadVerdict:
    """Result of the independent spread-quality check. Never blocks execution
    (post-settle) — it informs. Inside the emergency path the warning is shown but
    ``requires_ack`` is False (a kill-switch exit must not be stopped by a wide
    spread, only informed)."""
    has_baseline: bool
    wide: bool
    warning: str | None
    current_spread: float | None
    baseline_spread: float | None
    est_excess_slippage_usd: float | None
    requires_ack: bool


def spread_quality(current_spread: float | None,
                   baseline_spread: float | None,
                   contracts: int = 1,
                   *,
                   emergency_path: bool = False) -> SpreadVerdict:
    """Compare the current bid-ask spread to the trailing baseline. Returns a
    WIDE_SPREAD warning (with the estimated excess slippage in dollars) when the
    current spread exceeds ``SPREAD_QUALITY_MULT`` * baseline; a NO_BASELINE state
    when there is no trailing average yet (never a fabricated one). Execution is
    never blocked here; a wide spread post-settle requires an explicit acknowledge,
    but inside the emergency path it is shown and never blocks (no ack required)."""
    if baseline_spread is None or baseline_spread <= 0 or current_spread is None:
        return SpreadVerdict(
            has_baseline=False, wide=False, warning=SpreadWarning.NO_BASELINE,
            current_spread=current_spread, baseline_spread=baseline_spread,
            est_excess_slippage_usd=None, requires_ack=False)

    wide = current_spread > config.SPREAD_QUALITY_MULT * baseline_spread
    excess_per_share = max(0.0, current_spread - baseline_spread)
    # Dollar excess for the ticket: excess spread * contracts * 100 (one crossing;
    # mirrors burn.exit_slippage_est's spread * contracts * 100 convention).
    est_excess = excess_per_share * max(1, int(contracts)) * 100.0 if wide else 0.0
    return SpreadVerdict(
        has_baseline=True,
        wide=wide,
        warning=SpreadWarning.WIDE_SPREAD if wide else SpreadWarning.NONE,
        current_spread=current_spread,
        baseline_spread=baseline_spread,
        est_excess_slippage_usd=(est_excess if wide else None),
        # A wide spread requires an explicit acknowledge post-settle, but never
        # inside the emergency path (informed, not blocked).
        requires_ack=bool(wide and not emergency_path),
    )
