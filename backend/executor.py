"""Execute CFM actions (buy_leap / sell_short / close_short) and auto-log them.

Every execution captures the stock price + premium at the moment of execution
and appends an immutable record to state.json, from which the theta ledger and
extrinsic-payback meters are derived. Live order transmission to Schwab is
gated behind the CFM_LIVE_TRADING env flag; with it off (the default) the action
is captured and logged against live market prices but no order is sent — the
honest paper path. Position state updates identically either way.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import config
import data_handler
import execution_gate
import indicators
import logging_handler as log
import order_lifecycle as olc
import schwab_api
import sector_data
import session
import spread_monitor

VALID_ACTIONS = {"buy_leap", "sell_short", "close_short", "close_leap", "roll_short",
                 "roll_leap", "open_position_atomic", "close_position_atomic", "adjustment"}

# Actions REJECTED on a frozen (needs_review) position — new risk cannot be added
# to a position whose state is unverified. Closing actions (close_short,
# close_leap, close_position_atomic) are deliberately NOT here: a freeze must
# never trap the operator in a position during a kill-switch event — exiting is
# safe in either state of the world. ``adjustment`` is the resolution path, also
# allowed. See docs/reconciliation.md.
FROZEN_BLOCKED_ACTIONS = {"buy_leap", "sell_short", "roll_short", "roll_leap",
                          "open_position_atomic"}


class PositionFrozenError(RuntimeError):
    """A new-risk action was attempted on a position frozen by reconciliation
    (needs_review). The API surfaces this as HTTP 409 (distinct from the 400
    gate-rejection) with the diff summary in the body. Closing actions bypass
    this — a freeze protects against acting on wrong state, but exiting is safe."""

    def __init__(self, ticker: str, review: dict | None):
        self.ticker = ticker
        self.review = review or {}
        summary = self.review.get("summary") or "state is unverified against the broker"
        super().__init__(
            f"{ticker} is frozen for review — {summary}. New entries/rolls are blocked "
            f"until the reconciliation diff is resolved; closing the position is still allowed.")


class ExecutionWindowError(RuntimeError):
    """The market-settle execution gate blocked or deferred an order (settle window,
    close blackout, or off-hours). The API surfaces this as HTTP 409 with the
    machine-readable ``reason`` and an ``executable_at`` so the UI can stage the
    recommendation as PENDING_SETTLE and show a countdown. CANCEL is never gated,
    and this never fires for a genuine gap-emergency DEFENSE/EXIT (which the gate
    unlocks). Enforcement is governed by config.market_settle_gate_enabled()."""

    def __init__(self, ticker: str, gate_action: str, verdict) -> None:
        self.ticker = ticker
        self.gate_action = gate_action
        self.reason = verdict.reason
        self.executable_at = verdict.executable_at
        self.emergency_path = verdict.emergency_path
        at = verdict.executable_at.isoformat() if verdict.executable_at else "the next session"
        super().__init__(
            f"{ticker} {gate_action} is deferred by the market-settle gate "
            f"({verdict.reason}); executable at {at}. Alerts still fired — the "
            f"recommendation is staged and can be pre-approved for auto-release.")


class SpreadAckRequiredError(RuntimeError):
    """The spread-quality check found the current spread abnormally wide (> the
    trailing baseline * SPREAD_QUALITY_MULT). Execution is not blocked by time here,
    but the operator must explicitly acknowledge the estimated excess slippage
    (payload ``spread_ack: true``) before the order transmits. Never raised on the
    emergency path (a kill-switch exit is informed, not stopped)."""

    def __init__(self, ticker: str, verdict) -> None:
        self.ticker = ticker
        self.current_spread = verdict.current_spread
        self.baseline_spread = verdict.baseline_spread
        self.est_excess_slippage_usd = verdict.est_excess_slippage_usd
        super().__init__(
            f"{ticker}: spread {verdict.current_spread:.2f} is wide vs the trailing "
            f"baseline {verdict.baseline_spread:.2f} (~${verdict.est_excess_slippage_usd:.0f} "
            f"est. excess slippage). Acknowledge to proceed.")


# Why a roll happened — the whipsaw ledger key. Unrecognized values fall back to
# "scheduled" so the ledger enum stays clean for later calibration.
ROLL_REASONS = {"scheduled", "75%-rule", "defend", "earnings", "kill-switch-exit"}

# Why a cycle ended — a CODED reason (exit_reasons.ExitReason) logged on the
# close_leap execution and carried onto the derived cycle record. Validated at
# the execute() boundary (see _validate_exit_reason); OPERATOR_DISCRETION
# requires a typed exit_note. The coded enum replaced the old free-text set so
# calibration can bucket outcomes by why they ended.

# Schwab order instruction per single-leg CFM action (all legs are calls).
INSTRUCTION = {
    "buy_leap": "BUY_TO_OPEN",
    "sell_short": "SELL_TO_OPEN",
    "close_short": "BUY_TO_CLOSE",
    "close_leap": "SELL_TO_CLOSE",
}


def live_enabled() -> bool:
    """Whether live trading is switched on — via the CFM_LIVE_TRADING env override
    or the persisted UI toggle (config.live_trading_enabled). This alone does NOT
    mean an order will transmit; see live_transmit() for the demo-safe gate."""
    return config.live_trading_enabled()


def live_transmit() -> bool:
    """Whether an executed order may actually be transmitted to the broker.

    Two independent switches must BOTH allow it: CFM_LIVE_TRADING must be on, AND
    the session must NOT be in demo/paper mode. Demo mode swaps in a synthetic
    price feed and a separate paper book (config.active_state_path), so placing a
    real order from a demo session would trade the LIVE account against fake
    prices — a hard safety no. This is the single choke point the entire
    live-order path is gated on; ``mode`` is derived from it, so a demo session
    always records the honest logged/paper path and never reaches the broker."""
    return live_enabled() and not config.demo_enabled()


def _assert_transmit_allowed(action: str) -> None:
    """Defense-in-depth broker-boundary guard: refuse to place a real order from a
    demo/paper session even if a caller reached here without the mode check.
    execute() already downgrades a demo session to the logged path, so this is a
    backstop that guarantees the invariant can't be bypassed by a future path."""
    if config.demo_enabled():
        raise schwab_api.SchwabError(
            f"Refusing to place a live {action} order in demo/paper mode — "
            "switch to Live data to trade the real account.")


# ---------------------------------------------------------------------------
# Market-settle execution gate (time-of-day order discipline)
# ---------------------------------------------------------------------------
def _gate_now(now: datetime | None) -> datetime:
    """Normalize the injected clock to a UTC-aware datetime (defaulting to the wall
    clock only when nothing is injected — every gated test injects one)."""
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _build_gap_context(ticker: str, payload: dict,
                       sess: "session.SessionState") -> execution_gate.GapContext:
    """Assemble the gap-emergency inputs for a DEFENSE/EXIT_KILL inside the settle
    window, from data the layer already has (daily bars + a live quote — no new
    polling). FAIL-CLOSED per the audit: the overnight gap-vs-ATR is the one
    computable leg; the opening-range-low break is genuinely unavailable (passed
    False), and two-sided-print duration is proxied by elapsed session time pending
    tick-level tracking. All CFM orders are LIMIT, so is_limit_order is True."""
    adverse_gap_atr = None
    try:
        df = data_handler.get_daily(ticker)
        prior_close = indicators.last(df)
        atr_val = indicators.atr(df) if df is not None else None
        current = _capture_price(ticker, payload.get("stock_price"))[0]
        if (prior_close is not None and atr_val and atr_val > 0
                and current is not None):
            # Adverse (against the long LEAP) direction is DOWN: gap magnitude in
            # ATR units, floored at 0 (a favorable up-gap is not an emergency).
            adverse_gap_atr = max(0.0, float(prior_close) - float(current)) / float(atr_val)
    except Exception:  # noqa: BLE001 — a data hiccup must never fabricate an unlock
        adverse_gap_atr = None
    return execution_gate.GapContext(
        adverse_gap_atr=adverse_gap_atr,
        broke_opening_range_low=False,               # unavailable -> not satisfied
        two_sided_print_minutes=sess.minutes_since_open,  # elapsed-session proxy
        is_limit_order=True,                          # CFM only ever sends LIMIT
    )


def _evaluate_execution_window(action: str, ticker: str, payload: dict,
                               now: datetime) -> "execution_gate.WindowVerdict | None":
    """Compute the gate verdict for an ``execute`` action (always computed, even
    when enforcement is off, so the result can be surfaced for staging/countdown).
    Returns ``None`` for ungated paths (adjustment / CANCEL)."""
    gate_action = execution_gate.classify_action(action, payload)
    if gate_action is None or gate_action == execution_gate.GateAction.CANCEL:
        return None
    sess = session.session_state(now)
    gap = None
    if (gate_action in (execution_gate.GateAction.DEFENSE, execution_gate.GateAction.EXIT_KILL)
            and sess.is_open
            and (sess.minutes_since_open or 0.0) < config.MARKET_SETTLE_MINUTES):
        gap = _build_gap_context(ticker, payload, sess)
    return execution_gate.execution_window(gate_action, now, sess, gap)


def _enforce_execution_window(action: str, ticker: str, payload: dict,
                              now: datetime) -> "execution_gate.WindowVerdict | None":
    """The gate checkpoint in the shared execution path. Refuses a blocked order
    (ExecutionWindowError) when enforcement is enabled; on the emergency path it
    stamps ``emergency_path`` onto the payload so the immutable execution record is
    tagged for post-hoc review. Cancels and adjustments are never gated."""
    verdict = _evaluate_execution_window(action, ticker, payload, now)
    if verdict is None:
        return None
    if verdict.emergency_path:
        payload["emergency_path"] = True
        payload["gate_reason"] = verdict.reason
    if config.market_settle_gate_enabled() and not verdict.allowed:
        raise ExecutionWindowError(ticker, execution_gate.classify_action(action, payload), verdict)
    return verdict


def _enforce_spread_quality(ticker: str, payload: dict, verdict) -> None:
    """The independent spread-quality gate (Design §5). When the current spread is
    abnormally wide vs the trailing baseline, require an explicit acknowledge
    (payload ``spread_ack``) — except on the emergency path, where the warning is
    surfaced but never blocks. Never blocks when enforcement is off or there is no
    baseline. The spread inputs are read from the payload (``current_spread`` /
    ``bid``+``ask``) so the check stays offline-testable; the traded contract's
    spread is also recorded to build the baseline (no new polling)."""
    if not config.market_settle_gate_enabled():
        return
    emergency = bool(verdict and verdict.emergency_path)
    symbol = (payload.get("option_symbol") or payload.get("short_option_symbol")
              or ticker or "").strip().upper()
    current = payload.get("current_spread")
    if current is None:
        current = spread_monitor.spread_of(payload.get("bid"), payload.get("ask"))
    state = log.load_state()
    base = spread_monitor.baseline(state, symbol)
    # Record this observation for the trailing baseline (from the already-fetched
    # quote), then persist.
    if spread_monitor.record(state, symbol, payload.get("bid"), payload.get("ask")) is not None:
        log.save_state(state)
    if current is None or base is None:
        return  # no baseline yet, or nothing to compare -> never fabricate/block
    contracts = int(payload.get("contracts") or 0) or 1
    sq = execution_gate.spread_quality(current, base, contracts, emergency_path=emergency)
    payload["spread_warning"] = sq.warning
    payload["spread_excess_usd"] = sq.est_excess_slippage_usd
    if sq.requires_ack and not payload.get("spread_ack"):
        raise SpreadAckRequiredError(ticker, sq)


class ResubmitLockedError(RuntimeError):
    """A new LIVE order for a position intent was blocked by the resubmission gate
    (order_lifecycle: NO_RESUBMIT_BEFORE_TERMINAL / MAX_RESUBMIT_ATTEMPTS). The API
    surfaces this as HTTP 409. This gate is IN ADDITION to — never a replacement
    for — the Level-5 account gate, the kill switch, and the reconciliation freeze."""

    def __init__(self, intent_key: str, reason: str):
        self.intent_key = intent_key
        self.reason = reason
        super().__init__(
            f"An order for {intent_key} can't be sent yet — {reason}. The prior order "
            "must be confirmed terminal at the broker (and its fill reconciled) first.")


# The per-position resubmission gate covers ENTRY intents (this task's scope). The
# roll/exit paths have their own freeze/leg-imbalance lifecycle and are untouched.
_LOCKED_INTENTS = {"buy_leap", "sell_short", "open_position_atomic"}


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _intent_key(ticker: str, action: str) -> str:
    return f"{(ticker or '').upper()}:{action}"


def _guard_resubmit(ticker: str, action: str) -> None:
    """Enforce the resubmission gate before a LIVE placement. Raises
    ResubmitLockedError when a prior order for this intent isn't cleanly terminal
    and reconciled at the broker, or the per-session attempt cap is hit.

    HARD_CFM_RULE NO_RESUBMIT_BEFORE_TERMINAL is asserted here, not merely
    consulted — the flag existing-and-True is the invariant; the decision itself is
    order_lifecycle.check_resubmit (the single source of the rule)."""
    if action not in _LOCKED_INTENTS or not config.NO_RESUBMIT_BEFORE_TERMINAL:
        return
    key = _intent_key(ticker, action)
    lock = log.get_order_lock(key)
    allowed, reason = olc.check_resubmit(lock, config.MAX_RESUBMIT_ATTEMPTS)
    if not allowed:
        # Attempt cap exhausted is an alerted, terminal stop (do not keep crossing).
        if (lock and int(lock.get("attempts") or 0) >= config.MAX_RESUBMIT_ATTEMPTS
                and olc.is_terminal(lock.get("state"))):
            _alert_order("ORDER_RESUBMIT_EXHAUSTED", ticker,
                         f"{ticker} {action}: {reason}. The app has stopped resubmitting "
                         "this order — reprice or reassess the entry manually.",
                         data={"intent": key, "attempts": lock.get("attempts")})
        log.logger.warning("resubmit gate blocked %s: %s", key, reason)
        raise ResubmitLockedError(key, reason)
    # Belt-and-suspenders: a crash could leave a pending broker order with no lock.
    # Never place a second order for an intent that still has a live pending order.
    for oid, rec in log.list_pending_orders().items():
        if (rec.get("ticker") or "").upper() == (ticker or "").upper() and rec.get("action") == action:
            raise ResubmitLockedError(key, f"order {oid} for this position is still pending at the broker")


