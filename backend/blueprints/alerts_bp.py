"""Alerts (6 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import alerts

alerts_bp = Blueprint("alerts", __name__)


@alerts_bp.route("/api/alerts")
def api_alerts():
    try:
        return jsonify(alerts.view())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@alerts_bp.route("/api/alerts/run", methods=["POST"])
def api_alerts_run():
    """Force one evaluator pass now. Also the external-cron entry point: hitting
    this URL wakes a stopped Fly machine, and dedup makes repeat runs no-ops."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(alerts.run(dry_run=payload.get("dry_run")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@alerts_bp.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    """Send one SAMPLE position alert through the real delivery path (channels,
    settings and dry-run as persisted) and report what happened — the operator's
    "would I actually get paged?" check. Persists nothing."""
    try:
        return jsonify(alerts.test_delivery())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@alerts_bp.route("/api/alerts/ack", methods=["POST"])
def api_alerts_ack():
    payload = request.get_json(silent=True) or {}
    alert_id = payload.get("id", "")
    if not alert_id:
        return jsonify({"error": "id is required"}), 400
    try:
        return jsonify(alerts.acknowledge(alert_id))
    except ValueError as e:
        return _err(e, 404)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@alerts_bp.route("/api/alerts/ack-all", methods=["POST"])
def api_alerts_ack_all():
    """Mark every active alert seen — clears the bell without touching the
    conditions themselves (they still auto-resolve when they clear)."""
    try:
        return jsonify(alerts.acknowledge_all())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@alerts_bp.route("/api/alerts/settings", methods=["POST"])
def api_alerts_settings():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(alerts.update_settings(payload))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Recommendation trust layer: open recommendations, dismissals, the scoreboard.
# Everything served here is either an immutable record or a recompute_derived
# product — no endpoint computes a score.

