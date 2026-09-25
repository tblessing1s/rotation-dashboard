"""Execution log (raw records, void, verify-fills, order journal) (4 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import executor
import logging_handler as log

executions_bp = Blueprint("executions", __name__)


@executions_bp.route("/api/executions/raw")
def api_executions_raw():
    """Raw, unprocessed data for validation: the append-only execution log
    (newest first, capped) plus each position's LIVE derived legs (short_calls /
    leap_legs / shares). Read-only. Lets the operator eyeball exactly what state
    holds — e.g. spot a duplicate short leg or a leg with no entry extrinsic.

    Also returns ``corrected_by_id``: the same executions with any saved
    ``txn_correction`` overlaid (see logging_handler.derived_executions) — what
    the History editor should reseed its rows from after a save. The raw
    ``executions`` list deliberately stays pre-correction (this route's own
    validation purpose), so a saved correction otherwise looked reverted the
    instant the editor reloaded from it, even though it was already applied
    everywhere else (ledgers, Payouts)."""
    try:
        state = log.load_state()
        execs = list(reversed(state.get("executions", [])))[:300]
        corrected_by_id = {str(e["id"]): e for e in log.derived_executions(state) if e.get("id")}
        positions = []
        for p in state.get("positions", []):
            ticker = p.get("ticker")
            # What a full DATE-order replay of the transaction log implies —
            # vs. the live mirror (below), which is updated incrementally and
            # can drift after a historical trade is recovered out of order
            # (see executor.rebuild_shares_from_log). Absent (never wrong) the
            # instant the two agree, so this stays quiet for every ordinary
            # position.
            expected = executor.replay_shares_from_log(ticker, state)
            positions.append({
                "ticker": ticker,
                "status": p.get("status"),
                "needs_review": bool(p.get("needs_review")),
                "short_calls": p.get("short_calls") or [],
                "leap_legs": log.leap_legs(p),
                "shares": p.get("shares") or {},
                "expected_shares_count": expected["count"],
            })
        return jsonify({"executions": execs, "corrected_by_id": corrected_by_id, "positions": positions,
                        "execution_count": len(state.get("executions", []))})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@executions_bp.route("/api/executions/void", methods=["POST"])
def api_executions_void():
    """Void (exclude) or restore executions — an append-only soft delete for
    pruning pre-trading test/setup entries. Voided executions drop out of history
    + derived ledgers but stay on the immutable log."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
    if not ids:
        return jsonify({"error": "ids is required"}), 400
    try:
        if payload.get("restore"):
            return jsonify(executor.restore_executions(ids))
        return jsonify(executor.void_executions(ids, payload.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@executions_bp.route("/api/executions/order-journal")
def api_executions_order_journal():
    """The durable order journal (outside state.json, survives a lost/edited
    execution) — read-only, newest first. Filter with ?ticker= to check whether
    a real broker order for that name ever captured a stock price, e.g. to
    recover the underlying price at a historical fill without shell/flyctl
    access to the Fly volume the journal lives on."""
    ticker = (request.args.get("ticker") or "").strip().upper()
    limit = int(request.args.get("limit") or 200)
    try:
        entries = list(reversed(log.order_journal_entries()))
        if ticker:
            entries = [e for e in entries if str(e.get("ticker") or "").upper() == ticker]
        return jsonify({"entries": entries[:limit], "total_matched": len(entries)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@executions_bp.route("/api/verify-fills", methods=["POST"])
def api_verify_fills():
    """Re-fetch recent live orders from Schwab and diff their fills against what
    we recorded, plus a reconcile pass. The live-order verification harness."""
    import fill_verify
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(fill_verify.verify_live_fills(limit=int(payload.get("limit", 20))))
    except Exception as e:  # noqa: BLE001
        return _err(e)

