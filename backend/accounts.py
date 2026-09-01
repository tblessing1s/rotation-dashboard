"""Multiple books in one app — the account registry.

state.json is the single source of truth for ONE book. Running several real
brokerage accounts (a taxable book, an IRA, a spouse's account) through the same
dashboard therefore means one state file PER ACCOUNT, never one file with an
account column: the execution log is append-only and every derived ledger
(positions, theta, payback, put collateral) is recomputed from it wholesale, so
mixing two books in one log would make every derived number a blend of accounts
that no migration could unpick later.

So the shape here is deliberately small:

  * ``DATA_DIR/accounts.json`` — the registry: which accounts exist, which one is
    active, and each one's optional Schwab account number.
  * one state file per account, siblings of the primary store:
    ``state.json`` (primary) / ``state.<id>.json``, and in demo mode
    ``state.demo.json`` / ``state.demo.<id>.json``.
  * an ACTIVE account, resolved per request (the ``X-CFM-Account`` header or
    ``?account=``) and otherwise from the registry's persisted choice, which is
    what the background scheduler and CLI tools read.

The primary account keeps the EXACT paths the single-account app used, so an
existing deployment is already a valid one-account registry: nothing to migrate,
and a rollback loses only the registry file.

Everything that reads or writes a book goes through ``config.active_state_path``,
which delegates here — so switching accounts switches every derived view, alert
evaluation and order path at once, with no per-call-site plumbing.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
from datetime import datetime, timezone

import config

logger = logging.getLogger("cfm.alerts")

# The account that owns the original, un-suffixed store. Its id is fixed: state
# files are addressed by id, so renaming it would orphan a live book.
DEFAULT_ID = "primary"
DEFAULT_LABEL = "Main account"

MAX_ACCOUNTS = 12          # a sanity ceiling, not a product limit
LABEL_MAX = 40

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Per-request/per-job account override. A contextvar (not a global) so the Flask
# request handling one account can't leak its selection into the scheduler
# thread evaluating another.
_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cfm_active_account", default=None)


class UnknownAccount(KeyError):
    """Raised when an account id isn't in the registry. Never falls back to the
    primary book: silently answering for the wrong account is how one book's
    orders end up logged against another's."""


class RegistryCorrupt(RuntimeError):
    """Raised when accounts.json exists but can't be parsed. Like a corrupt
    state.json we refuse rather than re-initialize — the registry maps ids to
    live brokerage accounts, and guessing that mapping is worse than stopping."""


class AccountInUse(RuntimeError):
    """Raised when deleting an account whose book still holds executions."""


# ---------------------------------------------------------------------------
# Registry file
# ---------------------------------------------------------------------------
def registry_path() -> str:
    """DATA_DIR/accounts.json (the Fly volume in production)."""
    return os.path.join(config.DATA_DIR, "accounts.json")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The Schwab connection a book trades through. "shared" is the deployment-wide
# OAuth grant (DATA_DIR/schwab_token.json) every account uses by default; an
# account can instead hold its OWN grant when its brokerage account sits under a
# different Schwab login, which the shared token can never reach however the
# consent screen is answered.
SHARED_CONNECTION = "shared"


def _default_account() -> dict:
    return {"id": DEFAULT_ID, "label": DEFAULT_LABEL, "broker_account_number": None,
            "own_connection": False, "archived": False, "created_at": _utcnow(),
            "note": ""}


def _default_registry() -> dict:
    return {"active": DEFAULT_ID, "accounts": [_default_account()]}


def _normalize(registry: dict) -> dict:
    """Coerce a loaded registry into the canonical shape. The primary account is
    always present and always first, so a hand-edited or partial file still
    describes a usable book."""
    accounts: list[dict] = []
    seen: set[str] = set()
    for raw in (registry or {}).get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        acct_id = str(raw.get("id") or "").strip().lower()
        if not _ID_RE.match(acct_id) or acct_id in seen:
            continue
        seen.add(acct_id)
        number = raw.get("broker_account_number")
        accounts.append({
            "id": acct_id,
            "label": (str(raw.get("label") or acct_id).strip() or acct_id)[:LABEL_MAX],
            "broker_account_number": str(number).strip() if number else None,
            "own_connection": bool(raw.get("own_connection")),
            "archived": bool(raw.get("archived")),
            "created_at": raw.get("created_at") or _utcnow(),
            "note": str(raw.get("note") or "")[:200],
        })
    if DEFAULT_ID not in seen:
        accounts.insert(0, _default_account())
    else:
        accounts.sort(key=lambda a: a["id"] != DEFAULT_ID)
    active = str((registry or {}).get("active") or DEFAULT_ID).strip().lower()
    if active not in {a["id"] for a in accounts}:
        active = DEFAULT_ID
    return {"active": active, "accounts": accounts}


