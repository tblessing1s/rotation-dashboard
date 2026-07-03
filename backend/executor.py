"""Execute CFM actions (buy_leap / sell_short / close_short) and auto-log them.

Every execution captures the stock price + premium at the moment of execution
and appends an immutable record to state.json, from which the theta ledger and
extrinsic-payback meters are derived. Live order transmission to Schwab is
gated behind the CFM_LIVE_TRADING env flag; with it off (the default) the action
is captured and logged against live market prices but no order is sent — the
honest paper path. Position state updates identically either way.
"""
from __future__ import annotations

import os

import config
import data_handler
import indicators
import logging_handler as log
import schwab_api
import sector_data

VALID_ACTIONS = {"buy_leap", "sell_short", "close_short", "close_leap", "roll_short",
                 "roll_leap", "close_position_atomic", "adjustment"}

# Actions REJECTED on a frozen (needs_review) position — new risk cannot be added
# to a position whose state is unverified. Closing actions (close_short,
# close_leap, close_position_atomic) are deliberately NOT here: a freeze must
# never trap the operator in a position during a kill-switch event — exiting is
# safe in either state of the world. ``adjustment`` is the resolution path, also
# allowed. See docs/reconciliation.md.
FROZEN_BLOCKED_ACTIONS = {"buy_leap", "sell_short", "roll_short", "roll_leap"}


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

# Why a roll happened — the whipsaw ledger key. Unrecognized values fall back to
# "scheduled" so the ledger enum stays clean for later calibration.
ROLL_REASONS = {"scheduled", "75%-rule", "defend", "earnings", "kill-switch-exit"}

# Why a cycle ended — logged on the close_leap execution, carried onto the
# derived cycle record. Unrecognized values fall back to "discretionary".
EXIT_REASONS = {"target hit", "trailing stop", "kill switch", "circuit breaker",
                "earnings", "discretionary"}

# Schwab order instruction per single-leg CFM action (all legs are calls).
INSTRUCTION = {
    "buy_leap": "BUY_TO_OPEN",
    "sell_short": "SELL_TO_OPEN",
    "close_short": "BUY_TO_CLOSE",
    "close_leap": "SELL_TO_CLOSE",
}


def live_enabled() -> bool:
    return os.environ.get("CFM_LIVE_TRADING", "").strip() in ("1", "true", "yes")


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
        "shares": {"count": 0, "cost_basis_per_share": None, "cap": config.SHARE_CAP, "pct_to_cap": 0},
        "short_calls": [],
        "kill_switch": {},
        "thesis": {"fundamentals": "", "intact": True},
        "delta_history": [],  # nightly {date, leap_delta} snapshots (delta velocity)
    }
    state["positions"].append(p)
    return p


def execute(payload: dict) -> dict:
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
    # recorded on the immutable execution (see _buy_leap).
    if action == "buy_leap":
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

    log.save_state(state)  # persist the shell position before recording the fill

    mode = "live" if live_enabled() else "logged"

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
        leap = position.get("leap") or {}
        if leap and (strike is None or _strike_eq(leap.get("strike"), strike)):
            new = int(leap.get("contracts") or 0) + qty_delta
            if new <= 0:
                position["leap"] = None
                shares = position.get("shares") or {}
                if not position.get("short_calls") and int(shares.get("count") or 0) == 0:
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
    mode = "live" if live_enabled() else "logged"
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
    execution["mode"] = "live" if live_enabled() else "logged"
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

    log.save_pending_order(order_id, {
        "payload": payload, "ticker": ticker, "action": action, "contracts": contracts,
        "strike": strike, "stock_price": stock_price, "price_source": price_source,
        "account_hash": account_hash, "option_symbol": option_symbol,
        "limit_price": limit, "placed_at": log.utcnow(),
    })
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


def _commit_roll_from_pending(rec: dict, order: dict):
    """Commit a filled two-leg roll: overlay the actual per-leg fill prices onto
    the payload (falling back to the staged estimates) and record both legs."""
    payload = dict(rec.get("payload") or {})
    close_px, open_px = _roll_leg_fills(order, rec.get("close_option_symbol", ""),
                                        rec.get("open_option_symbol", ""))
    if close_px is not None:
        payload["close_price_per_share"] = close_px
    if open_px is not None:
        payload["premium_per_share"] = open_px
    return _commit_roll(payload, rec["ticker"], int(rec["contracts"]),
                        rec.get("stock_price"), "live", rec.get("price_source", "schwab"))


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
    if raw == "FILLED":
        kind = rec.get("kind")
        if kind == "roll_short":
            result = _commit_roll_from_pending(rec, order)
        elif kind == "exit":
            result = _commit_exit_from_pending(rec, order)
        elif kind == "roll_leap":
            result = _commit_leap_roll_from_pending(rec, order)
        else:
            result = _commit_from_pending(rec, _fill_price(order))
        log.pop_pending_order(order_id)
        return {"order_id": order_id, "status": "filled", "raw_status": raw, **result}
    if raw in ("CANCELED", "REJECTED", "EXPIRED"):
        log.pop_pending_order(order_id)
        return {"order_id": order_id, "status": "rejected" if raw == "REJECTED" else "canceled",
                "raw_status": raw}
    return {"order_id": order_id, "status": "working", "raw_status": raw}


