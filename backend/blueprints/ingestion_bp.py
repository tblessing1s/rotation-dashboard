"""Transaction ingestion (Schwab executions -> state, spec §4) (5 routes) — split out of the former monolithic app.py."""
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
    one-click adoption — never auto-booked (NO_AUTO_REMEDIATION). An optional JSON
    body {"lookback_days": N} pulls a deeper one-off window (e.g. to recover a
    trade placed through the broker's own platform long enough ago the normal
    daily window never saw it) without changing the standing default."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        lookback_days = payload.get("lookback_days")
        if lookback_days not in (None, ""):
            try:
                lookback_days = int(lookback_days)
            except (TypeError, ValueError):
                return jsonify({"error": "lookback_days must be a number"}), 400
        else:
            lookback_days = None
        try:
            import transaction_ingest
            return jsonify(transaction_ingest.run_ingestion(lookback_days=lookback_days))
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


@ingestion_bp.route("/api/ingestion/release", methods=["POST"])
def api_ingestion_release():
    """Un-forget one or more transaction ids from the dedupe ledger so the next
    ingestion run re-classifies them instead of silently skipping them as
    duplicates — recovery for a transaction wrongly marked "matched" by a
    known-but-unfulfilled app order id (see transaction_ingest.release_ingested).
    Removes only the dedupe marker; nothing in the immutable execution log is
    touched. Safe to call on a transaction that really was already booked — the
    next ingest just re-verifies it via content match and it matches again."""
    payload = request.get_json(silent=True) or {}
    transaction_ids = payload.get("transaction_ids") or []
    if not transaction_ids:
        return jsonify({"error": "transaction_ids is required"}), 400
    try:
        import transaction_ingest as ingest

        def _release(state):
            removed = ingest.release_ingested(state, transaction_ids)
            return {"released": removed}
        return jsonify(log.mutate_state(_release))
    except Exception as e:  # noqa: BLE001
        return _err(e)