def _record_placement(ticker: str, action: str, order_id: str, **extra) -> None:
    """After a confirmed live placement: append the SUBMITTED->WORKING event and,
    for a gated entry intent, (re)acquire the per-position lock — one placement is
    one resubmit attempt, counted for MAX_RESUBMIT_ATTEMPTS."""
    key = _intent_key(ticker, action)
    attempts = 0
    if action in _LOCKED_INTENTS:
        prior = log.get_order_lock(key) or {}
        attempts = int(prior.get("attempts") or 0) + 1
        log.save_order_lock(key, {
            "intent": key, "ticker": (ticker or "").upper(), "action": action,
            "order_id": str(order_id), "state": olc.WORKING,
            "reconciled": False, "attempts": attempts, "at": log.utcnow(),
        })
    log.append_order_event({
        "order_id": str(order_id), "ticker": (ticker or "").upper(), "action": action,
        "intent": key, "prior_state": olc.SUBMITTED, "new_state": olc.WORKING,
        "raw_status": "SUBMITTED", "attempt": attempts, **extra,
    })


def _settle_order(order_id: str, rec: dict, coded_state: str, raw: str, **extra) -> None:
    """Append a lifecycle transition to ``coded_state`` and update the intent lock.
    Terminal + clean states (CANCELED/REJECTED/EXPIRED/FILLED) mark the lock
    reconciled so a fresh order is allowed; review/unknown states leave it blocking."""
    ticker = rec.get("ticker") or ""
    action = rec.get("action") or rec.get("kind") or ""
    key = _intent_key(ticker, action)
    prior = None
    lock = log.get_order_lock(key)
    if lock is not None:
        prior = lock.get("state")
        updated = dict(lock)
        updated["state"] = coded_state
        updated["reconciled"] = coded_state in olc.RESUBMIT_OK_STATES
        updated["order_id"] = str(order_id)
        updated["at"] = log.utcnow()
        log.save_order_lock(key, updated)
    log.append_order_event({
        "order_id": str(order_id), "ticker": (ticker or "").upper(), "action": action,
        "intent": key, "prior_state": prior, "new_state": coded_state,
        "raw_status": raw, **extra,
    })


def _alert_order(type_: str, ticker: str, message: str, data: dict | None = None) -> None:
    """Fire a high-priority order-lifecycle alert through the existing engine.
    Best-effort: an alert failure must never unwind an already-committed fill."""
    try:
        import alerts
        alerts.record_event(type_, ticker, message, data=data, notify=True)
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        log.logger.error("order alert %s failed for %s: %s", type_, ticker, e)


def _order_filled_qty(order: dict) -> tuple[float, float]:
    """(filled, ordered) contract counts for an order. Prefers the order-level
    filledQuantity/quantity fields; falls back to summing execution-leg quantities
    as a coarse "some filled" signal. Schwab's exact partial-fill fields are a
    LIVE-VERIFY item, so the mapping is deliberately conservative."""
    ordered = _num(order.get("quantity"))
    filled = order.get("filledQuantity")
    if filled is not None:
        return _num(filled), ordered
    total = 0.0
    for act in order.get("orderActivityCollection", []) or []:
        for leg in act.get("executionLegs", []) or []:
            total += _num(leg.get("quantity"))
    return total, ordered


def _capture_price(ticker: str, supplied: float | None) -> tuple[float | None, str]:
    if supplied is not None:
        return float(supplied), "supplied"
    q = data_handler.latest_quote(ticker)
    if q:
        return q["price"], q["source"]
    return None, "unavailable"


def _ensure_position(state: dict, ticker: str) -> dict:
    p = log.find_position(state, ticker)
    if p:
        return p
    p = {
        "ticker": ticker.upper(),
        "sector": sector_data.sector_for(ticker) or "",
        "entry_date": log.utcnow()[:10],
        "status": "active",
        "leap": None,
        "leap_legs": [],
        "shares": {"count": 0, "cost_basis_per_share": None, "cap": config.SHARE_CAP, "pct_to_cap": 0},
        "short_calls": [],
        "kill_switch": {},
        "thesis": {"fundamentals": "", "intact": True},
        "delta_history": [],  # nightly {date, leap_delta} snapshots (delta velocity)
        "planned_exit_dte": config.PLANNED_EXIT_DTE,  # LEAP exit target; burn math keys off this
    }
    state["positions"].append(p)
    return p


def execute(payload: dict, now: datetime | None = None) -> dict:
    action = (payload.get("action") or "").strip()
    ticker = (payload.get("ticker") or "").strip().upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action '{action}' (expected one of {sorted(VALID_ACTIONS)})")
    if not ticker:
        raise ValueError("ticker is required")

    # Reconciliation freeze: reject new-risk actions on a position whose state is
    # unverified against the broker (checked BEFORE the account gate so a freeze
    # wins over a gate rejection). Closing actions + adjustments fall through.
    if action in FROZEN_BLOCKED_ACTIONS:
        _enforce_not_frozen(ticker)

    # Compensating adjustment (a reconciliation resolution) — its own path: an
    # immutable execution + a position holding correction, no gate/price capture.
    if action == "adjustment":
        return _adjustment(payload, ticker)

    contracts = int(payload.get("contracts") or 0)
    strike = payload.get("strike")
    stock_price, price_source = _capture_price(ticker, payload.get("stock_price"))

    # Level 5 gate (Account & Juice) — entry only. A blocking failure stops the
    # buy_leap unless the payload carries an explicit override_reason, which is
    # recorded on the immutable execution (see _buy_leap). Applies to the atomic
    # open too — it establishes the same LEAP long.
    if action in ("buy_leap", "open_position_atomic"):
        _enforce_account_gate(payload, ticker, contracts)

    state = log.load_state()
    position = _ensure_position(state, ticker)

    # Ordering invariant: never close the LONG leg while a short is still open —
    # that leaves a naked short call. A single-leg close_leap is REJECTED (no
    # override) when an open short remains; the operator must exit both legs
    # atomically (close_position_atomic) or close/roll the short first. Legit
    # single-leg closes (short already expired/closed, shares-only) still pass.
    if action == "close_leap" and (position.get("short_calls") or []):
        raise ValueError(
            "Refusing single-leg close_leap while an open short remains — it would "
            "leave a naked short call. Use close_position_atomic to exit both legs "
            "on one ticket, or close/roll the short first.")

    # Coded exit reason (+ typed note for OPERATOR_DISCRETION) — validated here,
    # at the operator-facing boundary, so a bad reason is rejected BEFORE any
    # order is placed. Normalizes payload["exit_reason"]/["exit_note"] in place.
    if action in ("close_leap", "close_position_atomic"):
        _validate_exit_reason(payload)

    # Market-settle execution gate (time-of-day order discipline) — the single
    # shared checkpoint every placement traverses (supervised approval today, the
    # future-automation switch tomorrow). Cancels never reach here; adjustments
    # returned above. Runs BEFORE any order is staged/placed. The independent
    # spread-quality gate runs second, informed by the window verdict (a genuine
    # gap-emergency exit is informed of a wide spread but never blocked by it).
    _gate_verdict = _enforce_execution_window(action, ticker, payload, _gate_now(now))
    _enforce_spread_quality(ticker, payload, _gate_verdict)

    log.save_state(state)  # persist the shell position before recording the fill

    mode = "live" if live_transmit() else "logged"

    if action == "open_position_atomic":
        return _open_position_atomic(payload, ticker, contracts, stock_price, mode, price_source)
    if action == "roll_short":
        return _roll_short(payload, ticker, contracts, stock_price, mode, price_source)
    if action == "roll_leap":
        return _roll_leap(payload, ticker, stock_price, mode, price_source)
    if action == "close_position_atomic":
        return _close_position_atomic(payload, ticker, stock_price, mode, price_source)

    # Live single-leg orders go to the broker and resolve asynchronously (place ->
    # poll -> fill/cancel); they're committed to state only once they actually
    # fill. Everything else (paper, or live without Schwab configured) commits
    # immediately as the honest logged path.
    if mode == "live" and schwab_api.configured():
        return _place_live(payload, ticker, action, contracts, strike, stock_price, price_source)
    return _commit(payload, ticker, action, contracts, strike, stock_price, price_source, mode)


def _enforce_not_frozen(ticker: str) -> None:
    """Raise PositionFrozenError if the ticker's position is frozen for review."""
    state = log.load_state()
    p = log.find_position(state, ticker)
    if p and p.get("needs_review"):
        raise PositionFrozenError(ticker, p.get("review"))