def load_registry() -> dict:
    """The registry as stored, normalized. A missing file is the implicit
    one-account registry every existing deployment already has."""
    path = registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return _default_registry()
    except OSError as e:
        raise RegistryCorrupt(f"cannot read {path}: {e}") from e
    except ValueError as e:
        raise RegistryCorrupt(
            f"{path} is not valid JSON ({e}) — fix or remove it; each account's "
            "state file is untouched") from e
    if not isinstance(raw, dict):
        raise RegistryCorrupt(f"{path} does not contain an object")
    return _normalize(raw)


def _write_registry(registry: dict) -> dict:
    """Atomic replace (temp + rename), same durability posture as mode.json."""
    registry = _normalize(registry)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = registry_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return registry


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def list_accounts(include_archived: bool = False) -> list[dict]:
    accounts = load_registry()["accounts"]
    return accounts if include_archived else [a for a in accounts if not a["archived"]]


def get(account_id: str) -> dict | None:
    account_id = str(account_id or "").strip().lower()
    for acct in load_registry()["accounts"]:
        if acct["id"] == account_id:
            return acct
    return None


def require(account_id: str) -> dict:
    acct = get(account_id)
    if acct is None:
        raise UnknownAccount(f"unknown account '{account_id}'")
    return acct


def exists(account_id: str) -> bool:
    return get(account_id) is not None


def active_id() -> str:
    """The account this call runs against: the request/job override when set,
    else the registry's persisted choice."""
    override = _override.get()
    if override:
        return override
    return load_registry()["active"]


def active() -> dict:
    """The active account record (falls back to the primary if the persisted
    active id has since been deleted)."""
    return get(active_id()) or _default_account()


def scheduled_ids() -> list[str]:
    """Accounts the background scheduler evaluates — every non-archived one.
    Archiving is how an operator retires a book without deleting its history."""
    return [a["id"] for a in list_accounts()]


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------
def set_override(account_id: str | None):
    """Bind this context (request or scheduler job) to one account. Returns the
    contextvar token for ``reset_override``. Validates: an unknown id must fail
    loudly at the boundary, not silently read the primary book."""
    if account_id is not None:
        account_id = str(account_id).strip().lower()
        require(account_id)
    return _override.set(account_id)


def reset_override(token) -> None:
    _override.reset(token)


@contextlib.contextmanager
def use(account_id: str | None):
    """Run a block against one account: ``with accounts.use("ira"): …``."""
    token = set_override(account_id)
    try:
        yield
    finally:
        reset_override(token)


# ---------------------------------------------------------------------------
# State paths
# ---------------------------------------------------------------------------
def _sibling(base_path: str, account_id: str) -> str:
    """``/data/state.json`` + ``ira`` -> ``/data/state.ira.json`` (and
    ``state.demo.json`` -> ``state.demo.ira.json``)."""
    root, ext = os.path.splitext(base_path)
    return f"{root}.{account_id}{ext}"


def state_path(account_id: str | None = None, demo: bool | None = None) -> str:
    """The state file for one account in the current (or given) data mode.

    Reads config.STATE_PATH / DEMO_STATE_PATH at call time so a test (or a
    relocated DATA_DIR) that repoints those still governs every account.
    """
    account_id = (account_id or active_id()).strip().lower()
    demo = config.demo_enabled() if demo is None else demo
    base = config.DEMO_STATE_PATH if demo else config.STATE_PATH
    return base if account_id == DEFAULT_ID else _sibling(base, account_id)


def active_state_path() -> str:
    return state_path()


def all_state_paths(include_archived: bool = True) -> list[str]:
    """Every account's live AND demo store — what a store-wide sweep (orphan temp
    files, integrity checks) has to cover now that a book is per account."""
    out: list[str] = []
    for acct in list_accounts(include_archived=include_archived):
        for demo in (False, True):
            path = state_path(acct["id"], demo=demo)
            if path not in out:
                out.append(path)
    return out


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------
def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(label or "").strip().lower()).strip("-")[:32]
    return slug.strip("-")


def _unique_id(base: str, taken: set[str]) -> str:
    if base and base not in taken:
        return base
    stem = base or "account"
    for n in range(2, 100):
        candidate = f"{stem}-{n}"[:32].strip("-")
        if candidate not in taken:
            return candidate
    raise ValueError("could not derive a unique account id")


