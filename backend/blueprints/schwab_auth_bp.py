"""Schwab OAuth (hosted re-auth) (5 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, request

from api_common import _err

import accounts
import schwab_api
import secrets

schwab_auth_bp = Blueprint("schwab_auth", __name__)


def _callback_uri() -> str:
    """The OAuth callback URL. Fly terminates TLS, so request.url_root can come
    back as http://; force https (except on localhost) so it matches the https
    callback registered with the Schwab app and used in the authorize request."""
    root = request.url_root.rstrip("/")
    if root.startswith("http://") and not any(h in root for h in ("localhost", "127.0.0.1")):
        root = "https://" + root[len("http://"):]
    return root + "/auth/schwab/callback"


def _state_for(connection: str) -> str:
    """OAuth state carrying the CONNECTION the grant is for.

    Schwab echoes ``state`` back verbatim, and the callback URL itself is fixed
    (it must match the one registered with the app), so state is the only channel
    that can tell the callback which book's login just consented. The random half
    is kept in front of it.
    """
    return f"{secrets.token_urlsafe(16)}.{connection}"


def _connection_from_state(state: str | None) -> str:
    """The connection a callback's state names, validated against the registry.

    An unknown or missing connection falls back to the SHARED grant — the only
    safe default, since it is the one every deployment already has; storing a
    stranger's consent into a book's slot is what must not happen.
    """
    tail = (state or "").rsplit(".", 1)[-1].strip()
    if not tail or tail == accounts.SHARED_CONNECTION:
        return accounts.SHARED_CONNECTION
    owner = accounts.connection_owner(tail)
    if owner and accounts.exists(owner):
        return tail
    return accounts.SHARED_CONNECTION


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
    code = request.args.get("code")
    if not code:
        return redirect("/?schwab=error&msg=missing+authorization+code")
    connection = _connection_from_state(request.args.get("state"))
    try:
        tokens = schwab_api.exchange_code(code, _callback_uri())
        # Store against the connection that STARTED the flow, never the currently
        # active account: the operator may well have switched books in another tab
        # while Schwab's consent screen was open.
        schwab_api.store_refresh_token(tokens["refresh_token"], connection)
        owner = accounts.connection_owner(connection)
        return redirect(f"/?schwab=connected&account={owner}" if owner else "/?schwab=connected")
    except Exception as e:  # noqa: BLE001
        from urllib.parse import quote
        return redirect(f"/?schwab=error&msg={quote(str(e)[:200])}")