# ---------------------------------------------------------------------------
# Reconciliation resolution paths (compensating adjustment / expiry booking / ack)
# ---------------------------------------------------------------------------
def _strike_eq(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def _apply_adjustment(position: dict, itype: str, strike, qty_delta: int) -> None:
    """Apply a compensating quantity_delta (signed) to the identified leg. This
    is the operator committing truth forward — never auto-correction."""
    if itype == "EQUITY":
        shares = position.setdefault("shares", {"count": 0, "cap": config.SHARE_CAP})
        shares["count"] = int(shares.get("count") or 0) + qty_delta
        return
    if itype == "OPTION":
        # A short call is stored with a positive contract count but is SHORT, so
        # its signed quantity is -contracts; applying the delta toward zero closes
        # it. Match a short by strike first, else fall to the LEAP (long call).
        for sc in list(position.get("short_calls") or []):
            if strike is not None and _strike_eq(sc.get("strike"), strike):
                new_signed = -int(sc.get("contracts") or 0) + qty_delta
                if new_signed >= 0:
                    position["short_calls"] = [x for x in position["short_calls"] if x is not sc]
                else:
                    sc["contracts"] = -new_signed
                return
        legs = log.leap_legs(position)
        leap = next((l for l in legs if strike is None or _strike_eq(l.get("strike"), strike)), None)
        if leap is not None:
            new = int(leap.get("contracts") or 0) + qty_delta
            if new <= 0:
                legs.remove(leap)
                position["leap_legs"] = legs
                position["leap"] = legs[0] if legs else None
                shares = position.get("shares") or {}
                if not legs and not position.get("short_calls") and int(shares.get("count") or 0) == 0:
                    position["status"] = "closed"
            else:
                leap["contracts"] = new
            return
    # Unrecognized leg: the immutable adjustment record still stands; the operator
    # can follow with another adjustment. Nothing is silently invented.


def _adjustment(payload: dict, ticker: str) -> dict:
    """Record a compensating ``adjustment`` execution (append-only) and apply the
    holding correction. Required fields: instrument_type, quantity_delta, reason.
    An optional linked_diff_id ties it to the reconciliation diff it resolves (and
    marks that diff resolved, lifting the freeze once the position is clean)."""
    import reconcile

    itype = (payload.get("instrument_type") or "").upper()
    qty_delta = payload.get("quantity_delta")
    reason = (payload.get("reason") or "").strip()
    if qty_delta is None:
        raise ValueError("adjustment requires quantity_delta (signed)")
    if not reason:
        raise ValueError("adjustment requires a typed reason")
    if itype not in ("EQUITY", "OPTION"):
        raise ValueError("adjustment requires instrument_type EQUITY or OPTION")
    qty_delta = int(round(float(qty_delta)))
    strike = payload.get("strike")
    price = payload.get("price")
    linked = payload.get("linked_diff_id")
    mode = "live" if live_transmit() else "logged"
    execution = {
        "ticker": ticker, "action": "adjustment",
        "instrument": payload.get("instrument"), "instrument_type": itype,
        "strike": strike, "quantity_delta": qty_delta,
        "price": float(price) if price is not None else None,
        "reason": reason, "linked_diff_id": linked, "mode": mode,
    }
    stored = log.append_execution(execution)

    state = log.load_state()
    position = log.find_position(state, ticker)
    if position is not None:
        _apply_adjustment(position, itype, strike, qty_delta)
    if linked:
        try:
            reconcile.mark_diff_resolved(state, linked, "adjustment",
                                         {"execution_id": stored["id"]})
        except ValueError:
            pass  # diff already rolled off the latest report — the execution still stands
    log.recompute_derived(state)
    log.save_state(state)
    return {"success": True, "status": "adjusted", "execution_id": stored["id"],
            "timestamp": stored["date"], "mode": mode, "execution": stored}


def resolve_expiry(diff_id: str) -> dict:
    """One-click resolution for an EXPIRED_WORTHLESS_PENDING diff: book a
    close_short at $0.00 with reason ``expired_worthless``, timestamped to the
    expiry date, and clear the diff. Append-only — history is corrected forward."""
    import reconcile

    state = log.load_state()
    _report, diff = reconcile._find_diff(state, diff_id)
    if diff is None:
        raise ValueError(f"unknown diff id {diff_id!r} in the latest reconciliation report")
    if diff["classification"] != reconcile.EXPIRED_WORTHLESS_PENDING:
        raise ValueError(
            f"resolve_expiry only applies to EXPIRED_WORTHLESS_PENDING diffs "
            f"(diff {diff_id} is {diff['classification']}); use an adjustment instead")
    ticker = diff["ticker"]
    strike = diff["strike"]
    contracts = abs(int(diff.get("expected_qty") or 0))
    expiry = diff.get("expiry")
    stock_price = diff.get("expiry_close")

    close_payload = {"ticker": ticker, "strike": strike, "contracts": contracts,
                     "close_price_per_share": 0.0, "stock_price": stock_price}
    execution, apply = _close_short(close_payload, ticker, strike, contracts, stock_price)
    execution["mode"] = "live" if live_transmit() else "logged"
    execution["reason"] = "expired_worthless"
    execution["linked_diff_id"] = diff_id
    if expiry:
        execution["date"] = f"{str(expiry)[:10]}T20:00:00Z"  # timestamp to expiry day
    stored = log.append_execution(execution)

    state = log.load_state()
    position = log.find_position(state, ticker)
    if position is not None:
        apply(position)
    reconcile.mark_diff_resolved(state, diff_id, "resolve_expiry", {"execution_id": stored["id"]})
    log.recompute_derived(state)
    log.save_state(state)
    return {"success": True, "status": "resolved", "execution_id": stored["id"],
            "timestamp": stored["date"], "diff_id": diff_id, "execution": stored}


def acknowledge_diff(diff_id: str, ack_reason: str) -> dict:
    """Acknowledge a reconciliation diff as a non-issue (typed reason required),
    logged onto the reconciliation record. Lifts the freeze once the position's
    diffs are all resolved/acked. No execution is recorded — nothing changed at
    the broker, the operator is asserting the state is already correct."""
    import reconcile

    state = log.load_state()
    d = reconcile.ack_diff(state, diff_id, ack_reason)
    log.save_state(state)
    return {"success": True, "status": "acknowledged", "diff_id": diff_id, "diff": d}


def _enforce_account_gate(payload, ticker, contracts):
    """Run the Level 5 gate for an entry. Blocking failures raise ValueError
    (HTTP 400) unless override_reason is supplied; the gate result is stashed on
    the payload so _buy_leap can log the override + failed checks."""
    import account_gate
    leap_cost_ps = None
    if payload.get("execution_price"):  # per-contract dollars -> per-share
        leap_cost_ps = float(payload["execution_price"]) / 100.0
    gate = account_gate.evaluate(
        ticker, contracts=contracts,
        leap_cost_per_share=leap_cost_ps,
        weekly_extrinsic_per_share=payload.get("weekly_extrinsic_per_share"),
    )
    payload["_account_gate"] = gate
    if gate["pass"]:
        return
    reason = (payload.get("override_reason") or "").strip()
    if not reason:
        failed = ", ".join(gate["blocking_failures"])
        details = "; ".join(
            f"{c['id']}: {c['label']}" for c in gate["checks"]
            if c["blocking"] and not c["pass"])
        raise ValueError(
            f"Level 5 gate blocked entry ({failed}) — {details}. "
            "Pass override_reason to enter anyway (logged).")


def _validate_exit_reason(payload):
    """Normalize + validate the coded exit reason for an operator close, mutating
    ``payload['exit_reason']`` (canonical code) and ``payload['exit_note']`` in
    place. Rules (see exit_reasons.py):
      * a recognized coded reason passes through;
      * a blank reason is treated as OPERATOR_DISCRETION (the operator closed
        without categorizing);
      * an unrecognized non-blank reason is REJECTED (catches typos/legacy text);
      * OPERATOR_DISCRETION requires a typed exit_note — a no-note manual close is
        rejected (mirrors the account gate's typed-override pattern).
    """
    import exit_reasons
    raw = payload.get("exit_reason")
    note = (payload.get("exit_note") or "").strip()
    code = exit_reasons.normalize(raw)
    if code is None:
        if raw and str(raw).strip():
            raise ValueError(
                f"unknown exit_reason '{raw}' — expected one of "
                f"{sorted(exit_reasons.OPERATOR_SELECTABLE)}")
        code = exit_reasons.ExitReason.OPERATOR_DISCRETION  # blank -> discretionary
    if exit_reasons.requires_note(code) and not note:
        raise ValueError(
            f"exit_reason {code} requires a typed exit_note explaining the close "
            "(mirrors the Level-5 typed-override rule).")
    payload["exit_reason"] = code
    payload["exit_note"] = note or None


def _build_leg(payload, ticker, action, strike, contracts, stock_price):
    if action == "buy_leap":
        return _buy_leap(payload, ticker, strike, contracts, stock_price)
    if action == "sell_short":
        return _sell_short(payload, ticker, strike, contracts, stock_price)
    if action == "close_leap":
        return _close_leap(payload, ticker, strike, contracts, stock_price)
    return _close_short(payload, ticker, strike, contracts, stock_price)


def _commit(payload, ticker, action, contracts, strike, stock_price, price_source, mode):
    """Record one filled leg: append the immutable execution, apply the position
    mutation, and rebuild the derived ledgers. Shared by the paper path and the
    live fill-confirmation path."""
    execution, position_update = _build_leg(payload, ticker, action, strike, contracts, stock_price)
    execution["mode"] = mode
    execution["price_source"] = price_source
    # Fill-quality provenance for the slippage / mid-fill caveat: a paper fill is
    # booked at the quoted MIDPOINT (fill == mid), a live fill at the broker's
    # actual price. Capturing the reference mid (the placement limit for a live
    # fill; the fill itself for paper) lets realized slippage be measured later.
    execution["fill_assumption"] = "mid" if mode == "logged" else "broker"
    qm = payload.get("quoted_mid_per_share")
    if qm is None and mode == "logged":
        try:
            qm = round(_limit_price(action, payload), 4)
        except (TypeError, ValueError):
            qm = None
    execution["quoted_mid_per_share"] = qm
    # Legged-roll linkage: when a roll is executed as two independent single-leg
    # orders (the legacy fallback), each leg carries the shared roll linkage in
    # its payload so the roll ledger treats the pair identically to an atomic roll.
    _stamp_roll_linkage(execution, payload)
    _stamp_source_rec(execution, payload)
    stored = log.append_execution(execution)

    state = log.load_state()
    position = _ensure_position(state, ticker)
    position_update(position)
    log.recompute_derived(state)
    log.save_state(state)

    return {
        "success": True,
        "status": "filled",
        "execution_id": stored["id"],
        "timestamp": stored["date"],
        "mode": mode,
        "captured_price": stock_price,
        "execution": stored,
    }


def _stamp_roll_linkage(execution: dict, source: dict) -> None:
    """Copy a legged roll's shared linkage (roll_group_id / roll_leg / roll_reason)
    from a payload or pending record onto a leg's execution, so a legged roll and
    an atomic roll land identical fields in the roll ledger. roll_id (the ledger
    key) mirrors roll_group_id. No-op when there is no roll linkage present."""
    gid = source.get("roll_group_id")
    if gid is None:
        return
    execution["roll_group_id"] = gid
    execution["roll_id"] = gid
    if source.get("roll_leg") is not None:
        execution["roll_leg"] = source["roll_leg"]
    if source.get("roll_reason") is not None:
        execution["roll_reason"] = source["roll_reason"]


def _stamp_source_rec(execution: dict, source: dict) -> None:
    """Passive trust-layer annotation: when the operator staged this action from
    a recommendation card, the payload carries the rec id — copying it onto the
    immutable execution lets resolution matching prefer the exact record the
    operator acted on (fallback matching by type/position/validity still works
    without it). Never changes order behavior; no-op when absent."""
    rid = source.get("source_rec_id")
    if rid:
        execution["source_rec_id"] = str(rid)


def _limit_price(action, payload):
    """Per-share LIMIT price for the order leg. buy_leap/close_leap carry
    per-contract dollars (÷100); the short legs are already per-share."""
    if action == "buy_leap":
        return float(payload.get("execution_price") or 0) / 100.0
    if action == "close_leap":
        return float(payload.get("close_price") or 0) / 100.0
    if action == "sell_short":
        return float(payload.get("premium_per_share") or 0)
    return float(payload.get("close_price_per_share") or 0)  # close_short


def _place_live(payload, ticker, action, contracts, strike, stock_price, price_source):
    """Transmit a real single-leg LIMIT order and park it as pending. The fill is
    confirmed (and committed) later via order_status; cancel_order drops it."""
    _assert_transmit_allowed(action)
    _guard_resubmit(ticker, action)
    client = data_handler.client()
    account_hash = client.primary_account_hash()

    option_symbol = payload.get("option_symbol")
    if not option_symbol:
        expiration = payload.get("expiration")
        if not expiration:
            raise ValueError(f"{action} live order needs option_symbol or expiration to build the contract")
        option_symbol = schwab_api.occ_option_symbol(ticker, expiration, strike, call=True)

    limit = _limit_price(action, payload)
    order = schwab_api.build_single_leg_order(INSTRUCTION[action], contracts, option_symbol, limit)
    placed = client.place_order(account_hash, order)
    order_id = placed.get("orderId")
    if not order_id:
        raise schwab_api.SchwabError("Schwab accepted the order but returned no order id")

    pending = {
        "payload": payload, "ticker": ticker, "action": action, "contracts": contracts,
        "strike": strike, "stock_price": stock_price, "price_source": price_source,
        "account_hash": account_hash, "option_symbol": option_symbol,
        "limit_price": limit, "placed_at": log.utcnow(),
    }
    # Preserve a legged roll's shared linkage so each leg commits into the roll ledger.
    for k in ("roll_group_id", "roll_leg", "roll_reason"):
        if payload.get(k) is not None:
            pending[k] = payload[k]
    log.save_pending_order(order_id, pending)
    _record_placement(ticker, action, order_id, limit_price=limit)
    return {
        "success": True,
        "status": "working",
        "order_id": str(order_id),
        "mode": "live",
        "option_symbol": option_symbol,
        "limit_price": limit,
    }


def _fill_price(order: dict):
    """Best-effort average fill price from a Schwab order's activity legs."""
    try:
        for act in order.get("orderActivityCollection", []) or []:
            for leg in act.get("executionLegs", []) or []:
                if leg.get("price") is not None:
                    return float(leg["price"])
    except (TypeError, ValueError):
        pass
    return None


def _commit_from_pending(rec: dict, fill_price):
    """Commit a pending order once filled, overlaying the actual fill price onto
    the right payload field so the logged execution reflects the real fill."""
    payload = dict(rec.get("payload") or {})
    action = rec["action"]
    # Carry a legged roll's shared linkage from the pending record onto the payload
    # so _commit stamps it onto the leg's execution (roll-ledger equivalence).
    for k in ("roll_group_id", "roll_leg", "roll_reason"):
        if rec.get(k) is not None:
            payload[k] = rec[k]
    # Reference mid at order time (the placement limit was set at the quoted mid),
    # carried onto the execution so realized slippage = broker fill vs this mid.
    if rec.get("limit_price") is not None:
        payload["quoted_mid_per_share"] = round(float(rec["limit_price"]), 4)
    if fill_price is not None:
        if action == "buy_leap":
            payload["execution_price"] = fill_price * 100
        elif action == "close_leap":
            payload["close_price"] = fill_price * 100
        elif action == "sell_short":
            payload["premium_per_share"] = fill_price
        else:  # close_short
            payload["close_price_per_share"] = fill_price
    return _commit(payload, rec["ticker"], action, int(rec["contracts"]),
                   rec["strike"], rec["stock_price"], rec.get("price_source", "schwab"), "live")


def _commit_roll_from_pending(rec: dict, order: dict, units: int | None = None):
    """Commit a filled roll (or one whole-spread partial unit-batch): resolve the
    per-leg fill prices and how they were allocated (R2), then record both legs.
    ``units`` books only the newly-filled quantity (partial fills, R3)."""
    payload = dict(rec.get("payload") or {})
    # Reference net mid captured at ticket time (the placement limit) for R5.
    if rec.get("net_limit") is not None:
        payload["reference_net_mid"] = round(float(rec["net_limit"]), 4)
    close_px, open_px, method = _allocate_roll_fills(order, rec)
    if close_px is not None:
        payload["close_price_per_share"] = close_px
    if open_px is not None:
        payload["premium_per_share"] = open_px
    units = int(units if units is not None else rec["contracts"])
    return _commit_roll(payload, rec["ticker"], units, rec.get("stock_price"),
                        "live", rec.get("price_source", "schwab"),
                        roll_group_id=rec.get("roll_group_id"), alloc_method=method)


def _roll_rejection_fallback(rec: dict, order: dict, order_id: str) -> dict:
    """Schwab rejected the complex roll. Surface the reason and OFFER the legacy
    legged path — but ONLY behind an explicit operator confirmation (R6). Never
    auto-fall-back: this just describes the option, it executes nothing."""
    payload = rec.get("payload") or {}
    reason = (order.get("statusDescription") or order.get("cancelReason")
              or "Schwab rejected the complex (multi-leg) roll order")
    keep = ("from_strike", "to_strike", "contracts", "close_price_per_share",
            "premium_per_share", "from_expiration", "to_expiration", "from_option_symbol",
            "to_option_symbol", "to_dte", "roll_reason", "extrinsic_sold")
    return {
        "order_id": str(order_id), "status": "rejected", "raw_status": "REJECTED",
        "reason": reason,
        "fallback": {
            "available": True,
            "action": "roll_short",
            "confirm_field": "confirm_leg_manually",
            "prompt": ("Leg this roll manually? This carries legging risk — the two legs "
                       "can fill apart, briefly leaving the position uncovered or "
                       "double-covered."),
            "ticker": rec.get("ticker"),
            "roll": {k: payload.get(k) for k in keep if payload.get(k) is not None},
        },
    }


def _roll_order_status(rec: dict, order: dict, order_id: str, raw: str) -> dict:
    """Lifecycle for an atomic spread roll: whole-unit partial fills, leg-imbalance
    freeze, full fill, and explicit rejection fallback (R3/R6)."""
    close_sym = rec.get("close_option_symbol", "") or ""
    open_sym = rec.get("open_option_symbol", "") or ""
    total = int(rec.get("contracts") or 0)
    close_qty, open_qty = _roll_leg_filled_qty(order, close_sym, open_sym)

    # Leg imbalance is only actionable once no further fills can rebalance it (a
    # terminal state). While WORKING, an unequal snapshot is just a fill in
    # progress — we book only the whole units filled on BOTH legs.
    terminal = raw in ("FILLED", "CANCELED", "REJECTED", "EXPIRED")
    if close_qty != open_qty and terminal:
        log.pop_pending_order(order_id)
        return _freeze_for_leg_imbalance(rec["ticker"], order_id, close_qty, open_qty)

    filled_units = min(close_qty, open_qty)
    if raw == "FILLED" and filled_units == 0:
        # FILLED but Schwab reported no per-leg quantities — the whole order filled.
        filled_units = total
    already = int(rec.get("filled") or 0)
    new_units = filled_units - already

    if new_units > 0:
        result = _commit_roll_from_pending(rec, order, units=new_units)
        rec["filled"] = already + new_units
        rec["roll_group_id"] = result.get("roll_group_id")
        _capture_order_receipt(order_id, raw, rec, order, result)
        if rec["filled"] >= total or raw == "FILLED":
            log.pop_pending_order(order_id)
            return {**result, "order_id": order_id, "status": "filled", "raw_status": raw}
        # Whole units booked; the remainder stays pending until it fills or cancels.
        log.save_pending_order(order_id, rec)
        return {**result, "order_id": order_id, "status": "partially_filled", "raw_status": raw,
                "filled": rec["filled"], "remaining": total - rec["filled"]}

    if raw == "REJECTED":
        log.pop_pending_order(order_id)
        return _roll_rejection_fallback(rec, order, order_id)
    if raw in ("CANCELED", "EXPIRED"):
        log.pop_pending_order(order_id)
        return {"order_id": order_id, "status": "canceled", "raw_status": raw}
    return {"order_id": order_id, "status": "working", "raw_status": raw,
            "filled": already, "remaining": total - already}


def order_status(order_id: str) -> dict:
    """Poll a live order. On FILLED, commit it as an execution and clear the
    pending entry; on CANCELED/REJECTED/EXPIRED, clear it; otherwise it's still
    working."""
    rec = log.get_pending_order(order_id)
    if not rec:
        # Already resolved (committed or cleared) — nothing left to confirm.
        return {"order_id": order_id, "status": "unknown"}
    order = data_handler.client().get_order(rec["account_hash"], order_id)
    raw = (order.get("status") or "").upper()
    # The atomic roll has its own lifecycle (partial whole-unit fills, leg-imbalance
    # freeze, rejection fallback) — see _roll_order_status.
    if rec.get("kind") == "roll_short":
        return _roll_order_status(rec, order, order_id, raw)
    if raw == "FILLED":
        kind = rec.get("kind")
        if kind == "open":
            result = _commit_open_from_pending(rec, order)
        elif kind == "exit":
            result = _commit_exit_from_pending(rec, order)
        elif kind == "roll_leap":
            result = _commit_leap_roll_from_pending(rec, order)
        else:
            result = _commit_from_pending(rec, _fill_price(order))
        log.pop_pending_order(order_id)
        _capture_order_receipt(order_id, raw, rec, order, result)
        _settle_order(order_id, rec, olc.FILLED, raw)
        return {"order_id": order_id, "status": "filled", "raw_status": raw, **result}
    if raw in ("CANCELED", "REJECTED", "EXPIRED"):
        log.pop_pending_order(order_id)
        coded = {"REJECTED": olc.REJECTED, "EXPIRED": olc.EXPIRED}.get(raw, olc.CANCELED)
        _settle_order(order_id, rec, coded, raw)
        return {"order_id": order_id, "status": "rejected" if raw == "REJECTED" else "canceled",
                "raw_status": raw}
    return {"order_id": order_id, "status": "working", "raw_status": raw}


def _capture_order_receipt(order_id, raw_status, rec, order, result) -> None:
    """Record a broker fill receipt: the Schwab order id + the committed
    execution ids, so the live-order path can later be diffed against Schwab's
    own record (fill_verify.py). Belt-and-braces: a receipt failure must NEVER
    unwind a fill that has already been committed and cleared."""
    try:
        execs = result.get("executions") or (
            [result["execution"]] if result.get("execution") else [])
        log.save_order_receipt({
            "order_id": str(order_id),
            "kind": rec.get("kind") or rec.get("action"),
            "ticker": rec.get("ticker"),
            "account_hash": rec.get("account_hash"),
            "broker_status": raw_status,
            "execution_ids": [e.get("id") for e in execs if e.get("id")],
            "captured_at": log.utcnow(),
        })
    except Exception as e:  # noqa: BLE001 — never let bookkeeping unwind a fill
        log.logger.error("order receipt capture failed for %s: %s", order_id, e)


# Terminal broker states that confirm an order is truly gone (no longer working).
_TERMINAL_STATUSES = ("CANCELED", "REJECTED", "EXPIRED")
# Schwab's cancel is asynchronous — after the DELETE we re-poll the order to
# confirm it actually reached a terminal state. Bounded retries + interval come
# from provenance-tagged config (CANCEL_POLL_*); the module names are kept so the
# window stays monkeypatchable (tests set interval ~0 for an effectively mocked
# clock). TIMEOUT = interval x max attempts.
CANCEL_CONFIRM_POLL_S = config.CANCEL_POLL_INTERVAL_SEC
CANCEL_CONFIRM_TIMEOUT_S = config.CANCEL_POLL_INTERVAL_SEC * config.CANCEL_POLL_MAX_ATTEMPTS


def _safe_status(client, rec: dict, order_id: str) -> str:
    """Best-effort current broker status (uppercased), "" if the read fails."""
    try:
        return (client.get_order(rec["account_hash"], order_id).get("status") or "").upper()
    except Exception:  # noqa: BLE001 — status is best-effort at call sites
        return ""


def cancel_order(order_id: str) -> dict:
    """Cancel a working order at the broker and drop the pending entry — BROKER
    FIRST (rule 1). The local pending record is cleared ONLY once the order is
    confirmed gone at the broker (a terminal state). A cancel that fails leaves the
    pending record in place and surfaces the error: dropping it would make us
    forget an order still working at Schwab, and the next order would collide.

    Lifecycle races handled up front: the order may have already FILLED (settle it
    as a fill, never lose it) or already be terminal (clear the stale record, which
    for a partial fill trips the defensive-review path). Otherwise we transition to
    CANCEL_REQUESTED, DELETE with bounded retries, and CONFIRM a terminal state
    before claiming the cancel — the 2xx ack alone is not trusted (rule 2).

    If every DELETE fails while the order is still WORKING, the broker state is
    effectively UNKNOWN: hard-lock the position (no resubmit while unknown, rule 5)
    and raise, keeping the pending record for the startup reconciler."""
    rec = log.get_pending_order(order_id)
    if not rec:
        # Already resolved (committed or cleared) — nothing left to cancel.
        return {"order_id": order_id, "status": "canceled"}

    client = data_handler.client()
    # Reconcile against the broker's current view before attempting the cancel.
    raw = _safe_status(client, rec, order_id)
    if raw == "FILLED":
        # It filled before we asked to cancel — a clean fill, commit it (no alert).
        return order_status(order_id)
    if raw in _TERMINAL_STATUSES:
        return _finalize_cancel_terminal(client, rec, order_id, cancel_requested=False)

    # Still working — record the cancel request, then DELETE with bounded retries.
    _settle_order(order_id, rec, olc.CANCEL_REQUESTED, raw or "WORKING")
    last_err: Exception | None = None
    for _ in range(max(1, int(config.CANCEL_POLL_MAX_ATTEMPTS))):
        try:
            client.cancel_order(rec["account_hash"], order_id)
            return _confirm_cancel(client, rec, order_id)
        except Exception as e:  # noqa: BLE001 — broker refused the DELETE
            last_err = e
            # Rule 4: the order may have raced to a FILL (a filled order can't be
            # cancelled — that's why the DELETE failed). Settle the fill instead.
            chk = _safe_status(client, rec, order_id)
            if chk == "FILLED":
                return order_status(order_id)
            if chk in _TERMINAL_STATUSES:
                return _finalize_cancel_terminal(client, rec, order_id, cancel_requested=True)
            if config.CANCEL_POLL_INTERVAL_SEC:
                time.sleep(config.CANCEL_POLL_INTERVAL_SEC)
    # Rule 5: every cancel failed and the order is still working — broker state is
    # unknown. Hard-lock (no resubmit ever while unknown) and surface the error.
    _hard_lock_unknown(order_id, rec, str(last_err))
    raise schwab_api.SchwabError(
        f"cancel of order {order_id} failed and it is still WORKING at the broker — "
        f"position hard-locked pending manual reconciliation: {last_err}")


def _confirm_cancel(client, rec: dict, order_id: str) -> dict:
    """Confirm a just-issued cancel actually took before dropping the pending
    record. Schwab cancels asynchronously, so poll (bounded) until the order is
    terminal, fills (settle it), or the window closes.

    If it hasn't gone terminal within the window it may be PENDING_CANCEL or still
    WORKING (and could yet fill) — keep the pending record and report
    ``pending_cancel`` so the operator is told the order may still be live."""
    deadline = time.monotonic() + CANCEL_CONFIRM_TIMEOUT_S
    raw = ""
    while time.monotonic() < deadline:
        time.sleep(CANCEL_CONFIRM_POLL_S)
        raw = _safe_status(client, rec, order_id)
        if not raw:
            continue
        if raw == "FILLED" or raw in _TERMINAL_STATUSES:
            # Terminal after we requested the cancel — resolve it with fill/partial
            # awareness (a FILLED here is a fill-DURING-cancel; a partial is a
            # defensive-review state). cancel_requested=True drives that mapping.
            return _finalize_cancel_terminal(client, rec, order_id, cancel_requested=True)

    # Accepted but not yet terminal — the order may still be working at Schwab.
    # Keep the pending record (never popped above) and say so plainly.
    return {"order_id": order_id, "status": "pending_cancel", "raw_status": raw or "PENDING_CANCEL"}


def _finalize_cancel_terminal(client, rec: dict, order_id: str, *, cancel_requested: bool) -> dict:
    """Resolve an order that is terminal at the broker during/after a cancel, with
    fill-quantity awareness (rules 3-4). Maps the raw status + filled quantity to a
    coded state and acts:

    - FILLED / fully filled -> reconcile the fill (never lose it). If it filled
      AFTER we requested the cancel, it's a fill-DURING-cancel: the position is
      unexpectedly LIVE -> high-priority alert, and NO resubmit (lock left blocking).
    - partial fill + canceled/expired -> PARTIAL_FILL_CANCELED: freeze the position
      for defensive review (trips the delta-coverage check) + alert. Flag only; the
      app never auto-fixes an unbalanced position. Resubmit blocked.
    - clean canceled/rejected/expired, zero filled -> clear the record; resubmit
      allowed once the terminal event is logged."""
    order = client.get_order(rec["account_hash"], order_id)
    raw = (order.get("status") or "").upper()
    filled, ordered = _order_filled_qty(order)

    if raw == "FILLED":
        # A genuine broker fill: order_status re-reads FILLED and books it through
        # the same commit/receipt path (no second, divergent commit here). If it
        # filled AFTER we asked to cancel it's a fill-DURING-cancel — the position
        # is unexpectedly LIVE: alert and leave the lock blocking (no resubmit).
        result = order_status(order_id)
        if cancel_requested:
            _settle_order(order_id, rec, olc.FILLED_DURING_CANCEL, raw, filled=filled)
            _alert_order(
                "ORDER_FILLED_DURING_CANCEL", rec.get("ticker"),
                f"{rec.get('ticker')} order {order_id} FILLED during cancel — the position "
                "is LIVE. Reconciled the fill; do NOT resubmit. Confirm delta coverage.",
                data={"order_id": str(order_id), "filled": filled})
        return result

    if filled > 0:
        # Terminal-but-not-FILLED yet some quantity filled: an unbalanced position
        # (or, in the contradictory "canceled-yet-filled" case, an ambiguous one).
        # Never silently commit OR drop it — freeze for defensive review and trip
        # the delta-coverage guardrail. The app flags; it never auto-fixes (rule 4).
        log.pop_pending_order(order_id)
        _settle_order(order_id, rec, olc.PARTIAL_FILL_CANCELED, raw, filled=filled, ordered=ordered)
        return _freeze_for_partial_fill_cancel(rec.get("ticker"), order_id, filled, ordered)

    # Clean terminal, zero filled.
    coded = olc.map_broker_status(raw, cancel_requested=cancel_requested)
    if not olc.is_terminal(coded):
        coded = olc.CANCELED  # defensive: an unexpected non-terminal here is treated as gone
    log.pop_pending_order(order_id)
    _settle_order(order_id, rec, coded, raw)
    return {"order_id": order_id,
            "status": "rejected" if coded == olc.REJECTED else "canceled",
            "raw_status": raw}


def _freeze_for_partial_fill_cancel(ticker: str, order_id: str, filled: float, ordered: float) -> dict:
    """A partial fill remained after the cancel: some quantity is LIVE, the rest was
    canceled — a two-leg entry can now be unbalanced (delta coverage unverified).
    Record it as a distinct coded review state, trip the delta-coverage guardrail
    review, and alert. The app FLAGS; it never auto-fixes (rule 4)."""
    summary = (f"order {order_id}: PARTIAL fill on cancel — {int(filled)} of {int(ordered)} "
               "filled, remainder canceled. Position may be unbalanced and its delta "
               "coverage is unverified. Frozen for review; the app will not auto-fix.")
    state = log.load_state()
    position = log.find_position(state, ticker) if ticker else None
    if position is not None:
        position["needs_review"] = True
        review = dict(position.get("review") or {})
        review["since"] = log.utcnow()
        review["summary"] = summary
        classes = set(review.get("classifications") or [])
        classes.add("PARTIAL_FILL_CANCELED")
        classes.add("DELTA_COVERAGE_CHECK")  # trips the delta-coverage guardrail review
        review["classifications"] = sorted(classes)
        review["partial_fill_cancel"] = {
            "order_id": str(order_id), "filled": int(filled), "ordered": int(ordered),
            "at": log.utcnow()}
        position["review"] = review
        log.save_state(state)
    _alert_order("ORDER_PARTIAL_FILL_CANCELED", ticker, summary,
                 data={"order_id": str(order_id), "filled": int(filled), "ordered": int(ordered)})
    log.logger.error("PARTIAL FILL ON CANCEL %s (%s): %s/%s — froze position, no auto-fix",
                     order_id, ticker, int(filled), int(ordered))
    return {"order_id": order_id, "status": "partial_fill_canceled", "frozen": True,
            "ticker": ticker, "filled": int(filled), "ordered": int(ordered), "summary": summary}


def _hard_lock_unknown(order_id: str, rec: dict, err: str) -> None:
    """The broker state of an order is UNKNOWN (cancel failed, still working). Lock
    the position intent so no new order can be sent while a working order might be
    live (rule 5), log a LOCKED_UNKNOWN transition, and alert. The pending record
    is kept — the startup reconciler (or a later poll) resolves it."""
    _settle_order(order_id, rec, olc.LOCKED_UNKNOWN, "UNKNOWN", error=err)
    _alert_order(
        "ORDER_STATE_UNKNOWN", rec.get("ticker"),
        f"{rec.get('ticker')} order {order_id}: cancel failed and the order may still be "
        "WORKING at the broker. Position hard-locked — resolve manually before trading it.",
        data={"order_id": str(order_id), "error": err})


def reconcile_pending_orders_on_startup() -> dict:
    """On app start, re-poll every locally non-terminal pending order against the
    broker BEFORE any new order activity is allowed for those positions (rule 6).

    A crash can leave a WORKING order in state.json that the app has otherwise
    forgotten. Re-polling settles it (fill / cancel / reject / partial), which
    releases or review-locks its intent. If the broker can't be reached for an
    order, its position is HARD-LOCKED (LOCKED_UNKNOWN) so no new order can be sent
    while a working order might still be live — a crash mid-cancel must never
    orphan a broker order invisibly. Best-effort per order; one failure never
    blocks reconciling the rest. Safe no-op when no live broker is configured."""
    if not schwab_api.configured():
        return {"reconciled": 0, "pending": 0, "skipped": "broker-not-configured"}
    pending = list(log.list_pending_orders().items())
    resolved = 0
    for order_id, rec in pending:
        try:
            res = order_status(order_id)
            if res.get("status") not in ("working", "pending_cancel", "unknown"):
                resolved += 1
        except Exception as e:  # noqa: BLE001 — unreachable broker: hard-lock, don't skip
            log.logger.error("startup reconcile: order %s unresolved (%s) — hard-locking", order_id, e)
            try:
                _hard_lock_unknown(order_id, rec, str(e))
            except Exception:  # noqa: BLE001 — never let one bad order abort startup
                pass
    if pending:
        log.logger.info("startup reconcile: %s/%s pending orders resolved", resolved, len(pending))
    return {"reconciled": resolved, "pending": len(pending)}


def _capture_entry_context(ticker: str, payload: dict) -> dict | None:
    """Freeze the immutable entry_context snapshot at entry time, and fire the
    low-severity data-quality alert if too many tracked fields came back null.

    entry_context.capture is already fully guarded (never raises, never fetches),
    but this wrapper is belt-and-suspenders: snapshot capture must NEVER block or
    delay an execution (config.SNAPSHOT_NEVER_BLOCKS_EXECUTION), so any failure
    here degrades to a null snapshot and the trade still logs."""
    try:
        import entry_context
        snap = entry_context.capture(ticker, payload)
    except Exception:  # noqa: BLE001 — a snapshot must never block an entry
        return None
    try:
        dq = (snap or {}).get("data_quality") or {}
        if dq.get("over_null_threshold"):
            import alerts
            alerts.record_event(
                "SNAPSHOT_DATA_QUALITY", ticker,
                f"{ticker} entry snapshot: {dq['null_fields']}/{dq['tracked_fields']} "
                f"tracked fields null ({dq['null_field_fraction']:.0%}) — "
                "calibration telemetry for this entry is thin.",
                data={"null_field_fraction": dq["null_field_fraction"],
                      "missing": dq.get("missing", [])},
                # LOW-severity telemetry — logged for visibility, not pushed to
                # the operator's phone (a data-quality note, not a trade signal).
                notify=False)
    except Exception:  # noqa: BLE001 — alerting is best-effort too
        pass
    return snap


def _norm_exp(value) -> str | None:
    return str(value)[:10] if value else None


def _match_leg(legs: list[dict], strike, expiration=None) -> dict | None:
    """Find the LEAP leg a strike (+ optional expiration) identifies. Strike
    alone matches when it's unambiguous; expiration disambiguates same-strike
    ladders. Used identically at build time (to stamp the execution) and at
    apply time (to mutate the position), so the two can never disagree."""
    exp = _norm_exp(expiration)
    matches = [l for l in legs if _strike_eq(l.get("strike"), strike)]
    if exp is not None:
        matches = [l for l in matches if _norm_exp(l.get("expiration")) == exp]
    return matches[0] if matches else None


def _buy_leap(payload, ticker, strike, contracts, stock_price):
    # execution_price is per-contract total dollars; execution_total is the trade.
    price_per_contract = float(payload.get("execution_price") or 0)
    total = float(payload.get("execution_total") or price_per_contract * contracts)
    intrinsic_per_contract = max((stock_price or 0) - (strike or 0), 0) * 100
    extrinsic_at_entry = float(payload.get("extrinsic_captured")
                               or max(price_per_contract - intrinsic_per_contract, 0) * contracts)
    execution = {
        "ticker": ticker, "action": "buy_leap", "strike": strike, "contracts": contracts,
        "execution_price": price_per_contract, "execution_total": total,
        "extrinsic_captured": round(extrinsic_at_entry, 2), "stock_price": stock_price,
        "expiration": _norm_exp(payload.get("expiration")),
    }

    # Multi-tranche classification, stamped on the immutable record so the
    # derived-ledger replay never needs position state: "merge" = more of the
    # identical contract (scale-in), "add" = a new leg beside existing ones.
    # Absent = fresh entry (or a roll — the roll id decides that at replay).
    existing = log.find_position(log.load_state(), ticker)
    legs_now = log.leap_legs(existing) if existing else []
    if _match_leg(legs_now, strike, payload.get("expiration")) is not None:
        execution["leap_add"] = "merge"
    elif legs_now:
        execution["leap_add"] = "add"

    # Level-5 gate context: log any override (with what it overrode) on the
    # immutable record, and resolve the circuit breaker + dividend to store.
    gate = payload.get("_account_gate") or {}
    if payload.get("override_reason"):
        execution["override"] = {
            "reason": str(payload["override_reason"]).strip(),
            "failed_checks": gate.get("blocking_failures", []),
        }

    # Entry REQUIRES a line-in-the-sand: operator's price, else the suggested
    # default max(MA50, entry - 2xATR) — the entry always stores one.
    cb_price = payload.get("circuit_breaker_price")
    cb_source = "operator"
    if cb_price is None:
        cb_price = (gate.get("suggested_circuit_breaker") or {}).get("price")
        cb_source = "default"
        if cb_price is None:
            import account_gate
            cb_price = account_gate.suggested_circuit_breaker(ticker).get("price")
    # entry_price is the underlying's price at entry — the reference the
    # circuit-breaker drawdown leg (>= 15% drop) measures against.
    circuit_breaker = ({"price": round(float(cb_price), 2), "source": cb_source,
                        "set_at": log.utcnow()[:10],
                        "entry_price": round(float(stock_price), 2) if stock_price else None}
                       if cb_price is not None else None)
    execution["circuit_breaker_price"] = circuit_breaker["price"] if circuit_breaker else None

    dividend = gate.get("dividend")
    if dividend is None:
        import dividends
        try:
            dividend = dividends.next_dividend(ticker)
        except Exception:  # noqa: BLE001 — dividend data must never block an entry
            dividend = {"ex_date": None, "amount": None, "source": "error"}

    # Entry-context snapshot AT ENTRY, frozen onto the immutable execution — the
    # closed-cycle record later shows every feature value that produced the GO
    # verdict (this cannot be re-derived after the fact). It's also mirrored onto
    # the position (apply below) for the live UI. Captured once; never modified.
    entry_context = _capture_entry_context(ticker, payload)
    execution["entry_context"] = entry_context

    def apply(position):
        legs = log.leap_legs(position)
        position["leap_legs"] = legs
        match = _match_leg(legs, strike, payload.get("expiration"))
        if match is not None:
            # Scale-in: more of the identical contract — counts, cost and the
            # extrinsic payback target all add; the cycle just grows.
            match["contracts"] = int(match.get("contracts") or 0) + contracts
            match["cost_basis"] = round(float(match.get("cost_basis") or 0) + total, 2)
            match["current_bid"] = round(float(match.get("current_bid") or 0) + total, 2)
            match["intrinsic"] = round(intrinsic_per_contract * match["contracts"], 2)
            match["extrinsic"] = round(float(match.get("extrinsic") or 0) + extrinsic_at_entry, 2)
            match["extrinsic_at_entry"] = round(
                float(match.get("extrinsic_at_entry") or 0) + extrinsic_at_entry, 2)
        else:
            legs.append({
                "strike": strike, "contracts": contracts, "cost_basis": total,
                "current_bid": total, "intrinsic": round(intrinsic_per_contract * contracts, 2),
                "extrinsic": round(extrinsic_at_entry, 2),
                "entry_date": log.utcnow()[:10], "dte": payload.get("dte", config.LEAP_TARGET_DTE),
                "expiration": payload.get("expiration"),
                "extrinsic_at_entry": round(extrinsic_at_entry, 2), "extrinsic_collected_to_date": 0,
            })
        if len(legs) == 1 and match is None:
            # Fresh entry (or a roll's buy side): a new engine starts a new
            # cycle. Adds to a running engine keep the original entry date and
            # line-in-the-sand — scaling in must not move the sand line.
            position["entry_date"] = log.utcnow()[:10]
            position["circuit_breaker"] = circuit_breaker
            position["dividend"] = dividend
            # Freeze the entry-context onto the position (written once, never
            # regenerated — recompute_derived treats it as opaque raw record).
            position["entry_context"] = entry_context
        else:
            if position.get("circuit_breaker") is None:
                position["circuit_breaker"] = circuit_breaker
            if position.get("dividend") is None:
                position["dividend"] = dividend
        position["leap"] = legs[0]
        position["status"] = "active"
    return execution, apply


def _close_leap(payload, ticker, strike, contracts, stock_price):
    """Sell the deep-ITM LEAP to close (exit or roll the long).

    close_price is per-contract total dollars (mirrors buy_leap's execution_price).
    Realized P&L is the sale proceeds minus the stored cost basis; the position's
    leap is cleared and the position is marked closed if no shares/shorts remain.
    """
    close_per_contract = float(payload.get("close_price") or 0)
    close_total = float(payload.get("close_total") or close_per_contract * contracts)
    intrinsic_per_contract = max((stock_price or 0) - (strike or 0), 0) * 100
    extrinsic_remaining = max(close_per_contract - intrinsic_per_contract, 0) * contracts

    # Cost basis from the stored LEAP leg the strike/expiration identifies
    # (caller may override). With one leg this is exactly the old behavior.
    state = log.load_state()
    position = log.find_position(state, ticker)
    legs_now = log.leap_legs(position) if position else []
    leg = _match_leg(legs_now, strike, payload.get("expiration")) or (legs_now[0] if legs_now else {})
    cost_basis = payload.get("cost_basis")
    cost_basis = float(cost_basis if cost_basis is not None else leg.get("cost_basis") or 0)
    realized_pnl = round(close_total - cost_basis, 2)

    # Coded exit reason + optional typed note, frozen on the immutable close so
    # the derived cycle can be bucketed by why it ended (and never re-inferred).
    # For operator closes these were validated/normalized at the execute()
    # boundary; the internal LEAP-roll close supplies its own coded reason.
    import entry_context
    import exit_reasons
    exit_reason = exit_reasons.normalize(payload.get("exit_reason"))
    execution = {
        "ticker": ticker, "action": "close_leap", "strike": strike, "contracts": contracts,
        "close_price": close_per_contract, "close_total": close_total, "stock_price": stock_price,
        "cost_basis": round(cost_basis, 2), "realized_pnl": realized_pnl,
        "extrinsic_remaining": round(extrinsic_remaining, 2),
        "exit_reason": exit_reason,
        "exit_note": (payload.get("exit_note") or None),
        # Exit-time counterpart metrics (same stock-level set as the entry
        # snapshot) so calibration can compute entry->exit deltas.
        "exit_metrics": entry_context.exit_metrics(ticker),
        "expiration": _norm_exp(leg.get("expiration") or payload.get("expiration")),
        # Stamped so the payback replay knows whether this close ended the
        # cycle (last leg out) or the engine kept running on remaining legs.
        "legs_remaining": max(len(legs_now) - 1, 0),
    }

    def apply(position):
        legs = log.leap_legs(position)
        position["leap_legs"] = legs
        target = _match_leg(legs, strike, payload.get("expiration")) or (legs[0] if legs else None)
        if target is not None:
            legs.remove(target)
        position["leap"] = legs[0] if legs else None
        shares = position.get("shares") or {}
        if not legs and not position.get("short_calls") and int(shares.get("count") or 0) == 0:
            position["status"] = "closed"
    return execution, apply


def _sell_short(payload, ticker, strike, contracts, stock_price):
    premium_per_share = float(payload.get("premium_per_share") or 0)
    premium_total = float(payload.get("premium_total") or premium_per_share * contracts * 100)
    intrinsic_per_share = max((stock_price or 0) - (strike or 0), 0)
    entry_extrinsic_per_share = round(max(premium_per_share - intrinsic_per_share, 0), 4)
    execution = {
        "ticker": ticker, "action": "sell_short", "strike": strike, "contracts": contracts,
        "premium_per_share": premium_per_share, "premium_total": premium_total,
        "stock_price": stock_price, "entry_extrinsic_per_share": entry_extrinsic_per_share,
    }

    def apply(position):
        position.setdefault("short_calls", []).append({
            "strike": strike, "contracts": contracts, "open_date": log.utcnow()[:10],
            "expiration": payload.get("expiration"),
            "dte": payload.get("dte", 5), "entry_extrinsic_per_share": entry_extrinsic_per_share,
            "entry_premium_total": premium_total, "current_bid": premium_per_share,
            "current_cost": premium_total,
        })
    return execution, apply


def _close_short(payload, ticker, strike, contracts, stock_price):
    close_per_share = float(payload.get("close_price_per_share") or 0)
    close_total = float(payload.get("close_total") or close_per_share * contracts * 100)
    intrinsic_per_share = max((stock_price or 0) - (strike or 0), 0)
    extrinsic_paid_back = round(max(close_per_share - intrinsic_per_share, 0), 4)

    # Pull the matching open short to recover what extrinsic we originally sold.
    state = log.load_state()
    position = log.find_position(state, ticker)
    extrinsic_sold = payload.get("extrinsic_sold")
    if extrinsic_sold is None and position:
        for sc in position.get("short_calls", []):
            if sc.get("strike") == strike:
                extrinsic_sold = sc.get("entry_extrinsic_per_share")
                break
    extrinsic_sold = round(float(extrinsic_sold or 0), 4)
    net_juice = round(extrinsic_sold - extrinsic_paid_back, 4)
    net_juice_total = round(net_juice * contracts * 100, 2)
    execution = {
        "ticker": ticker, "action": "close_short", "strike": strike, "contracts": contracts,
        "close_price_per_share": close_per_share, "close_total": close_total,
        "stock_price": stock_price, "extrinsic_sold": extrinsic_sold,
        "extrinsic_paid_back": extrinsic_paid_back, "net_juice": net_juice,
        "net_juice_total": net_juice_total,
    }

    def apply(position):
        # Contract-aware close: reduce the matching short leg(s) by the closed
        # quantity, dropping a leg only when it is fully closed. A full close
        # (contracts == the leg's contracts) reduces to zero and drops it — the
        # original behavior — while a partial close (a partially-filled roll)
        # leaves the remainder open with proportionally-scaled cost fields.
        remaining = int(contracts or 0)
        kept = []
        for sc in position.get("short_calls", []):
            if remaining <= 0 or sc.get("strike") != strike:
                kept.append(sc)
                continue
            have = int(sc.get("contracts") or 0)
            if have > remaining:
                frac = (have - remaining) / have
                sc["contracts"] = have - remaining
                for k in ("entry_premium_total", "current_cost"):
                    if sc.get(k) is not None:
                        sc[k] = round(float(sc[k]) * frac, 2)
                remaining = 0
                kept.append(sc)
            else:
                remaining -= have  # leg fully closed -> dropped
        position["short_calls"] = kept
    return execution, apply


def _roll_reason(payload) -> str:
    reason = (payload.get("roll_reason") or "").strip()
    return reason if reason in ROLL_REASONS else "scheduled"


def _next_roll_id(state) -> str:
    n = sum(1 for e in state.get("executions", [])
            if e.get("roll_id") and e.get("action") == "close_short")
    return f"roll_{n + 1:03d}"


def _roll_short(payload, ticker, contracts, stock_price, mode, price_source):
    """Roll an open short in one operation: buy to close the existing leg, then
    sell a new one. The caller chooses the new week (``to_expiration``/``to_dte``)
    and strike (``to_strike``) independently — same week / different week and same
    strike / different strike are all just different values here.

    Paper (logged) mode records both legs immediately at the supplied/midpoint
    prices. LIVE mode transmits ONE two-leg net-credit/debit ticket (no legging
    risk) and commits both legs only when the ticket fills, via the same
    pending -> poll -> commit/auto-cancel lifecycle as single-leg orders."""
    from_strike = payload.get("from_strike", payload.get("strike"))
    to_strike = payload.get("to_strike")
    if from_strike is None or to_strike is None:
        raise ValueError("roll_short requires from_strike and to_strike")
    contracts = int(contracts or 0)
    if mode == "live" and schwab_api.configured():
        # Atomic by default (ATOMIC_ROLLS_ENABLED); the legacy legged path is used
        # only when the flag is off OR the operator explicitly confirmed manual
        # legging after a rejection (R6/R7). Never a silent fallback.
        atomic = config.ATOMIC_ROLLS_ENABLED and not payload.get("confirm_leg_manually")
        if atomic:
            return _place_live_roll(payload, ticker, contracts, stock_price, price_source)
        return _place_legged_roll(payload, ticker, contracts, stock_price, price_source)
    return _commit_roll(payload, ticker, contracts, stock_price, mode, price_source)


def _place_legged_roll(payload, ticker, contracts, stock_price, price_source):
    """Legacy legged roll: TWO independent single-leg live orders (buy-to-close the
    old short, then sell-to-open the new one). This carries legging risk — the legs
    can fill apart — and is reached only when ATOMIC_ROLLS_ENABLED is off or the
    operator explicitly confirmed manual legging. Both legs share a roll_group_id so
    the roll ledger treats the pair identically to an atomic roll."""
    _assert_transmit_allowed("roll_short")
    from_strike = payload.get("from_strike", payload.get("strike"))
    to_strike = payload.get("to_strike")
    roll_group_id = _next_roll_id(log.load_state())
    reason = _roll_reason(payload)
    close_payload = {
        "action": "close_short", "ticker": ticker, "strike": from_strike, "contracts": contracts,
        "close_price_per_share": payload.get("close_price_per_share"),
        "option_symbol": payload.get("from_option_symbol"),
        "expiration": payload.get("from_expiration"),
        "extrinsic_sold": payload.get("extrinsic_sold"), "stock_price": stock_price,
        "roll_group_id": roll_group_id, "roll_leg": "close", "roll_reason": reason,
    }
    open_payload = {
        "action": "sell_short", "ticker": ticker, "strike": to_strike, "contracts": contracts,
        "premium_per_share": payload.get("premium_per_share"),
        "option_symbol": payload.get("to_option_symbol"),
        "expiration": payload.get("to_expiration"),
        "dte": payload.get("to_dte", payload.get("dte", 5)), "stock_price": stock_price,
        "roll_group_id": roll_group_id, "roll_leg": "open", "roll_reason": reason,
    }
    # Buy-to-close first, then sell-to-open — the historical legged order.
    close_res = _place_live(close_payload, ticker, "close_short", contracts,
                            from_strike, stock_price, price_source)
    open_res = _place_live(open_payload, ticker, "sell_short", contracts,
                           to_strike, stock_price, price_source)
    return {
        "success": True, "status": "working", "mode": "live", "legged": True,
        "roll_group_id": roll_group_id,
        "orders": [close_res, open_res],
        "warning": ("Legged roll: two independent orders — the legs can fill apart "
                    "(legging risk). Monitor both fills."),
    }


def _place_live_roll(payload, ticker, contracts, stock_price, price_source):
    """Transmit the roll as a single two-leg NET order and park it as pending."""
    _assert_transmit_allowed("roll_short")
    client = data_handler.client()
    account_hash = client.primary_account_hash()

    def _symbol(prefix):
        sym = payload.get(f"{prefix}_option_symbol")
        if sym:
            return sym
        expiration = payload.get(f"{prefix}_expiration")
        strike = payload.get(f"{prefix}_strike")
        if not expiration:
            raise ValueError(
                f"live roll needs {prefix}_option_symbol or {prefix}_expiration to build the contract")
        return schwab_api.occ_option_symbol(ticker, expiration, strike, call=True)

    close_symbol = _symbol("from")
    open_symbol = _symbol("to")
    buyback = float(payload.get("close_price_per_share") or 0)
    new_premium = float(payload.get("premium_per_share") or 0)
    net = round(new_premium - buyback, 2)
    order = schwab_api.build_roll_order(contracts, close_symbol, open_symbol, net)
    placed = client.place_order(account_hash, order)
    order_id = placed.get("orderId")
    if not order_id:
        raise schwab_api.SchwabError("Schwab accepted the roll but returned no order id")

    log.save_pending_order(order_id, {
        "kind": "roll_short",
        "payload": payload, "ticker": ticker, "action": "roll_short",
        "contracts": contracts, "stock_price": stock_price,
        "price_source": price_source, "account_hash": account_hash,
        "close_option_symbol": close_symbol, "open_option_symbol": open_symbol,
        "net_limit": net, "placed_at": log.utcnow(),
    })
    return {
        "success": True,
        "status": "working",
        "order_id": str(order_id),
        "mode": "live",
        "option_symbols": [close_symbol, open_symbol],
        "net_limit": net,
    }


def _roll_leg_fills(order: dict, close_symbol: str, open_symbol: str):
    """(close_fill, open_fill) per-share prices from a filled two-leg order's
    activity, matched by legId -> orderLegCollection symbol. None when absent."""
    leg_symbol = {}
    for i, leg in enumerate(order.get("orderLegCollection") or [], start=1):
        sym = ((leg.get("instrument") or {}).get("symbol") or "").strip()
        leg_symbol[leg.get("legId") or i] = sym
    close_px = open_px = None
    try:
        for act in order.get("orderActivityCollection", []) or []:
            for leg in act.get("executionLegs", []) or []:
                price = leg.get("price")
                if price is None:
                    continue
                sym = leg_symbol.get(leg.get("legId"))
                if sym == close_symbol.strip():
                    close_px = float(price)
                elif sym == open_symbol.strip():
                    open_px = float(price)
    except (TypeError, ValueError):
        pass
    return close_px, open_px


def _roll_leg_filled_qty(order: dict, close_symbol: str, open_symbol: str):
    """(close_filled, open_filled) cumulative filled CONTRACT counts per leg from
    a Schwab order's activity, matched by legId -> orderLegCollection symbol.

    A spread fills as whole units, so a healthy fill has equal counts on both
    legs; unequal counts are a leg imbalance (R3) that must freeze the position,
    never book a one-legged fill. Returns (0, 0) when no fills are reported yet.
    NOTE: Schwab's exact partial-fill quantity fields are a LIVE-VERIFY item."""
    leg_symbol = {}
    for i, leg in enumerate(order.get("orderLegCollection") or [], start=1):
        sym = ((leg.get("instrument") or {}).get("symbol") or "").strip()
        leg_symbol[leg.get("legId") or i] = sym
    close_qty = open_qty = 0.0
    try:
        for act in order.get("orderActivityCollection", []) or []:
            for leg in act.get("executionLegs", []) or []:
                qty = leg.get("quantity")
                if qty is None:
                    continue
                sym = leg_symbol.get(leg.get("legId"))
                if sym == close_symbol.strip():
                    close_qty += float(qty)
                elif sym == open_symbol.strip():
                    open_qty += float(qty)
    except (TypeError, ValueError):
        pass
    return int(round(close_qty)), int(round(open_qty))


def _allocate_roll_fills(order: dict, rec: dict):
    """Resolve the per-leg fill prices for a filled roll and how they were
    derived (R2). Priority:

      1. Schwab reports both per-leg fill prices  -> use them ("broker_per_leg").
      2. Per-leg prices absent but a net is known -> split the net across the two
         legs proportional to the reference mids captured at ticket time
         ("proportional_to_mid").
      3. Neither available -> keep the staged mid estimates ("staged_estimate").

    Returns (close_price_per_share, open_price_per_share, method)."""
    close_sym = rec.get("close_option_symbol", "") or ""
    open_sym = rec.get("open_option_symbol", "") or ""
    close_px, open_px = _roll_leg_fills(order, close_sym, open_sym)
    if close_px is not None and open_px is not None:
        return close_px, open_px, "broker_per_leg"

    payload = rec.get("payload") or {}
    ref_close = float(payload.get("close_price_per_share") or 0)
    ref_open = float(payload.get("premium_per_share") or 0)
    ref_net = round(ref_open - ref_close, 4)
    # Best available realized net: the placement limit (a filled DAY limit order
    # trades at or better than its limit; without per-leg data the limit is the
    # honest anchor). LIVE-VERIFY: confirm Schwab exposes no net-fill field.
    net_fill = rec.get("net_limit")
    net_fill = float(net_fill) if net_fill is not None else ref_net
    if abs(ref_net) > 1e-9:
        # Scale both legs by the same factor so open-close == net_fill while
        # preserving each leg's share of the spread (proportional to its mid).
        k = net_fill / ref_net
        return round(ref_close * k, 4), round(ref_open * k, 4), "proportional_to_mid"
    return (close_px if close_px is not None else ref_close,
            open_px if open_px is not None else ref_open, "staged_estimate")


def _freeze_for_leg_imbalance(ticker: str, order_id: str, close_qty: int, open_qty: int) -> dict:
    """A spread reported a leg-imbalanced fill (one leg filled, the other did not).
    Per ROLL_LEG_IMBALANCE_ACTION this NEVER auto-corrects: freeze the position
    for review and surface it as an alert. NO execution is written — the operator
    reconciles the true broker state and resolves the freeze. (R3.)"""
    summary = (f"roll order {order_id}: leg-imbalanced fill — buy-to-close filled "
               f"{close_qty}, sell-to-open filled {open_qty}. Position frozen; "
               f"reconcile against the broker before trading it.")
    state = log.load_state()
    position = log.find_position(state, ticker)
    if position is not None:
        position["needs_review"] = True
        review = dict(position.get("review") or {})
        review["since"] = log.utcnow()
        review["summary"] = summary
        classes = set(review.get("classifications") or [])
        classes.add("ROLL_LEG_IMBALANCE")
        review["classifications"] = sorted(classes)
        review["leg_imbalance"] = {
            "order_id": str(order_id), "close_filled": close_qty,
            "open_filled": open_qty, "at": log.utcnow(),
        }
        position["review"] = review
        log.save_state(state)
    log.logger.error("LEG IMBALANCE on roll %s (%s): close=%s open=%s — froze position, "
                     "no execution written", order_id, ticker, close_qty, open_qty)
    return {"order_id": str(order_id), "status": "leg_imbalance", "frozen": True,
            "ticker": ticker, "close_filled": close_qty, "open_filled": open_qty,
            "summary": summary}


def _roll_reference_net_mid(payload, close_ps, open_ps) -> float | None:
    """The reference NET mid for the roll = mid(new short) − mid(old short),
    captured at ticket time (R1/R5). Prefer an explicitly-carried value (the
    live placement limit); else derive from the per-leg mids."""
    ref = payload.get("reference_net_mid")
    if ref is not None:
        try:
            return round(float(ref), 4)
        except (TypeError, ValueError):
            pass
    if open_ps is None and close_ps is None:
        return None
    return round(float(open_ps or 0) - float(close_ps or 0), 4)


def _commit_roll(payload, ticker, contracts, stock_price, mode, price_source,
                 *, roll_group_id=None, alloc_method="mid"):
    from_strike = payload.get("from_strike", payload.get("strike"))
    to_strike = payload.get("to_strike")
    contracts = int(contracts or 0)

    close_payload = {
        "ticker": ticker, "strike": from_strike, "contracts": contracts,
        "close_price_per_share": payload.get("close_price_per_share"),
        "close_total": payload.get("close_total"),
        "stock_price": stock_price,
        "extrinsic_sold": payload.get("extrinsic_sold"),
    }
    close_exec, close_apply = _close_short(close_payload, ticker, from_strike, contracts, stock_price)

    sell_payload = {
        "ticker": ticker, "strike": to_strike, "contracts": contracts,
        "premium_per_share": payload.get("premium_per_share"),
        "premium_total": payload.get("premium_total"),
        "stock_price": stock_price,
        "expiration": payload.get("to_expiration"),
        "dte": payload.get("to_dte", payload.get("dte", 5)),
    }
    sell_exec, sell_apply = _sell_short(sell_payload, ticker, to_strike, contracts, stock_price)

    # Link the pair for the roll ledger (derived in recompute_derived). The
    # spec's roll_group_id and the ledger's roll_id are the SAME value — one
    # logical roll — so partial fills of one order all share it. A live/legged
    # commit passes it in; a fresh paper roll mints the next one.
    roll_id = roll_group_id or _next_roll_id(log.load_state())
    reason = _roll_reason(payload)
    # Net reference mid + realized net for the slippage feedback (R5). The realized
    # net is computed from the prices actually booked onto each leg.
    ref_net_mid = _roll_reference_net_mid(
        payload, payload.get("close_price_per_share"), payload.get("premium_per_share"))
    net_fill = round(float(close_exec["close_price_per_share"]) * -1
                     + float(sell_exec["premium_per_share"]), 4)
    for leg_exec, leg in ((close_exec, "close"), (sell_exec, "open")):
        leg_exec["mode"] = mode
        leg_exec["price_source"] = price_source
        leg_exec["roll_leg"] = leg
        leg_exec["roll_id"] = roll_id
        # roll_group_id is the spec's name for the roll linkage; stamped equal to
        # roll_id so the API/UI and the ledger agree and never drift.
        leg_exec["roll_group_id"] = roll_id
        leg_exec["roll_reason"] = reason
        # How the net fill was split across the legs (R2): "mid" (paper, booked at
        # the quoted mids), "broker_per_leg" (Schwab's reported per-leg fills), or
        # "proportional_to_mid" (net split by reference mids when per-leg absent).
        leg_exec["roll_alloc_method"] = alloc_method
        leg_exec["roll_reference_net_mid"] = ref_net_mid
        leg_exec["roll_net_fill"] = net_fill

    _stamp_source_rec(close_exec, payload)
    _stamp_source_rec(sell_exec, payload)
    stored_close = log.append_execution(close_exec)
    stored_sell = log.append_execution(sell_exec)

    # Apply both position mutations onto the freshly written state, once.
    state = log.load_state()
    position = _ensure_position(state, ticker)
    close_apply(position)
    sell_apply(position)
    log.recompute_derived(state)
    log.save_state(state)

    close_total = float(stored_close.get("close_total") or 0)
    new_total = float(stored_sell.get("premium_total") or 0)
    return {
        "success": True,
        "status": "filled",
        "execution_id": stored_sell["id"],
        "close_execution_id": stored_close["id"],
        "timestamp": stored_sell["date"],
        "mode": mode,
        "captured_price": stock_price,
        "net_credit": round(new_total - close_total, 2),
        "roll_group_id": roll_id,
        "alloc_method": alloc_method,
        "executions": [stored_close, stored_sell],
    }


# ---------------------------------------------------------------------------
# Atomic open (buy LEAP + sell weekly short on one ticket — a diagonal entry)
# ---------------------------------------------------------------------------
def _next_open_id(state) -> str:
    n = len({e.get("open_id") for e in state.get("executions", []) if e.get("open_id")})
    return f"open_{n + 1:03d}"


def _open_position_atomic(payload, ticker, contracts, stock_price, mode, price_source):
    """Open a position on ONE ticket: buy-to-open the deep-ITM LEAP +
    sell-to-open this week's short (a diagonal), a single net debit, pending ->
    poll -> commit/auto-cancel. The long and its cover go on together — no
    legging risk, and the juice starts the day the position is opened. Paper mode
    books both legs immediately.

    Works for a fresh entry AND as a one-ticket add-on to a ticker that already
    holds a LEAP: the buy leg reuses _buy_leap's apply, which scales in when the
    strike/expiration matches an existing leg ("merge") or stacks a new tranche
    beside it ("add"). The short is sold against the enlarged long the same way."""
    leap_strike = payload.get("strike")
    short_strike = payload.get("short_strike")
    if leap_strike is None or short_strike is None:
        raise ValueError("open_position_atomic requires the LEAP strike and short_strike")
    if mode == "live" and schwab_api.configured():
        return _place_live_open(payload, ticker, contracts, stock_price, price_source)
    return _commit_open(payload, ticker, contracts, stock_price, mode, price_source)


def _commit_open(payload, ticker, contracts, stock_price, mode, price_source):
    """Book both entry legs: buy_leap (buy-to-open) + sell_short (sell-to-open),
    linked by a shared open_id. Shared by the paper path and the live fill-
    confirmation path."""
    leap_strike = payload.get("strike")
    short_strike = payload.get("short_strike")
    leap_exec, leap_apply = _buy_leap(payload, ticker, leap_strike, contracts, stock_price)

    short_payload = {
        "ticker": ticker, "strike": short_strike, "contracts": contracts,
        "premium_per_share": payload.get("short_premium_per_share"),
        "premium_total": payload.get("short_premium_total"),
        "stock_price": stock_price,
        "expiration": payload.get("short_expiration"),
        "dte": payload.get("short_dte", 5),
    }
    short_exec, short_apply = _sell_short(short_payload, ticker, short_strike, contracts, stock_price)

    open_id = _next_open_id(log.load_state())
    for e, leg in ((leap_exec, "leap"), (short_exec, "short")):
        e["mode"] = mode
        e["price_source"] = price_source
        e["open_id"] = open_id
        e["open_leg"] = leg

    # Establish the long, then sell the cover. Apply both mutations once on fresh
    # state (leap sets up the position; short appends its call).
    _stamp_source_rec(leap_exec, payload)
    _stamp_source_rec(short_exec, payload)
    stored_leap = log.append_execution(leap_exec)
    stored_short = log.append_execution(short_exec)

    state = log.load_state()
    position = _ensure_position(state, ticker)
    leap_apply(position)
    short_apply(position)
    log.recompute_derived(state)
    log.save_state(state)

    debit = float(stored_leap.get("execution_total") or 0)
    credit = float(stored_short.get("premium_total") or 0)
    return {
        "success": True,
        "status": "filled",
        "open_id": open_id,
        "execution_id": stored_leap["id"],
        "short_execution_id": stored_short["id"],
        "timestamp": stored_leap["date"],
        "mode": mode,
        "captured_price": stock_price,
        "net_debit": round(debit - credit, 2),
        "executions": [stored_leap, stored_short],
    }


def _place_live_open(payload, ticker, contracts, stock_price, price_source):
    """Transmit the entry as ONE two-leg NET_DEBIT diagonal: buy-to-open the LEAP
    + sell-to-open the weekly short on one ticket, so it can't leg out. Committed
    on fill via the same pending -> poll lifecycle as the atomic exit."""
    _assert_transmit_allowed("open_position_atomic")
    _guard_resubmit(ticker, "open_position_atomic")
    leap_strike = payload.get("strike")
    short_strike = payload.get("short_strike")
    client = data_handler.client()
    account_hash = client.primary_account_hash()

    leap_symbol = payload.get("option_symbol") or (
        schwab_api.occ_option_symbol(ticker, payload.get("expiration"), leap_strike, call=True)
        if payload.get("expiration") else None)
    short_symbol = payload.get("short_option_symbol") or (
        schwab_api.occ_option_symbol(ticker, payload.get("short_expiration"), short_strike, call=True)
        if payload.get("short_expiration") else None)
    if not leap_symbol or not short_symbol:
        raise ValueError("live open needs option_symbol/expiration for both the LEAP and the short")

    leap_ps = float(payload.get("execution_price") or 0) / 100.0  # per-contract -> per-share
    short_ps = float(payload.get("short_premium_per_share") or 0)
    # Entry is a net DEBIT (the LEAP costs more than the short credit): the short
    # credit minus the LEAP debit is negative, which build_net_order reads as a
    # NET_DEBIT at that magnitude.
    net_ps = round(short_ps - leap_ps, 2)
    legs = [("BUY_TO_OPEN", leap_symbol, contracts), ("SELL_TO_OPEN", short_symbol, contracts)]
    # Entry routes strategy type / duration through its provenance-tagged config
    # (LIVE_VERIFY: DIAGONAL vs CUSTOM), so entry and roll can't silently disagree.
    order = schwab_api.build_net_order(
        legs, net_ps,
        complex_strategy_type=config.ENTRY_COMPLEX_STRATEGY_TYPE,
        duration=config.ENTRY_ORDER_DURATION)
    placed = client.place_order(account_hash, order)
    order_id = placed.get("orderId")
    if not order_id:
        raise schwab_api.SchwabError("Schwab accepted the open but returned no order id")
    log.save_pending_order(order_id, {
        "kind": "open", "payload": payload, "ticker": ticker, "action": "open_position_atomic",
        "contracts": contracts, "stock_price": stock_price, "price_source": price_source,
        "account_hash": account_hash, "leap_symbol": leap_symbol, "short_symbol": short_symbol,
        "net_limit": net_ps, "placed_at": log.utcnow(),
    })
    _record_placement(ticker, "open_position_atomic", order_id, net_limit=net_ps)
    return {"success": True, "status": "working", "order_id": str(order_id), "mode": "live",
            "option_symbols": [leap_symbol, short_symbol], "net_limit": net_ps}


def _commit_open_from_pending(rec: dict, order: dict) -> dict:
    """Commit a filled atomic open, overlaying the real per-leg fills onto the
    LEAP's execution_price and the short's premium before booking both legs."""
    payload = dict(rec.get("payload") or {})
    fills = _leg_fills(order, [rec.get("leap_symbol", ""), rec.get("short_symbol", "")])
    leap_fill = fills.get((rec.get("leap_symbol") or "").strip())
    short_fill = fills.get((rec.get("short_symbol") or "").strip())
    if leap_fill is not None:
        payload["execution_price"] = leap_fill * 100  # per-contract dollars
    if short_fill is not None:
        payload["short_premium_per_share"] = short_fill
    return _commit_open(payload, rec["ticker"], int(rec["contracts"]),
                        rec.get("stock_price"), "live", rec.get("price_source", "schwab"))


# ---------------------------------------------------------------------------
# Atomic exit (close LEAP + short on one ticket) and atomic LEAP roll
# ---------------------------------------------------------------------------
def _next_exit_id(state) -> str:
    n = len({e.get("exit_id") for e in state.get("executions", []) if e.get("exit_id")})
    return f"exit_{n + 1:03d}"


def _next_leap_roll_id(state) -> str:
    n = len({e.get("leap_roll_id") for e in state.get("executions", []) if e.get("leap_roll_id")})
    return f"leaproll_{n + 1:03d}"


def _leg_fills(order: dict, symbols: list[str]) -> dict:
    """symbol -> average per-share fill price from a filled multi-leg order,
    matched by legId -> orderLegCollection symbol. Missing legs are absent."""
    leg_symbol = {}
    for i, leg in enumerate(order.get("orderLegCollection") or [], start=1):
        sym = ((leg.get("instrument") or {}).get("symbol") or "").strip()
        leg_symbol[leg.get("legId") or i] = sym
    wanted = {s.strip() for s in symbols if s}
    fills: dict[str, float] = {}
    try:
        for act in order.get("orderActivityCollection", []) or []:
            for leg in act.get("executionLegs", []) or []:
                price = leg.get("price")
                if price is None:
                    continue
                sym = leg_symbol.get(leg.get("legId"))
                if sym in wanted:
                    fills[sym] = float(price)
    except (TypeError, ValueError):
        pass
    return fills


def _leap_close_per_contract(leap: dict, payload: dict) -> float:
    """Per-contract sell-to-close price for the LEAP: supplied leap_close_price,
    else the stored per-position mark (current_bid) split back per contract."""
    supplied = payload.get("leap_close_price")
    if supplied is not None:
        return float(supplied)
    contracts = int(leap.get("contracts") or 0)
    cur = leap.get("current_bid")
    return float(cur) / contracts if (cur is not None and contracts) else 0.0


def _build_exit_legs(position, payload, stock_price):
    """(leap_close_exec, leap_apply, [(short_exec, short_apply)...], net_per_share,
    symbols) for an atomic exit. Prices come from supplied values / stored marks;
    the live path overlays real per-leg fills before committing."""
    leap = position.get("leap") or {}
    n_leap = int(leap.get("contracts") or 0)
    leap_strike = leap.get("strike")
    leap_pc = _leap_close_per_contract(leap, payload)
    leap_payload = {
        "ticker": position["ticker"], "strike": leap_strike, "contracts": n_leap,
        "close_price": leap_pc, "stock_price": stock_price,
        "cost_basis": payload.get("cost_basis", leap.get("cost_basis")),
        "exit_reason": payload.get("exit_reason"),
        "exit_note": payload.get("exit_note"),
    }
    leap_exec, leap_apply = _close_leap(leap_payload, position["ticker"], leap_strike, n_leap, stock_price)

    shorts = []
    short_buyback_total = 0.0
    for sc in position.get("short_calls") or []:
        n_sc = int(sc.get("contracts") or 0)
        buyback_ps = sc.get("current_bid")
        sp = {
            "ticker": position["ticker"], "strike": sc.get("strike"), "contracts": n_sc,
            "close_price_per_share": buyback_ps, "stock_price": stock_price,
            "extrinsic_sold": sc.get("entry_extrinsic_per_share"),
        }
        e, ap = _close_short(sp, position["ticker"], sc.get("strike"), n_sc, stock_price)
        shorts.append((e, ap))
        short_buyback_total += float(e.get("close_total") or 0)

    leap_close_total = float(leap_exec.get("close_total") or 0)
    # Net = LEAP sale proceeds (credit) minus short buyback (debit), per LEAP share.
    net_total = leap_close_total - short_buyback_total
    net_ps = round(net_total / (n_leap * 100), 2) if n_leap else 0.0
    return leap_exec, leap_apply, shorts, net_ps


def _commit_exit(payload, ticker, stock_price, mode, price_source):
    state = log.load_state()
    position = log.find_position(state, ticker)
    if not position or not (position.get("leap") or {}):
        raise ValueError(f"{ticker} has no open LEAP to close")
    leap_exec, leap_apply, shorts, _net = _build_exit_legs(position, payload, stock_price)

    exit_id = _next_exit_id(state)
    for e in [leap_exec] + [se for se, _ in shorts]:
        e["mode"] = mode
        e["price_source"] = price_source
        e["exit_id"] = exit_id
    leap_exec["exit_leg"] = "leap"
    for se, _ in shorts:
        se["exit_leg"] = "short"

    # Append shorts (buy-to-close) then the LEAP (sell-to-close); on the immutable
    # log order is cosmetic, but this mirrors "cover the short, then release the
    # long". Apply all mutations on the freshly written state, once.
    for se, _ in shorts:
        _stamp_source_rec(se, payload)
    _stamp_source_rec(leap_exec, payload)
    stored = [log.append_execution(se) for se, _ in shorts]
    stored_leap = log.append_execution(leap_exec)

    state = log.load_state()
    position = _ensure_position(state, ticker)
    for _, ap in shorts:
        ap(position)
    leap_apply(position)
    log.recompute_derived(state)
    log.save_state(state)

    return {
        "success": True,
        "status": "filled",
        "exit_id": exit_id,
        "execution_id": stored_leap["id"],
        "short_execution_ids": [s["id"] for s in stored],
        "timestamp": stored_leap["date"],
        "mode": mode,
        "captured_price": stock_price,
        "realized_pnl": stored_leap.get("realized_pnl"),
        "executions": stored + [stored_leap],
    }


def _place_live_exit(payload, ticker, stock_price, price_source):
    """Transmit the exit as ONE multi-leg NET order (sell-to-close the LEAP +
    buy-to-close every open short) and park it pending; commit on fill."""
    _assert_transmit_allowed("close_position_atomic")
    state = log.load_state()
    position = log.find_position(state, ticker)
    if not position or not (position.get("leap") or {}):
        raise ValueError(f"{ticker} has no open LEAP to close")
    leap = position["leap"]
    _le, _la, _shorts, net_ps = _build_exit_legs(position, payload, stock_price)

    client = data_handler.client()
    account_hash = client.primary_account_hash()

    def _sym(prefix, strike, default_exp_key):
        sym = payload.get(f"{prefix}_option_symbol")
        if sym:
            return sym
        expiration = payload.get(default_exp_key)
        if not expiration:
            raise ValueError(f"live exit needs {prefix}_option_symbol or {default_exp_key}")
        return schwab_api.occ_option_symbol(ticker, expiration, strike, call=True)

    leap_symbol = _sym("leap", leap.get("strike"), "leap_expiration")
    legs = [("SELL_TO_CLOSE", leap_symbol, int(leap.get("contracts") or 0))]
    short_symbols = []
    overrides = payload.get("short_option_symbols") or {}
    for sc in position.get("short_calls") or []:
        s = overrides.get(str(sc.get("strike")))
        if not s and sc.get("expiration"):
            s = schwab_api.occ_option_symbol(ticker, sc.get("expiration"), sc.get("strike"), call=True)
        if not s:
            raise ValueError(f"live exit needs an option symbol/expiration for short {sc.get('strike')}")
        legs.append(("BUY_TO_CLOSE", s, int(sc.get("contracts") or 0)))
        short_symbols.append(s)

    order = schwab_api.build_net_order(legs, net_ps)
    placed = client.place_order(account_hash, order)
    order_id = placed.get("orderId")
    if not order_id:
        raise schwab_api.SchwabError("Schwab accepted the exit but returned no order id")
    log.save_pending_order(order_id, {
        "kind": "exit", "payload": payload, "ticker": ticker, "action": "close_position_atomic",
        "stock_price": stock_price, "price_source": price_source, "account_hash": account_hash,
        "leap_symbol": leap_symbol, "short_symbols": short_symbols,
        "net_limit": net_ps, "placed_at": log.utcnow(),
    })
    return {"success": True, "status": "working", "order_id": str(order_id), "mode": "live",
            "option_symbols": [leap_symbol] + short_symbols, "net_limit": net_ps}


def _commit_exit_from_pending(rec: dict, order: dict) -> dict:
    """Commit a filled atomic exit, overlaying real per-leg fills onto the
    payload marks (leap_close_price + per-short buyback)."""
    payload = dict(rec.get("payload") or {})
    fills = _leg_fills(order, [rec.get("leap_symbol", "")] + list(rec.get("short_symbols") or []))
    leap_fill = fills.get((rec.get("leap_symbol") or "").strip())
    if leap_fill is not None:
        payload["leap_close_price"] = leap_fill * 100  # per-contract dollars
    # Overlay each short's real buyback fill onto its stored mark so _commit_exit
    # books the short at the actual fill. short_symbols align with short_calls.
    state = log.load_state()
    position = log.find_position(state, rec["ticker"])
    if position:
        for sc, sym in zip(position.get("short_calls") or [], rec.get("short_symbols") or []):
            f = fills.get(sym.strip())
            if f is not None:
                sc["current_bid"] = f
        log.save_state(state)
    return _close_position_atomic(payload, rec["ticker"], rec.get("stock_price"),
                                  "live", rec.get("price_source", "schwab"), _committed=True)


def _close_position_atomic(payload, ticker, stock_price, mode, price_source, _committed=False):
    """Exit a full position on ONE ticket: sell-to-close the LEAP + buy-to-close
    the open short(s), single net price, pending -> poll -> commit/auto-cancel —
    reusing the same two-leg machinery as an atomic short roll. This is the
    default action for a kill-switch / circuit-breaker exit: legging out is most
    expensive exactly when those fire. Paper mode books both legs immediately."""
    state = log.load_state()
    position = log.find_position(state, ticker)
    if not position or not (position.get("leap") or {}):
        raise ValueError(f"{ticker} has no open LEAP to close")
    if mode == "live" and schwab_api.configured() and not _committed:
        return _place_live_exit(payload, ticker, stock_price, price_source)
    return _commit_exit(payload, ticker, stock_price, mode, price_source)


def _roll_leap(payload, ticker, stock_price, mode, price_source):
    """Roll the LONG leg: sell-to-close the old LEAP + buy-to-open a fresh one,
    recorded as close_leap + buy_leap executions linked by a shared leap_roll_id
    so the derived layer carries the position's payback continuity across the
    roll (juice carries, the new extrinsic is ADDED to the target) rather than
    treating it as an exit + re-entry. Reserve is checked like an entry: a roll
    debit that breaches the 2xATR reserve needs an override_reason.

    Paper mode books both legs immediately at supplied/estimated prices. Live
    mode transmits ONE two-leg NET order (no legging risk)."""
    import leap_policy

    state = log.load_state()
    position = log.find_position(state, ticker)
    if not position or not (position.get("leap") or {}):
        raise ValueError(f"{ticker} has no open LEAP to roll")

    # Reserve check (blocking unless overridden), mirroring the entry gate.
    est = leap_policy.roll_cost_estimate(ticker, position=position, state=state)
    if est.get("reserve_ok") is False and not (payload.get("override_reason") or "").strip():
        raise ValueError(
            f"LEAP roll would breach the 2xATR cash reserve "
            f"(debit ${est.get('net_debit')}, free after ${est.get('free_cash_after')} "
            f"< reserve ${est.get('reserve_required')}). Pass override_reason to roll anyway.")

    if mode == "live" and schwab_api.configured():
        return _place_live_leap_roll(payload, ticker, position, stock_price, price_source, est)
    return _commit_leap_roll(payload, ticker, position, stock_price, mode, price_source, est)


def _commit_leap_roll(payload, ticker, position, stock_price, mode, price_source, est):
    state = log.load_state()
    position = log.find_position(state, ticker)
    old_leap = position["leap"]
    n = int(old_leap.get("contracts") or 0)
    leap_roll_id = _next_leap_roll_id(state)

    # Close the old LEAP.
    close_pc = _leap_close_per_contract(old_leap, payload)
    import exit_reasons
    close_payload = {"ticker": ticker, "strike": old_leap.get("strike"), "contracts": n,
                     "close_price": close_pc, "stock_price": stock_price,
                     "cost_basis": old_leap.get("cost_basis"),
                     # A LEAP roll is a mechanical continuation, not a graded exit;
                     # the derivation still treats it as a cycle boundary, so it
                     # gets its own coded reason (no operator note needed).
                     "exit_reason": exit_reasons.ExitReason.LEAP_ROLL}
    close_exec, close_apply = _close_leap(close_payload, ticker, old_leap.get("strike"), n, stock_price)

    # Open the replacement LEAP.
    new_strike = payload.get("to_strike", (est.get("new_leap") or {}).get("strike"))
    new_pc = payload.get("execution_price")
    if new_pc is None:
        new_pc = ((est.get("new_leap") or {}).get("est_cost") or 0) / n * 100 if n else 0
    buy_payload = {"ticker": ticker, "strike": new_strike, "contracts": n,
                   "execution_price": new_pc, "stock_price": stock_price,
                   "dte": payload.get("to_dte", config.LEAP_TARGET_DTE),
                   "expiration": payload.get("to_expiration"),
                   "circuit_breaker_price": (position.get("circuit_breaker") or {}).get("price")}
    buy_exec, buy_apply = _buy_leap(buy_payload, ticker, new_strike, n, stock_price)

    for e, leg in ((close_exec, "close"), (buy_exec, "open")):
        e["mode"] = mode
        e["price_source"] = price_source
        e["leap_roll_id"] = leap_roll_id
        e["leap_roll_leg"] = leg
    if payload.get("override_reason"):
        buy_exec["override"] = {"reason": str(payload["override_reason"]).strip(),
                                "failed_checks": ["cash_reserve"] if est.get("reserve_ok") is False else []}

    stored_close = log.append_execution(close_exec)
    stored_buy = log.append_execution(buy_exec)

    state = log.load_state()
    position = _ensure_position(state, ticker)
    close_apply(position)
    buy_apply(position)
    log.recompute_derived(state)
    log.save_state(state)

    return {
        "success": True,
        "status": "filled",
        "leap_roll_id": leap_roll_id,
        "close_execution_id": stored_close["id"],
        "execution_id": stored_buy["id"],
        "timestamp": stored_buy["date"],
        "mode": mode,
        "net_debit": round(float(stored_buy.get("execution_total") or 0)
                           - float(stored_close.get("close_total") or 0), 2),
        "executions": [stored_close, stored_buy],
    }


def _place_live_leap_roll(payload, ticker, position, stock_price, price_source, est):
    """Transmit a LEAP roll as ONE two-leg NET order: sell-to-close the old LEAP
    + buy-to-open the new one. Committed on fill via the same lifecycle."""
    _assert_transmit_allowed("roll_leap")
    leap = position["leap"]
    n = int(leap.get("contracts") or 0)
    new_strike = payload.get("to_strike", (est.get("new_leap") or {}).get("strike"))
    client = data_handler.client()
    account_hash = client.primary_account_hash()

    close_symbol = payload.get("from_option_symbol") or (
        schwab_api.occ_option_symbol(ticker, payload.get("from_expiration"), leap.get("strike"), call=True)
        if payload.get("from_expiration") else None)
    open_symbol = payload.get("to_option_symbol") or (
        schwab_api.occ_option_symbol(ticker, payload.get("to_expiration"), new_strike, call=True)
        if payload.get("to_expiration") else None)
    if not close_symbol or not open_symbol:
        raise ValueError("live LEAP roll needs from/to option_symbol or expiration to build the contracts")

    net_ps = round(-float(est.get("net_debit") or 0) / (n * 100), 2) if n else 0.0
    legs = [("SELL_TO_CLOSE", close_symbol, n), ("BUY_TO_OPEN", open_symbol, n)]
    order = schwab_api.build_net_order(legs, net_ps)
    placed = client.place_order(account_hash, order)
    order_id = placed.get("orderId")
    if not order_id:
        raise schwab_api.SchwabError("Schwab accepted the LEAP roll but returned no order id")
    log.save_pending_order(order_id, {
        "kind": "roll_leap", "payload": payload, "ticker": ticker, "action": "roll_leap",
        "stock_price": stock_price, "price_source": price_source, "account_hash": account_hash,
        "close_option_symbol": close_symbol, "open_option_symbol": open_symbol,
        "net_limit": net_ps, "placed_at": log.utcnow(),
    })
    return {"success": True, "status": "working", "order_id": str(order_id), "mode": "live",
            "option_symbols": [close_symbol, open_symbol], "net_limit": net_ps}


def _commit_leap_roll_from_pending(rec: dict, order: dict) -> dict:
    payload = dict(rec.get("payload") or {})
    fills = _leg_fills(order, [rec.get("close_option_symbol", ""), rec.get("open_option_symbol", "")])
    close_fill = fills.get((rec.get("close_option_symbol") or "").strip())
    open_fill = fills.get((rec.get("open_option_symbol") or "").strip())
    if close_fill is not None:
        payload["leap_close_price"] = close_fill * 100
    if open_fill is not None:
        payload["execution_price"] = open_fill * 100
    return _roll_leap(payload, rec["ticker"], rec.get("stock_price"), "logged",
                      rec.get("price_source", "schwab"))


def defend_recommendation(ticker: str) -> dict:
    """Defensive roll-down for a breached short (underlying < short strike):
    new strike from the regime x posture table (strike_policy — the deeper of
    an ATR-distance strike and an ITM% floor), same or next weekly expiry, with
    the estimated net credit/debit, the new short's extrinsic, and the effect
    on effective cost basis. Prices come from the stored short mark + a
    Black-Scholes estimate at trailing realized vol, so this works in demo /
    off-hours; the staged roll itself re-prices from the live chain."""
    import screening
    import strike_policy

    ticker = ticker.upper()
    state = log.load_state()
    pos = log.find_position(state, ticker)
    if not pos:
        return {"ticker": ticker, "error": "no position"}
    df = data_handler.get_daily(ticker)
    close = indicators.last(df)
    atr_val = indicators.atr(df) if df is not None else None
    hv = indicators.hist_vol(df) if df is not None else None
    if close is None or atr_val is None:
        return {"ticker": ticker, "error": "insufficient data"}

    # A short is breached only when BOTH the last daily close and the live price
    # sit below the strike: close-confirmed (no intraday whipsaw), but cleared
    # once the stock recovers above the strike intraday. The current price drives
    # the roll-down suggestion, so use the live quote when we have one.
    live = data_handler.live_price(ticker)
    price = live if live is not None else close

    breached = [sc for sc in pos.get("short_calls", [])
                if sc.get("strike") is not None
                and close < float(sc["strike"]) and price < float(sc["strike"])]
    if not breached:
        return {"ticker": ticker, "breached": False,
                "stock_price": round(price, 2), "last_close": round(close, 2)}
    sc = min(breached, key=lambda s: s.get("dte") if s.get("dte") is not None else 1e9)

    regime = screening.regime().get("status", "yellow")
    sp = strike_policy.suggest_strike(price, atr_val, regime)
    atr_mult, itm_pct, posture = sp["atr_mult"], sp["itm_pct"], sp["posture"]
    new_strike = sp["strike"]

    contracts = int(sc.get("contracts") or 0)
    dte = sc.get("dte")
    roll_dte = int(dte) if dte else 5  # same week when it has time, else next weekly
    buyback = sc.get("current_bid")
    new_premium = new_extrinsic = None
    if hv:
        bs = indicators._bs_call_price(price, new_strike, max(roll_dte, 1) / 365.0,
                                       config.RISK_FREE_RATE, hv / 100.0)
        new_premium = round(bs, 2)
        new_extrinsic = round(max(bs - max(price - new_strike, 0.0), 0.0), 2)
    net = (round((new_premium - float(buyback)) * contracts * 100, 2)
           if (new_premium is not None and buyback is not None) else None)
    # Whipsaw circuit breaker: if this position has already rolled down too many
    # times / bled too much drag, the correct move is to EXIT, not defend again —
    # surface that on the very recommendation the operator opens to roll.
    import position_manager
    whipsaw = position_manager.whipsaw_status(
        pos, (state.get("roll_ledger") or {}).get("rolls", []))
    return {
        "ticker": ticker,
        "breached": True,
        "stock_price": round(price, 2),
        "last_close": round(close, 2),
        "atr": round(atr_val, 2),
        "regime": regime,
        "atr_mult": atr_mult,
        "itm_pct": itm_pct,
        "posture": posture,
        "whipsaw": whipsaw,
        "current_short": {"strike": sc.get("strike"), "contracts": contracts,
                          "dte": dte, "expiration": sc.get("expiration"),
                          "buyback_per_share": buyback},
        "recommended_strike": new_strike,
        "recommended_dte": roll_dte,
        "new_premium_per_share": new_premium,
        "new_extrinsic_per_share": new_extrinsic,
        "net_total": net,
        # A net credit lowers the effective LEAP cost basis; a debit raises it.
        "cost_basis_effect": -net if net is not None else None,
        "source": "estimate",
    }


def roll_suggestion(ticker: str) -> dict:
    """Next weekly short strike from the regime x posture table (strike_policy)."""
    import screening
    import strike_policy

    df = data_handler.get_daily(ticker)
    price = indicators.last(df)
    atr_val = indicators.atr(df)
    if price is None or atr_val is None:
        return {"ticker": ticker, "error": "insufficient data"}
    regime = screening.regime().get("status", "yellow")
    sp = strike_policy.suggest_strike(price, atr_val, regime)
    return {
        "ticker": ticker,
        "stock_price": round(price, 2),
        "atr": round(atr_val, 2),
        "regime": regime,
        "atr_mult": sp["atr_mult"],
        "itm_pct": sp["itm_pct"],
        "posture": sp["posture"],
        "suggested_strike": sp["strike"],
    }