def create(label: str, broker_account_number: str | None = None,
           account_id: str | None = None, note: str = "") -> dict:
    """Register a new book. The state file is NOT created here — it is written
    lazily on the account's first load, exactly like a fresh install, so a
    mis-typed account can be removed again without leaving a store behind."""
    registry = load_registry()
    taken = {a["id"] for a in registry["accounts"]}
    if len(registry["accounts"]) >= MAX_ACCOUNTS:
        raise ValueError(f"at most {MAX_ACCOUNTS} accounts are supported")
    label = (str(label or "").strip() or "Account")[:LABEL_MAX]
    if account_id:
        account_id = str(account_id).strip().lower()
        if not _ID_RE.match(account_id):
            raise ValueError("account id must be lowercase letters, digits or dashes")
        if account_id in taken:
            raise ValueError(f"account '{account_id}' already exists")
    else:
        account_id = _unique_id(slugify(label), taken)
        if not _ID_RE.match(account_id):
            raise ValueError("could not derive an account id from that name")
    acct = {
        "id": account_id,
        "label": label,
        "broker_account_number": (str(broker_account_number).strip()
                                  if broker_account_number else None),
        "own_connection": False,
        "archived": False,
        "created_at": _utcnow(),
        "note": str(note or "")[:200],
    }
    registry["accounts"].append(acct)
    _write_registry(registry)
    logger.info("account registered: %s (%s)", acct["id"], acct["label"])
    return acct


def update(account_id: str, label: str | None = None,
           broker_account_number: str | None = None,
           archived: bool | None = None, note: str | None = None,
           own_connection: bool | None = None) -> dict:
    """Rename an account, (re)bind its brokerage account number, archive it, or
    switch it between the shared Schwab connection and its own.

    ``broker_account_number=""`` clears the binding (back to the first linked
    Schwab account); ``None`` leaves it as it is.

    Switching a book ONTO its own connection does not itself connect anything —
    it declares that this book authenticates separately, and until that grant is
    completed (``/auth/schwab?account=<id>``) the book reads as not connected
    rather than silently borrowing the shared token. Switching it back OFF leaves
    the stored grant in place, so a book can be moved back and forth without a
    re-consent; ``disconnect()`` is what discards it.
    """
    registry = load_registry()
    target = None
    for acct in registry["accounts"]:
        if acct["id"] == str(account_id or "").strip().lower():
            target = acct
            break
    if target is None:
        raise UnknownAccount(f"unknown account '{account_id}'")
    if label is not None:
        target["label"] = (str(label).strip() or target["label"])[:LABEL_MAX]
    if broker_account_number is not None:
        number = str(broker_account_number).strip()
        target["broker_account_number"] = number or None
    if note is not None:
        target["note"] = str(note)[:200]
    if own_connection is not None:
        if bool(own_connection) and target["id"] == DEFAULT_ID:
            raise ValueError(
                "the primary account uses the deployment's own Schwab connection "
                "— that connection IS the shared one")
        target["own_connection"] = bool(own_connection)
    if archived is not None:
        if bool(archived) and target["id"] == DEFAULT_ID:
            raise ValueError("the primary account cannot be archived")
        target["archived"] = bool(archived)
        if target["archived"] and registry["active"] == target["id"]:
            registry["active"] = DEFAULT_ID
    _write_registry(registry)
    return target


def set_active(account_id: str) -> dict:
    """Persist the operator's account choice (what background jobs and a fresh
    browser session read). Archived accounts can't be made active."""
    acct = require(account_id)
    if acct["archived"]:
        raise ValueError(f"account '{acct['id']}' is archived — unarchive it first")
    registry = load_registry()
    registry["active"] = acct["id"]
    _write_registry(registry)
    return acct


