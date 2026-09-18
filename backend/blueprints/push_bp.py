"""Web Push (PWA native push): VAPID key handshake + subscription registry (4 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import webpush

push_bp = Blueprint("push", __name__)


@push_bp.route("/api/push/vapid-key")
def api_push_vapid_key():
    """The applicationServerKey the browser needs to subscribe, plus whether the
    server is configured and how many devices are currently registered."""
    return jsonify({
        "key": webpush.public_key(),
        "configured": webpush.keys_configured(),
        "subscriptions": webpush.subscription_count(),
    })


@push_bp.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    """Store a browser PushSubscription so alert batches reach this device."""
    payload = request.get_json(silent=True) or {}
    sub = payload.get("subscription") or payload
    try:
        return jsonify(webpush.add_subscription(sub))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@push_bp.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint", "")
    if not endpoint:
        return jsonify({"error": "endpoint is required"}), 400
    try:
        return jsonify(webpush.remove_subscription(endpoint))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@push_bp.route("/api/push/test", methods=["POST"])
def api_push_test():
    """Send a test push to every registered device — confirms the phone wiring
    without waiting for a real alert to trip."""
    if not webpush.keys_configured():
        return jsonify({"error": "VAPID keys not configured on the server"}), 400
    if webpush.subscription_count() == 0:
        return jsonify({"error": "no device subscribed yet"}), 400
    try:
        webpush.send("[CFM] Test alert",
                     "Push is wired up — real alerts will arrive here.", [])
        return jsonify({"ok": True, "devices": webpush.subscription_count()})
    except Exception as e:  # noqa: BLE001
        return _err(e)

