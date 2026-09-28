"""Schwab OAuth (hosted re-auth) (5 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, request

from api_common import _err

import hmac
import json
import os
import secrets
import threading
import time

import accounts
import config
import schwab_api

schwab_auth_bp = Blueprint("schwab_auth", __name__)


def _callback_uri() -> str:
    """The OAuth callback URL. Fly terminates TLS, so request.url_root can come
    back as http://; force https (except on localhost) so it matches the https
    callback registered with the Schwab app and used in the authorize request."""
    root = request.url_root.rstrip("/")
    if root.startswith("http://") and not any(h in root for h in ("localhost", "127.0.0.1")):
        root = "https://" + root[len("http://"):]
    return root + "/auth/schwab/callback"


# One-time OAuth ``state`` nonces issued by /auth/schwab (a signed-in request)
# and redeemed by the callback. The callback is reachable WITHOUT a session (it
# is a browser redirect back from Schwab), so the nonce is what proves a consent
# landing there was started from this app — without it, anyone who completed
# Schwab's consent against this app's client id could land a code on the
# callback and pick, through ``state``, which book's grant (or the shared one)
# it replaces.
STATE_TTL_SECONDS = 15 * 60
_state_lock = threading.Lock()


def _pending_path() -> str:
    return os.path.join(config.DATA_DIR, "oauth_pending.json")


def _load_pending() -> dict:
    try:
        with open(_pending_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pending(data: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = _pending_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _pending_path())


def _state_for(connection: str) -> str:
    """Issue an OAuth state for ``connection``: ``<nonce>.<connection>``, the
    nonce recorded server-side (single use, STATE_TTL_SECONDS).

    Schwab echoes ``state`` back verbatim, and the callback URL itself is fixed
    (it must match the one registered with the app), so state is the only
    channel that can tell the callback which book's login just consented."""
    nonce = secrets.token_urlsafe(24)
    now = time.time()
    with _state_lock:
        pending = {k: v for k, v in _load_pending().items()
                   if now - float(v.get("at") or 0) < STATE_TTL_SECONDS}
        pending[nonce] = {"connection": connection, "at": now}
        _save_pending(pending)
    return f"{nonce}.{connection}"


class StateRejected(ValueError):
    pass


def _redeem_state(state: str | None) -> str:
    """The connection a callback may store into — ONLY for a state this app
    issued, unexpired and not yet used, naming the connection it was issued for,
    whose book still exists. Consumes the nonce. Anything else raises
    StateRejected and nothing is stored (no fallback to the shared grant)."""
    nonce, _, connection = (state or "").partition(".")
    if not nonce or not connection:
        raise StateRejected("missing or malformed state")
    now = time.time()
    with _state_lock:
        pending = _load_pending()
        rec = None
        for k in list(pending):
            if hmac.compare_digest(k, nonce):
                rec = pending.pop(k)
                break
        pending = {k: v for k, v in pending.items()
                   if now - float(v.get("at") or 0) < STATE_TTL_SECONDS}
        _save_pending(pending)
    if rec is None:
        raise StateRejected("this Schwab login was not started from this app, or was already used")
    if now - float(rec.get("at") or 0) >= STATE_TTL_SECONDS:
        raise StateRejected("this Schwab login request expired")
    if not hmac.compare_digest(str(rec.get("connection")), connection):
        raise StateRejected("state does not match the login that was started")
    if connection != accounts.SHARED_CONNECTION:
        owner = accounts.connection_owner(connection)
        if not (owner and accounts.exists(owner)):
            raise StateRejected("the account this login was started for no longer exists")
    return connection


@schwab_auth_bp.route("/auth/schwab")
def auth_schwab():
    """Start the Schwab consent flow for ONE connection.

    Which one comes from the account this request is bound to (the
    ``?account=``/header binding): a book on its own connection re-consents its
    own login, every other book re-consents the shared one."""
    try:
        connection = accounts.connection_id()
        return jsonify({
            "authorize_url": schwab_api.authorize_url(_callback_uri(),
                                                      _state_for(connection)),
            "connection": connection,
            "account": accounts.active_id(),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e, 400)


@schwab_auth_bp.route("/auth/schwab/callback")
def auth_schwab_callback():
    from urllib.parse import quote
    code = request.args.get("code")
    if not code:
        return redirect("/?schwab=error&msg=missing+authorization+code")
    try:
        connection = _redeem_state(request.args.get("state"))
    except StateRejected as e:
        return redirect("/?schwab=error&msg=" + quote(
            f"Schwab login refused: {e}. Start it again from Settings."))
    try:
        tokens = schwab_api.exchange_code(code, _callback_uri())
        # Store against the connection that STARTED the flow, never the currently
        # active account: the operator may well have switched books in another tab
        # while Schwab's consent screen was open.
        schwab_api.store_refresh_token(tokens["refresh_token"], connection)
        owner = accounts.connection_owner(connection)
        return redirect(f"/?schwab=connected&account={owner}" if owner else "/?schwab=connected")
    except Exception as e:  # noqa: BLE001
        return redirect(f"/?schwab=error&msg={quote(str(e)[:200])}")