def delete(account_id: str, purge: bool = False) -> dict:
    """Remove an account from the registry.

    Refuses on the primary account, and on any book that has executions unless
    ``purge`` is set — and even then the state file is RENAMED aside
    (``state.<id>.json.deleted-<ts>``), never unlinked: an execution log is a
    trading record, and no UI click should be able to destroy one.
    """
    account_id = str(account_id or "").strip().lower()
    if account_id == DEFAULT_ID:
        raise ValueError("the primary account cannot be deleted")
    require(account_id)
    moved: list[str] = []
    for demo in (False, True):
        path = state_path(account_id, demo=demo)
        if not os.path.exists(path):
            continue
        if not purge and _execution_count(path):
            raise AccountInUse(
                f"account '{account_id}' still has executions — archive it, or "
                "delete with purge to set its book aside")
        if purge:
            aside = f"{path}.deleted-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            os.replace(path, aside)
            moved.append(aside)
    # A deleted book's own Schwab grant goes with it — a credential for an
    # account nothing points at any more is pure liability.
    try:
        import schwab_api
        os.remove(schwab_api.token_path(f"account-{account_id}"))
    except Exception:  # noqa: BLE001 — a stuck token file must never block the delete
        pass
    registry = load_registry()
    registry["accounts"] = [a for a in registry["accounts"] if a["id"] != account_id]
    if registry["active"] == account_id:
        registry["active"] = DEFAULT_ID
    _write_registry(registry)
    logger.info("account deleted: %s (books set aside: %s)", account_id, moved or "none")
    return {"id": account_id, "removed": True, "books_set_aside": moved}


# ---------------------------------------------------------------------------
# Schwab connection
#
# The default is one grant for the whole deployment: every book authenticates as
# the same Schwab login, and the per-account BINDING (below) says which of that
# login's accounts it trades. That covers the common case — several accounts
# under one login.
#
# It cannot cover an account under a DIFFERENT login (a spouse's account, a
# second credential, an account linked on schwab.com for viewing only): the
# Trader API only ever returns the accounts the grant itself covers, so no
# consent-screen answer makes that account visible to this token. Such a book
# holds its own grant instead — its own refresh token, its own access token, its
# own /accounts enumeration — and everything downstream (orders, transactions,
# cash, reconciliation) follows it because the client is resolved per connection.
# ---------------------------------------------------------------------------
def connection_id(account_id: str | None = None) -> str:
    """Which Schwab grant this book authenticates with: ``SHARED_CONNECTION`` for
    the deployment-wide one, or ``account-<id>`` for a book with its own."""
    acct = get(account_id or active_id())
    if acct and acct.get("own_connection"):
        return f"account-{acct['id']}"
    return SHARED_CONNECTION


def connection_owner(connection: str) -> str | None:
    """The account id behind an ``account-<id>`` connection (None for shared)."""
    if not connection or connection == SHARED_CONNECTION:
        return None
    return connection[len("account-"):] if connection.startswith("account-") else None


def connections() -> list[str]:
    """Every distinct grant this deployment holds — the shared one plus one per
    account that authenticates separately. What a token-expiry sweep iterates."""
    out = [SHARED_CONNECTION]
    for acct in list_accounts(include_archived=True):
        if acct.get("own_connection"):
            out.append(f"account-{acct['id']}")
    return out


def disconnect(account_id: str) -> dict:
    """Discard a book's own Schwab grant and put it back on the shared one.

    The refresh token file is DELETED rather than set aside: unlike an execution
    log it is a credential, not a record, and a stale one on the volume is a
    liability with no recovery value (reconnecting re-mints it in a click).
    """
    import schwab_api

    acct = require(account_id)
    path = schwab_api.token_path(f"account-{acct['id']}")
    removed = False
    try:
        os.remove(path)
        removed = True
    except FileNotFoundError:
        pass
    update(acct["id"], own_connection=False)
    logger.info("account %s disconnected from its own Schwab grant (token removed: %s)",
                acct["id"], removed)
    return {"id": acct["id"], "token_removed": removed, "own_connection": False}


# ---------------------------------------------------------------------------
# Brokerage binding
# ---------------------------------------------------------------------------
def broker_account_number(account_id: str | None = None) -> str | None:
    """The Schwab account number bound to this book, if the operator set one."""
    acct = get(account_id or active_id())
    return (acct or {}).get("broker_account_number") or None


def broker_hash(client, account_id: str | None = None) -> str:
    """The Schwab account HASH orders for this book must be placed against.

    Unbound accounts keep the historical behaviour (the first linked account), so
    a single-account deployment is unchanged. A bound account resolves its number
    through /accounts/accountNumbers and FAILS if that number isn't linked —
    routing an order to the wrong account is precisely what the binding exists
    to prevent, so there is no fallback.
    """
    number = broker_account_number(account_id)
    if not number:
        return client.primary_account_hash()
    for entry in client.account_numbers() or []:
        if str(entry.get("accountNumber") or "").strip() == number:
            hash_value = entry.get("hashValue")
            if hash_value:
                return hash_value
    raise RuntimeError(
        f"account '{account_id or active_id()}' is bound to Schwab account "
        f"{_mask(number)}, which is not linked to this login — re-link it or clear "
        "the binding in Settings → Accounts")


