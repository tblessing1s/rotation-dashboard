"""Accounts (one book per brokerage account; see accounts.py) (9 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import accounts
import config
import data_handler
import schwab_api

accounts_bp = Blueprint("accounts", __name__)


@accounts_bp.route("/api/accounts")
def api_accounts():
    """The registry: every account, its Schwab connection, which is active, and
    this request's binding.

    The connection block is what makes "why can't this book see its account"
    answerable in the UI: it says whether the book authenticates with the shared
    grant or its own, and whether that grant is actually good right now."""
    try:
        rows = []
        for acct in accounts.list_accounts(include_archived=True):
            connection = accounts.connection_id(acct["id"])
            status = schwab_api.token_status(connection)
            rows.append({**acct, "connection": {
                "id": connection,
                "mode": "own" if acct.get("own_connection") else "shared",
                "connected": bool(status.get("present")),
                "status": status.get("status"),
                "days_left": status.get("daysLeft"),
            }})
        return jsonify({
            "active": accounts.active_id(),
            "persisted_active": accounts.load_registry()["active"],
            "demo": config.demo_enabled(),
            "accounts": rows,
            "max_accounts": accounts.MAX_ACCOUNTS,
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@accounts_bp.route("/api/accounts", methods=["POST"])
def api_accounts_create():
    """Register another book. Its state file is created lazily on first use, so a
    new account starts as an empty book at the current schema version."""
    payload = request.get_json(silent=True) or {}
    try:
        acct = accounts.create(
            payload.get("label") or "",
            broker_account_number=payload.get("broker_account_number"),
            account_id=payload.get("id"),
            note=payload.get("note") or "")
        return jsonify(acct)
    except (ValueError, accounts.RegistryCorrupt) as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@accounts_bp.route("/api/accounts/<account_id>", methods=["PATCH"])
def api_accounts_update(account_id: str):
    """Rename, (re)bind to a brokerage account number, archive/unarchive."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(accounts.update(
            account_id,
            label=payload.get("label"),
            broker_account_number=payload.get("broker_account_number"),
            archived=payload.get("archived"),
            note=payload.get("note"),
            own_connection=payload.get("own_connection")))
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@accounts_bp.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_accounts_delete(account_id: str):
    """Remove an account. Refused while its book still holds executions unless
    ``?purge=1``, and even then the book is only set aside on the volume — an
    execution log is a trading record, not a UI-deletable object."""
    purge = request.args.get("purge", "").strip().lower() in ("1", "true", "yes")
    try:
        return jsonify(accounts.delete(account_id, purge=purge))
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except accounts.AccountInUse as e:
        return _err(e, 409)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@accounts_bp.route("/api/accounts/<account_id>/connection", methods=["DELETE"])
def api_accounts_disconnect(account_id: str):
    """Discard this book's own Schwab grant and put it back on the shared one.

    The refresh token is deleted rather than set aside — it is a credential, not
    a record, and reconnecting re-mints it in one click."""
    try:
        return jsonify(accounts.disconnect(account_id))
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@accounts_bp.route("/api/accounts/active", methods=["POST"])
def api_accounts_set_active():
    """Persist the operator's account choice.

    Deliberately clears NOTHING. The demo switch drops the scan and price caches
    because it changes the DATA SOURCE — a sweep computed against synthetic prices
    must never be replayed for real ones. An account switch changes the BOOK, and
    the market caches are account-free by construction: the memoized sweep holds
    market facts only, affordability and the Level-5 overlay are applied per
    request from the active book's state.

    Clearing them here cost the day's full-universe sweep (`scan_cache.clear()`
    deletes it) and every cached daily frame on every switch, so the next Scan had
    to re-sweep ~500 tickers and re-read every parquet — which is what made the
    Scan tab time out after switching accounts.
    """
    payload = request.get_json(silent=True) or {}
    try:
        acct = accounts.set_active(payload.get("id") or payload.get("account") or "")
        return jsonify({"active": acct["id"], "account": acct})
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@accounts_bp.route("/api/accounts/summary")
def api_accounts_summary():
    """Every book on one screen — open positions, deployed capital, week/month
    theta, live alerts, working orders and un-adopted broker fills per account.

    Read straight off the state files (no provider calls), so the multi-account
    monitor stays a single cheap request however many books there are."""
    include_archived = request.args.get("include_archived", "").strip().lower() in ("1", "true", "yes")
    try:
        return jsonify(accounts.summary(include_archived=include_archived))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@accounts_bp.route("/api/accounts/broker-accounts")
def api_broker_accounts():
    """The brokerage accounts this Schwab login can reach, for the binding picker.

    One login commonly reaches several accounts; binding a book to one is what
    keeps its orders, transactions, cash and reconciliation on that account. Only
    account numbers are returned — the trading hashes stay server-side.

    ALWAYS 200, carrying the reason when the list is short or empty. Schwab
    returns only the accounts the app authorization covers, so "my other account
    isn't in the picker" has several different causes (not connected, token
    expired, the account wasn't ticked at consent) and an opaque 400 hides which
    one it is. The UI shows the count and the reason verbatim, and lets the
    operator type a number the enumeration can't see."""
    connection = accounts.connection_id()
    own = connection != accounts.SHARED_CONNECTION
    out = {
        "accounts": [],
        "count": 0,
        "connection": connection,
        "connection_mode": "own" if own else "shared",
        "schwab_configured": schwab_api.configured(),
        "token": schwab_api.token_status(),
        "demo": config.demo_enabled(),
        "error": None,
    }
    if not out["schwab_configured"]:
        out["error"] = (
            "This book authenticates with its own Schwab login, which isn't "
            "connected yet — use Connect below." if own else
            "Schwab isn't connected on this deployment, so the account list can't "
            "be read. Connect it from the Schwab card above.")
        return jsonify(out)
    try:
        numbers = data_handler.broker_client().account_numbers() or []
    except Exception as e:  # noqa: BLE001 — report the reason, don't hide it
        out["error"] = str(e)
        return jsonify(out)
    bound = {a["broker_account_number"]: a["id"]
             for a in accounts.list_accounts(include_archived=True)
             if a["broker_account_number"]}
    for entry in numbers:
        number = str(entry.get("accountNumber") or "").strip()
        if not number:
            continue
        out["accounts"].append({
            "account_number": number,
            "masked": f"…{number[-4:]}" if len(number) > 4 else number,
            "bound_to": bound.get(number),
        })
    out["count"] = len(out["accounts"])
    if not out["count"]:
        out["error"] = ("Schwab returned no accounts for this login. That usually "
                        "means the app authorization covers no account yet — "
                        "reconnect Schwab and tick every account on the consent screen.")
    return jsonify(out)


@accounts_bp.route("/api/accounts/connections")
def api_account_connections():
    """Every Schwab grant this deployment holds, and how each one is doing.

    One expired grant is a silent outage for the books behind it — the shared
    token's expiry is already surfaced, and a second login's must be too."""
    try:
        rows = []
        for connection in accounts.connections():
            owner = accounts.connection_owner(connection)
            acct = accounts.get(owner) if owner else None
            rows.append({
                "connection": connection,
                "mode": "own" if owner else "shared",
                "account": owner,
                "label": (acct or {}).get("label") if acct else "Shared login",
                "token": schwab_api.token_status(connection),
                "configured": schwab_api.configured(connection),
            })
        return jsonify({"connections": rows})
    except Exception as e:  # noqa: BLE001
        return _err(e)

