"""One broker fill, one book — the cross-book duplicate guard.

Every Schwab order and transaction belongs to exactly one brokerage account, so
it belongs in exactly one book. CONFIRMED LIVE: a secondary book imported the
primary's IBIT trades and an SPCX roll, and the primary book carried the
secondary's whole early SPCX history (same execution ids and timestamps). Each
book looked internally consistent; only comparing books shows it.

Two defenses, both keyed on the broker's own ids (a Schwab transaction id, a
Schwab order id) carried by LIVE executions — voided / reversed ones don't
count, so a book that already cleaned up a copy is not flagged for it:

  * ``find_duplicates`` / ``run_and_persist`` — the daily cross-book check;
    each involved book gets ``state["cross_book"]`` and a CROSS_BOOK_DUPLICATE
    alert.
  * ``held_elsewhere`` — refuse to ADOPT a broker trade another book holds.

Reads other books straight off their state files (read-only), the same way the
account roll-up does; writes only through ``accounts.use`` + ``mutate_state``.
"""
from __future__ import annotations

import accounts
import logging_handler as log

logger = log.logger


def broker_ids(state: dict) -> dict[str, list[str]]:
    """``{"txn:<id>" | "order:<id>": [execution ids]}`` for the live executions
    of one book: the Schwab transaction id an execution was booked from, its
    Schwab order id, and the order id its fill receipt records for it."""
    live = {str(e.get("id")): e for e in log.derived_executions(state or {})
            if e.get("id") is not None}
    out: dict[str, list[str]] = {}

    def add(key: str, eid: str) -> None:
        ids = out.setdefault(key, [])
        if eid not in ids:
            ids.append(eid)

    for eid, e in live.items():
        if e.get("transaction_id"):
            add(f"txn:{e['transaction_id']}", eid)
        if e.get("broker_order_id"):
            add(f"order:{e['broker_order_id']}", eid)
    for r in (state or {}).get("order_receipts") or []:
        if r.get("order_id") is None:
            continue
        for eid in r.get("execution_ids") or []:
            if str(eid) in live:
                add(f"order:{r['order_id']}", str(eid))
    return out


def find_duplicates(books: dict[str, dict]) -> list[dict]:
    """Every broker id held by live executions in MORE than one book.
    ``books`` maps account id -> state. Pure."""
    seen: dict[str, dict[str, list[str]]] = {}
    for acct_id, state in books.items():
        for key, eids in broker_ids(state).items():
            seen.setdefault(key, {})[acct_id] = eids
    dups = []
    for key, where in sorted(seen.items()):
        if len(where) < 2:
            continue
        kind, _, value = key.partition(":")
        dups.append({"id": key, "kind": kind, "broker_id": value,
                     "books": {a: sorted(e) for a, e in sorted(where.items())}})
    return dups


def _live_books() -> dict[str, dict]:
    books = {}
    for acct in accounts.list_accounts(include_archived=True):
        book = accounts._read_book(accounts.state_path(acct["id"], demo=False))
        if book:
            books[acct["id"]] = book
    return books


def held_elsewhere(transaction_ids, order_id=None, account_id: str | None = None) -> dict | None:
    """The first OTHER book holding any of these Schwab transaction ids or this
    order id in a live execution: ``{"account", "label", "broker_id",
    "execution_ids"}``, else None."""
    me = account_id or accounts.active_id()
    keys = [f"txn:{t}" for t in (transaction_ids or []) if t]
    if order_id:
        keys.append(f"order:{order_id}")
    if not keys:
        return None
    for acct_id, book in _live_books().items():
        if acct_id == me:
            continue
        ids = broker_ids(book)
        for k in keys:
            if k in ids:
                acct = accounts.get(acct_id) or {}
                return {"account": acct_id, "label": acct.get("label") or acct_id,
                        "broker_id": k, "execution_ids": ids[k]}
    return None


def run_and_persist(as_of: str | None = None) -> dict:
    """Check every live book against every other and store, on EACH book, the
    duplicates it is part of (``state["cross_book"]``). Books with none get an
    empty, CLEAN record so a fixed duplicate clears its alert."""
    as_of = as_of or log.utcnow()
    books = _live_books()
    dups = find_duplicates(books)
    labels = {a["id"]: a.get("label") or a["id"]
              for a in accounts.list_accounts(include_archived=True)}
    for acct_id in books:
        mine = [dict(d, labels={a: labels.get(a, a) for a in d["books"]})
                for d in dups if acct_id in d["books"]]
        try:
            with accounts.use(acct_id):
                log.mutate_state(lambda s, m=mine: _store(s, m, as_of))
        except Exception as e:  # noqa: BLE001 — one book must not sink the rest
            logger.error("cross-book record failed for %s: %s", acct_id, e)
    return {"as_of": as_of, "books": sorted(books), "duplicates": dups,
            "status": "DIRTY" if dups else "CLEAN"}


def _store(state: dict, mine: list[dict], as_of: str) -> None:
    acks = (state.get("cross_book") or {}).get("acks") or {}
    for d in mine:
        if d["id"] in acks:
            d["ack"] = acks[d["id"]]
    open_n = sum(1 for d in mine if not d.get("ack"))
    state["cross_book"] = {"as_of": as_of, "status": "DIRTY" if open_n else "CLEAN",
                           "open": open_n, "duplicates": mine, "acks": acks}


def acknowledge(dup_id: str, reason: str) -> dict:
    """Mark one duplicate explained FOR THIS BOOK (typed reason, logged) — e.g. a
    fill receipt left pointing at another account's order after the execution
    was corrected into this book's own trade. Stays acknowledged across runs."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("acknowledging a cross-book duplicate requires a typed reason")

    def _ack(state: dict) -> dict:
        cb = state.setdefault("cross_book", {"duplicates": []})
        rec = {"reason": reason, "at": log.utcnow()}
        cb.setdefault("acks", {})[str(dup_id)] = rec
        _store(state, cb.get("duplicates") or [], cb.get("as_of") or log.utcnow())
        return rec
    return log.mutate_state(_ack)


def open_duplicates(state: dict) -> list[dict]:
    return [d for d in (state.get("cross_book") or {}).get("duplicates") or []
            if not d.get("ack")]