def _mask(number: str | None) -> str:
    number = str(number or "")
    return f"…{number[-4:]}" if len(number) > 4 else number


# ---------------------------------------------------------------------------
# Cross-account roll-up
# ---------------------------------------------------------------------------
def _read_book(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            book = json.load(fh)
    except (OSError, ValueError):
        return None
    return book if isinstance(book, dict) else None


def _execution_count(path: str) -> int:
    book = _read_book(path)
    return len(book.get("executions") or []) if book else 0


def _account_summary(acct: dict) -> dict:
    """One account's monitoring line, read STRAIGHT off its state file.

    Deliberately provider-free: the point of the multi-account view is to see
    every book at a glance, which it can only do if the roll-up costs a few file
    reads rather than N Schwab round-trips. Live numbers stay one click away on
    the account's own tabs.
    """
    path = state_path(acct["id"])
    connection = connection_id(acct["id"])
    try:
        import schwab_api
        token = schwab_api.token_status(connection)
    except Exception:  # noqa: BLE001 — a token read must never sink the roll-up
        token = {}
    row = {
        "id": acct["id"],
        "label": acct["label"],
        "archived": acct["archived"],
        # A book whose own Schwab login isn't connected can't trade or reconcile,
        # which belongs on the monitor next to its positions, not two tabs away.
        "connection": {
            "mode": "own" if acct.get("own_connection") else "shared",
            "connected": bool(token.get("present")),
            "status": token.get("status"),
        },
        "broker_account_number": _mask(acct["broker_account_number"]) or None,
        "broker_bound": bool(acct["broker_account_number"]),
        "state_path": path,
        "exists": os.path.exists(path),
        "open_positions": 0,
        "tickers": [],
        "capital_deployed": 0.0,
        "operating_cash": 0.0,
        "theta": {},
        "active_alerts": 0,
        "pending_orders": 0,
        "open_proposals": 0,
        "last_updated": None,
        "last_reconciled": None,
        "error": None,
    }
    if not row["exists"]:
        return row
    book = _read_book(path)
    if book is None:
        row["error"] = "state file unreadable"
        return row
    open_positions = [p for p in book.get("positions") or []
                      if p.get("status") != "closed"]
    metadata = book.get("metadata") or {}
    ledger = book.get("theta_ledger") or {}
    reconciliation = (book.get("reconciliation") or {}).get("last") or {}
    row.update({
        "open_positions": len(open_positions),
        "tickers": sorted({p.get("ticker") for p in open_positions if p.get("ticker")}),
        "capital_deployed": round(float(metadata.get("capital_deployed") or 0), 2),
        "operating_cash": round(float(metadata.get("operating_cash") or 0), 2),
        "theta": ledger.get("totals") or {},
        "active_alerts": len((book.get("alerts") or {}).get("active") or {}),
        "pending_orders": len(book.get("pending_orders") or {}),
        "open_proposals": len((book.get("ingestion") or {}).get("proposals") or []),
        "last_updated": metadata.get("last_updated"),
        "last_reconciled": reconciliation.get("timestamp") or reconciliation.get("ts"),
    })
    return row


def summary(include_archived: bool = False) -> dict:
    """Every book on one screen: positions, deployed capital, week/month theta,
    live alerts, pending orders and un-adopted broker fills per account, plus the
    totals across them."""
    rows = [_account_summary(a) for a in list_accounts(include_archived=include_archived)]
    totals = {
        "accounts": len(rows),
        "open_positions": sum(r["open_positions"] for r in rows),
        "capital_deployed": round(sum(r["capital_deployed"] for r in rows), 2),
        "operating_cash": round(sum(r["operating_cash"] for r in rows), 2),
        "this_week": round(sum(float((r["theta"] or {}).get("this_week") or 0)
                               for r in rows), 2),
        "this_month": round(sum(float((r["theta"] or {}).get("this_month") or 0)
                                for r in rows), 2),
        "ytd": round(sum(float((r["theta"] or {}).get("ytd") or 0) for r in rows), 2),
        "active_alerts": sum(r["active_alerts"] for r in rows),
        "pending_orders": sum(r["pending_orders"] for r in rows),
        "open_proposals": sum(r["open_proposals"] for r in rows),
    }
    return {"active": active_id(), "demo": config.demo_enabled(),
            "accounts": rows, "totals": totals}
