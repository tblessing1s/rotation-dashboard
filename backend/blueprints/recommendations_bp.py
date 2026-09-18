"""Recommendation trust layer: recommendations, dismissals, the scoreboard (8 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import config
import logging_handler as log

recommendations_bp = Blueprint("recommendations", __name__)


@recommendations_bp.route("/api/recommendations")
def api_recommendations():
    """Open (unresolved, unexpired) recommendations + the last pass summary."""
    import recommendation_runner
    import recommendation_settle as settle
    import trust_derive
    import position_manager
    import circuit_breaker
    import recommendation_auto_execute
    from datetime import datetime, timezone
    try:
        state = log.load_state()
        now = datetime.now(timezone.utc)
        open_recs = trust_derive.open_recommendations(state, now)
        # ENTER recs are frozen at the pass that emitted them: `input_snapshot`
        # carries the dry powder available AT THAT TIME. Capital moves after
        # (a later ENTER gets executed, cash changes) without pruning the ones
        # already on the board, so a lot that fit when proposed can stop
        # fitting before it's acted on. Re-check against CURRENT deployable
        # capital here — display-only, the persisted rec is untouched, so it
        # reappears the moment dry powder frees back up (a close, a cash top-up).
        deployable = position_manager.capital_summary(state).get("deployable")
        if deployable is not None:
            def _fits_dry_powder(r):
                if r.get("action_type") != "ENTER":
                    return True
                snap = r.get("input_snapshot") or {}
                lot = snap.get("lot_cost")
                if lot is None:
                    # Recs emitted before input_snapshot carried lot_cost
                    # (pre-schema, still legally "open") still price the lot
                    # on the ticket itself — fall back there rather than
                    # reading a merely-absent key as "can't judge, let it
                    # through", which is the reading only a truly unpriced
                    # candidate (no chain data at all) should get.
                    estimates = (r.get("proposed_ticket") or {}).get("estimates") or {}
                    lot = estimates.get("shares_notional")
                return lot is None or lot <= deployable
            open_recs = [r for r in open_recs if _fits_dry_powder(r)]
        # Bars are snapshot working data, not payload — strip anything
        # non-JSON-serializable defensively (records themselves never carry
        # DataFrames, but keep the endpoint robust to engine additions).
        return jsonify({
            "open": open_recs,
            "open_actionable": [r for r in open_recs if r.get("action_type") != "NO_ACTION"],
            # PENDING_SETTLE recs carry executable_at so the card can render a
            # live countdown and a pre-approve toggle (the gate deferred the order;
            # the alert already fired).
            "pending_settle": settle.pending(state),
            # How the last two weeks of engine calls were closed out — matched by
            # a move (from the card, by hand, or at Schwab) or overridden by
            # acting differently — so a position card can show the connection.
            "recent_resolutions": trust_derive.recent_resolutions(state, now),
            "gate_enforced": config.market_settle_gate_enabled(),
            "last_run": recommendation_runner.last_run(),
            "total": len(state.get("recommendations", [])),
            # Per-condition circuit-breaker auto-exit permissions (default OFF).
            # See circuit_breaker.py's AUTO-EXIT PERMISSIONS section.
            "circuit_breaker_auto_exit": circuit_breaker.get_auto_exit_permissions(state),
            # Per-trigger roll/defend auto-execute permissions (default OFF).
            # See recommendation_auto_execute.py.
            "roll_defend_auto_execute": recommendation_auto_execute.get_permissions(state),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@recommendations_bp.route("/api/recommendations/circuit-breaker-auto-exit", methods=["POST"])
def api_set_circuit_breaker_auto_exit():
    """Grant or revoke ONE circuit-breaker condition's permission to close a
    position UNATTENDED the moment it trips — see circuit_breaker.py's
    AUTO-EXIT PERMISSIONS section. Every condition defaults OFF; this is the
    only way any of them turns on. Body: {condition, on}."""
    payload = request.get_json(silent=True) or {}
    try:
        import circuit_breaker
        return jsonify(circuit_breaker.set_auto_exit_permission(
            payload.get("condition"), bool(payload.get("on"))))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return _err(e)


@recommendations_bp.route("/api/recommendations/roll-defend-auto-execute", methods=["POST"])
def api_set_roll_defend_auto_execute():
    """Grant or revoke ONE roll/defend trigger's permission to roll the short
    UNATTENDED the moment it fires — see recommendation_auto_execute.py. Every
    trigger defaults OFF; this is the only way any of them turns on.
    Body: {trigger_rule, on}."""
    payload = request.get_json(silent=True) or {}
    try:
        import recommendation_auto_execute
        return jsonify(recommendation_auto_execute.set_permission(
            payload.get("trigger_rule"), bool(payload.get("on"))))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return _err(e)


@recommendations_bp.route("/api/recommendations/run", methods=["POST"])
def api_recommendations_run():
    """Force one evaluation pass now (the scheduled slots call the same code)."""
    import recommendation_runner
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(recommendation_runner.run(trigger="manual",
            notify=bool(payload.get("notify", True)),
            include_entry=bool(payload.get("include_entry", True)),
            dry_run=payload.get("dry_run")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@recommendations_bp.route("/api/recommendations/dismiss", methods=["POST"])
def api_recommendations_dismiss():
    """Operator dismissal with a CODED override reason (+ optional note; OTHER
    requires one). Appends an immutable override record — the recommendation
    itself is never mutated; precision math derives from the record."""
    import rec_types
    import trust_derive
    from datetime import datetime, timezone
    payload = request.get_json(silent=True) or {}
    rec_id = str(payload.get("rec_id") or "")
    reason = str(payload.get("reason") or "").strip().upper()
    note = (payload.get("note") or "").strip() or None
    if not rec_id:
        return jsonify({"error": "rec_id is required"}), 400
    if not rec_types.is_override_reason(reason):
        return jsonify({"error": f"reason must be one of {sorted(rec_types.OVERRIDE_REASONS)}"}), 400
    if rec_types.override_requires_note(reason) and not note:
        return jsonify({"error": f"a typed note is required for {reason}"}), 400
    try:
        state = log.load_state()
        now = datetime.now(timezone.utc)
        open_ids = {r.get("rec_id") for r in trust_derive.open_recommendations(state, now)}
        if rec_id not in open_ids:
            return jsonify({"error": f"{rec_id} is not an open recommendation "
                                     "(already resolved, expired, or unknown)"}), 404
        stored = log.append_recommendation_override(
            {"rec_id": rec_id, "reason": reason, "note": note})
        return jsonify({"override": stored})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@recommendations_bp.route("/api/recommendations/acknowledge-miss", methods=["POST"])
def api_recommendations_acknowledge_miss():
    """Operator acknowledgement of a COVERAGE_MISS with a CODED reason (+ optional
    note; OTHER requires one). Appends an immutable record keyed on the miss's
    execution ids — the miss itself is derived and is never removed; it is
    classified. Body: {execution_ids: [...], reason, note?}."""
    import rec_types
    import trust_derive
    payload = request.get_json(silent=True) or {}
    ids = payload.get("execution_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    ids = [str(i) for i in ids if i]
    reason = str(payload.get("reason") or "").strip().upper()
    note = (payload.get("note") or "").strip() or None
    if not ids:
        return jsonify({"error": "execution_ids is required"}), 400
    if not rec_types.is_miss_ack_reason(reason):
        return jsonify({"error": f"reason must be one of {sorted(rec_types.MISS_ACK_REASONS)}"}), 400
    if rec_types.miss_ack_requires_note(reason) and not note:
        return jsonify({"error": f"a typed note is required for {reason}"}), 400
    try:
        state = log.load_state()
        key = trust_derive.miss_key(ids)
        miss = next((r for r in state.get("recommendation_resolutions", []) or []
                     if r.get("status") == "COVERAGE_MISS" and r.get("miss_key") == key), None)
        if miss is None:
            return jsonify({"error": f"no coverage miss on executions {', '.join(sorted(ids))}"}), 404
        if miss.get("acknowledged"):
            return jsonify({"error": "that coverage miss is already acknowledged",
                            "acknowledged": miss["acknowledged"]}), 409
        stored = log.append_coverage_miss_ack({
            "execution_ids": sorted(ids), "ticker": miss.get("ticker"),
            "action_type": miss.get("action_type"), "reason": reason, "note": note})
        return jsonify({"acknowledgement": stored})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@recommendations_bp.route("/api/recommendations/preapprove", methods=["POST"])
def api_recommendations_preapprove():
    """Toggle pre-approval on a PENDING_SETTLE recommendation. A pre-approved rec
    auto-submits when its settle window opens — but ONLY if its trigger re-validates
    at that moment (a filled gap self-cancels it). Body: {rec_id, approve?: bool}."""
    import recommendation_settle as settle
    from datetime import datetime, timezone
    payload = request.get_json(silent=True) or {}
    rec_id = str(payload.get("rec_id") or "")
    approve = bool(payload.get("approve", True))
    if not rec_id:
        return jsonify({"error": "rec_id is required"}), 400
    try:
        with log._lock:
            state = log.load_state()
            rec = settle.set_pre_approved(state, rec_id, approve, datetime.now(timezone.utc))
            if rec is None:
                return jsonify({"error": f"{rec_id} is not a PENDING_SETTLE recommendation "
                                         "(unknown, already released, or not deferred)"}), 404
            log.save_state(state)
        return jsonify({"rec_id": rec_id, "settle": rec.get("settle")})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@recommendations_bp.route("/api/trust-scoreboard")
def api_trust_scoreboard():
    """The derived trust scoreboard: coverage / precision / timeliness /
    fidelity / graduation per action type, plus the loud lists (coverage
    misses, fidelity failures). Read-only; recompute_derived owns the math."""
    try:
        state = log.load_state()
        board = state.get("trust_scoreboard") or {}
        fidelity = state.get("order_fidelity") or {}
        return jsonify({
            "scoreboard": board,
            "fidelity_failures": [f for f in fidelity.values() if f.get("pass") is False],
            "fidelity_records": sorted(fidelity.values(),
                                       key=lambda f: f.get("graded_at") or "")[-50:],
            "resolutions": (state.get("recommendation_resolutions") or [])[-100:],
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)

