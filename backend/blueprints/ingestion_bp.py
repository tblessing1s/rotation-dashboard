"""Transaction ingestion (Schwab executions -> state, spec §4) (4 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import executor
import logging_handler as log

ingestion_bp = Blueprint("ingestion", __name__)


@ingestion_bp.route("/api/ingestion", methods=["GET", "POST"])
def api_ingestion():
    """GET: the last ingestion summary + open out-of-band adoption proposals.
    POST: run ingestion now (pulls Schwab transactions; dedupe by transaction id).
    Matched fills confirm app orders; out-of-band trades surface as proposals for
    one-click adoption — never auto-booked (NO_AUTO_REMEDIATION)."""
    if request.method == "POST":
        try:
            import transaction_ingest
            return jsonify(transaction_ingest.run_ingestion())
        except Exception as e:  # noqa: BLE001
            return _err(e)
    state = log.load_state()
    return jsonify(state.get("ingestion") or {"last": None, "proposals": []})


@ingestion_bp.route("/api/ingestion/adopt", methods=["POST"])
def api_ingestion_adopt():
    """Adopt one out-of-band broker trade (a proposal) into state.json, booking it
    through the same builders app fills use — economics verbatim from the broker
    record. Human-gated; the operator confirms the proposal."""
    payload = request.get_json(silent=True) or {}
    proposal_id = payload.get("proposal_id", "")
    if not proposal_id:
        return jsonify({"error": "proposal_id is required"}), 400
    stock_price = payload.get("stock_price")
    if stock_price in (None, ""):
        stock_price = None
    else:
        try:
            stock_price = float(stock_price)
        except (TypeError, ValueError):
            return jsonify({"error": "stock_price must be a number"}), 400
        if stock_price <= 0:
            return jsonify({"error": "stock_price must be positive"}), 400
    try:
        return jsonify(executor.adopt_broker_trade(proposal_id, stock_price))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ingestion_bp.route("/api/ingestion/adoptions")
def api_ingestion_adoptions():
    """List broker_manual adoptions booked into state (for the Undo control)."""
    return jsonify({"adoptions": executor.list_broker_manual_adoptions()})


@ingestion_bp.route("/api/ingestion/reverse", methods=["POST"])
def api_ingestion_reverse():
    """Reverse (undo) one broker_manual adoption exactly — inverts each execution
    it appended, restoring a removed LEAP leg with its original entry extrinsic."""
    payload = request.get_json(silent=True) or {}
    proposal_id = payload.get("proposal_id", "")
    if not proposal_id:
        return jsonify({"error": "proposal_id is required"}), 400
    try:
        return jsonify(executor.reverse_adoption(proposal_id, payload.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)