def cancel_order(order_id: str) -> dict:
    """Cancel a working order at the broker and drop the pending entry."""
    rec = log.get_pending_order(order_id)
    if rec:
        try:
            data_handler.client().cancel_order(rec["account_hash"], order_id)
        finally:
            log.pop_pending_order(order_id)
    return {"order_id": order_id, "status": "canceled"}


def _entry_snapshot(ticker: str) -> dict | None:
    """One scorecard row for the ticker, computed at entry time. Best-effort:
    offline/missing data degrades to a row of Nones, never an error."""
    try:
        from metrics import scorecard as scorecard_metrics
        rows = scorecard_metrics.scorecard([ticker]).get("results") or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001 — a snapshot must never block an entry
        return None


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
    }

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
    circuit_breaker = ({"price": round(float(cb_price), 2), "source": cb_source,
                        "set_at": log.utcnow()[:10]} if cb_price is not None else None)
    execution["circuit_breaker_price"] = circuit_breaker["price"] if circuit_breaker else None

    dividend = gate.get("dividend")
    if dividend is None:
        import dividends
        try:
            dividend = dividends.next_dividend(ticker)
        except Exception:  # noqa: BLE001 — dividend data must never block an entry
            dividend = {"ex_date": None, "amount": None, "source": "error"}

    # Scorecard verdict + metric snapshot AT ENTRY, frozen onto the immutable
    # execution — the closed-cycle record later shows what the numbers said the
    # day the trade went on (this cannot be re-derived after the fact).
    execution["entry_snapshot"] = _entry_snapshot(ticker)

    def apply(position):
        position["entry_date"] = log.utcnow()[:10]  # a new LEAP starts a new cycle
        position["circuit_breaker"] = circuit_breaker
        position["dividend"] = dividend
        position["leap"] = {
            "strike": strike, "contracts": contracts, "cost_basis": total,
            "current_bid": total, "intrinsic": round(intrinsic_per_contract * contracts, 2),
            "extrinsic": round(extrinsic_at_entry, 2),
            "entry_date": log.utcnow()[:10], "dte": payload.get("dte", config.LEAP_TARGET_DTE),
            "expiration": payload.get("expiration"),
            "extrinsic_at_entry": round(extrinsic_at_entry, 2), "extrinsic_collected_to_date": 0,
        }
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

    # Cost basis from the stored LEAP (caller may override).
    state = log.load_state()
    position = log.find_position(state, ticker)
    leap = (position or {}).get("leap") or {}
    cost_basis = payload.get("cost_basis")
    cost_basis = float(cost_basis if cost_basis is not None else leap.get("cost_basis") or 0)
    realized_pnl = round(close_total - cost_basis, 2)

    exit_reason = (payload.get("exit_reason") or "").strip()
    execution = {
        "ticker": ticker, "action": "close_leap", "strike": strike, "contracts": contracts,
        "close_price": close_per_contract, "close_total": close_total, "stock_price": stock_price,
        "cost_basis": round(cost_basis, 2), "realized_pnl": realized_pnl,
        "extrinsic_remaining": round(extrinsic_remaining, 2),
        "exit_reason": exit_reason if exit_reason in EXIT_REASONS else "discretionary",
    }

    def apply(position):
        position["leap"] = None
        shares = position.get("shares") or {}
        if not position.get("short_calls") and int(shares.get("count") or 0) == 0:
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
        position["short_calls"] = [sc for sc in position.get("short_calls", [])
                                   if sc.get("strike") != strike]
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
        return _place_live_roll(payload, ticker, contracts, stock_price, price_source)
    return _commit_roll(payload, ticker, contracts, stock_price, mode, price_source)


def _place_live_roll(payload, ticker, contracts, stock_price, price_source):
    """Transmit the roll as a single two-leg NET order and park it as pending."""
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


def _commit_roll(payload, ticker, contracts, stock_price, mode, price_source):
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

    # Link the pair for the roll ledger (derived in recompute_derived).
    roll_id = _next_roll_id(log.load_state())
    reason = _roll_reason(payload)
    for leg_exec, leg in ((close_exec, "close"), (sell_exec, "open")):
        leg_exec["mode"] = mode
        leg_exec["price_source"] = price_source
        leg_exec["roll_leg"] = leg
        leg_exec["roll_id"] = roll_id
        leg_exec["roll_reason"] = reason

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
        "executions": [stored_close, stored_sell],
    }


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
    close_payload = {"ticker": ticker, "strike": old_leap.get("strike"), "contracts": n,
                     "close_price": close_pc, "stock_price": stock_price,
                     "cost_basis": old_leap.get("cost_basis"), "exit_reason": "discretionary"}
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
    price = indicators.last(df)
    atr_val = indicators.atr(df) if df is not None else None
    hv = indicators.hist_vol(df) if df is not None else None
    if price is None or atr_val is None:
        return {"ticker": ticker, "error": "insufficient data"}

    breached = [sc for sc in pos.get("short_calls", [])
                if sc.get("strike") is not None and price < float(sc["strike"])]
    if not breached:
        return {"ticker": ticker, "breached": False, "stock_price": round(price, 2)}
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
    return {
        "ticker": ticker,
        "breached": True,
        "stock_price": round(price, 2),
        "atr": round(atr_val, 2),
        "regime": regime,
        "atr_mult": atr_mult,
        "itm_pct": itm_pct,
        "posture": posture,
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
