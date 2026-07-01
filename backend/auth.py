"""Single-user password gate for the dashboard.

One password guards every /api route (and the Schwab re-auth initiation). A
successful login sets a signed, HttpOnly session cookie that lasts 30 days, so
you log in once and stay in.

Configuration (all optional for local dev, required in production):
  DASHBOARD_PASSWORD_HASH  preferred — a werkzeug password hash. Generate with:
      python -c "from werkzeug.security import generate_password_hash as g; \\
                 print(g(input('password: ')))"
  DASHBOARD_PASSWORD       plaintext fallback (fine for local use, avoid in prod).
  DASHBOARD_SECRET_KEY     cookie-signing key. If unset a random key is generated
                           once and persisted under DATA_DIR/.session_secret so
                           sessions survive restarts on the Fly volume.
  DASHBOARD_COOKIE_INSECURE=1  drop the cookie "Secure" flag (only for local http).

If no password is configured the gate is DISABLED (open) — this keeps local
development frictionless. Set the secret in production so the app is protected.
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from datetime import timedelta

from flask import jsonify, request, session
from werkzeug.security import check_password_hash

import config

SESSION_LIFETIME = timedelta(days=30)
COOKIE_NAME = "cfm_session"
SECRET_FILE = os.path.join(config.DATA_DIR, ".session_secret")

_PW_HASH = (os.environ.get("DASHBOARD_PASSWORD_HASH") or "").strip()
_PW_PLAIN = (os.environ.get("DASHBOARD_PASSWORD") or "").strip()

# Paths that must stay reachable without a session: the auth endpoints
# themselves, and the Schwab OAuth callback (a browser redirect landing that
# only exchanges a Schwab-issued code). Everything else under /api or
# /auth/schwab is protected; the static frontend is served openly so the login
# page can load.
_OPEN_PATHS = {"/api/login", "/api/logout", "/api/auth/status", "/auth/schwab/callback"}


def enabled() -> bool:
    """True when a password is configured (and therefore the gate is active)."""
    return bool(_PW_HASH or _PW_PLAIN)


def _secure_cookies() -> bool:
    return (os.environ.get("DASHBOARD_COOKIE_INSECURE") or "").strip() not in ("1", "true", "yes")


def _secret_key() -> str:
    env = (os.environ.get("DASHBOARD_SECRET_KEY") or "").strip()
    if env:
        return env
    try:
        with open(SECRET_FILE, encoding="utf-8") as fh:
            key = fh.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError:
        pass  # fall back to the in-memory key (sessions reset on restart)
    return key


def init_app(app) -> None:
    app.secret_key = _secret_key()
    app.config.update(
        SESSION_COOKIE_NAME=COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_secure_cookies(),
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    )


def verify_password(password: str) -> bool:
    """Constant-time-ish password check. Sleeps briefly on failure to throttle
    brute-force attempts against the single account."""
    ok = False
    if password:
        if _PW_HASH:
            try:
                ok = check_password_hash(_PW_HASH, password)
            except Exception:  # noqa: BLE001 — malformed hash string
                ok = False
        elif _PW_PLAIN:
            ok = hmac.compare_digest(password, _PW_PLAIN)
    if not ok:
        time.sleep(0.75)
    return ok


def login() -> None:
    session.permanent = True
    session["auth"] = True


def logout() -> None:
    session.clear()


def is_authenticated() -> bool:
    return session.get("auth") is True


def _is_protected(path: str) -> bool:
    if path in _OPEN_PATHS:
        return False
    return path.startswith("/api/") or path.startswith("/auth/schwab")


def gate():
    """before_request hook. Returns a 401 response for protected routes when the
    request carries no valid session; returns None to let the request through."""
    if not enabled():
        return None
    if request.method == "OPTIONS":  # never block CORS preflight
        return None
    if not _is_protected(request.path):
        return None
    if is_authenticated():
        return None
    return jsonify({"error": "authentication required", "auth_required": True}), 401
