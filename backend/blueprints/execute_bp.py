"""Execute (order placement + order-lifecycle status) (8 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import executor
import logging_handler as log
import position_manager
import schwab_api

execute_bp = Blueprint("execute", __name__)


@execute_bp.route("/api/execute", methods=["POST"])
def api_execute():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(executor.execute(payload))
    except executor.PositionFrozenError as e:
        # 409 (distinct from the 400 gate-rejection): the position is frozen for
        # reconciliation review. The diff summary rides in the body. Closing
        # actions are never rejected here, so the operator can still exit.
        return jsonify({"error": str(e), "frozen": True, "ticker": e.ticker,
                        "review": e.review}), 409
    except executor.ResubmitLockedError as e:
        # 409: the resubmission gate blocked a new live order for this position
        # intent — a prior order isn't confirmed terminal at the broker yet (or the
        # per-session attempt cap is hit). In addition to the freeze/gate/kill-switch.
        return jsonify({"error": str(e), "resubmit_locked": True,
                        "intent": e.intent_key, "reason": e.reason}), 409
    except executor.ExecutionWindowError as e:
        # 409: the market-settle execution gate deferred the order (settle window /
        # close blackout / off-hours). The UI stages it as PENDING_SETTLE and shows
        # the countdown to executable_at; the alert already fired.
        return jsonify({"error": str(e), "execution_deferred": True,
                        "reason": e.reason, "ticker": e.ticker,
                        "action": e.gate_action,
                        "executable_at": (e.executable_at.isoformat()
                                          if e.executable_at else None)}), 409
    except executor.SpreadAckRequiredError as e:
        # 409: spread abnormally wide vs the trailing baseline — the operator must
        # acknowledge the estimated excess slippage (resend with spread_ack: true).
        return jsonify({"error": str(e), "spread_ack_required": True,
                        "ticker": e.ticker, "current_spread": e.current_spread,
                        "baseline_spread": e.baseline_spread,
                        "est_excess_slippage_usd": e.est_excess_slippage_usd}), 409
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@execute_bp.route("/api/order-status")
def api_order_status():
    order_id = request.args.get("order_id", "")
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    try:
        return jsonify(executor.order_status(order_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@execute_bp.route("/api/schwab/rate-limit")
def api_schwab_rate_limit():
    """The process-wide Schwab pacing in effect (requests/minute, tokens left,
    any 429 pause) — what to look at when reads feel slow."""
    return jsonify(schwab_api.rate_limit_status())


@execute_bp.route("/api/ticker-strip")
def api_ticker_strip():
    """The chrome's per-position readout (spot + distance to each short strike)
    for the active book. Thin and polled from every tab — see
    position_manager.ticker_strip."""
    try:
        state = log.load_state()
        return jsonify({"as_of": log.utcnow(),
                        "positions": position_manager.ticker_strip(state)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@execute_bp.route("/api/orders/pending")
def api_orders_pending():
    """The active book's pending (placed, not yet settled) orders, with the
    stock price captured at order time — what a re-poll would book a fill at."""
    try:
        return jsonify({"orders": executor.list_pending_orders()})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@execute_bp.route("/api/orders/repoll", methods=["POST"])
def api_orders_repoll():
    """Re-poll every pending order against Schwab now (the startup sweep, on
    demand): a fill that happened at the broker but never got booked is committed
    with its original captured economics; terminal orders are cleared."""
    try:
        return jsonify(executor.repoll_pending_orders())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@execute_bp.route("/api/order-cancel", methods=["POST"])
def api_order_cancel():
    payload = request.get_json(silent=True) or {}
    order_id = payload.get("order_id", "")
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    try:
        return jsonify(executor.cancel_order(order_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@execute_bp.route("/api/order-submission-status")
def api_order_submission_status():
    """MANUAL status check for a client_order_ref (incident hotfix, D2/D4). Resolves
    an order whose broker outcome isn't yet confirmed — recovers a missing orderId by
    recent-orders match and syncs the durable record to the broker truth. Never
    auto-retries the submission; the operator drives it. Reading it never lies:
    UNKNOWN stays 'confirming', a rejection carries Schwab's verbatim reason."""
    ref = request.args.get("ref", "") or request.args.get("client_order_ref", "")
    if not ref:
        return jsonify({"error": "ref is required"}), 400
    try:
        return jsonify(executor.submission_status(ref))
    except Exception as e:  # noqa: BLE001
        return _err(e)

