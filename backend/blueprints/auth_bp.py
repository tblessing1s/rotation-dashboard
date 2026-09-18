"""Auth (single-user password gate; see auth.py) (3 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import auth

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/api/auth/status")
def api_auth_status():
    return jsonify({"required": auth.enabled(), "authenticated": auth.is_authenticated()})


@auth_bp.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(silent=True) or {}
    if auth.verify_password(payload.get("password", "")):
        auth.login()
        return jsonify({"ok": True})
    return jsonify({"error": "invalid password"}), 401


@auth_bp.route("/api/logout", methods=["POST"])
def api_logout():
    auth.logout()
    return jsonify({"ok": True})

