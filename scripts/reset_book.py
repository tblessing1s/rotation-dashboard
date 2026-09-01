#!/usr/bin/env python3
"""One-time reset of the trading book — clear all positions and start over.

    ################################################################
    #  STOP THE APP FIRST.                                          #
    #                                                              #
    #  state.json is a single-writer store. This script writes     #
    #  the ACTIVE state file. If the Fly machine (or a local dev    #
    #  server) is running, its in-process writer can clobber your   #
    #  reset a second later. On Fly:  `fly scale count 0`,          #
    #  reset, then `fly scale count 1`.                            #
    ################################################################

Positions and the theta / payback / roll / dividend ledgers are DERIVED from the
append-only execution log — deleting position records alone would just be rebuilt
by recompute_derived(). A real "start over" therefore clears the execution log and
every derived surface, resetting the book to a clean empty state at the current
schema version. This is a deliberate, one-time exception to the log's immutability,
which is why it (a) snapshots the current state before touching anything and
(b) refuses to run without an explicit --yes.

WHAT IS CLEARED: executions, positions, theta_ledger, extrinsic_payback,
roll_ledger, cycles, recommendations + overrides + fidelity, pending_orders,
order_events, order_locks, reconciliation, ingestion + ingested_transactions,
payouts records.

WHAT IS KEPT (not positions — annoying to re-enter, and the full backup has them
anyway): your phone push subscriptions (alerts.push_subscriptions) and account
cash settings (metadata.operating_cash / reserve_required). Pass --wipe-all to
reset those too for a bare state. VAPID keys live in a separate file
(DATA_DIR/.vapid_keys.json) and are never touched.

RECOVERABLE: before writing, a rotating copy of the current book is placed in the
backups dir (and shipped off-machine when SMTP_*/CFM_BACKUP_S3 are configured),
and the current file is written aside as state.json.pre-reset.<timestamp>. Undo a
mistaken reset with the sibling script:

    python scripts/restore_state.py --latest --yes

Usage:
    python scripts/reset_book.py                 # DRY RUN — show what would clear
    python scripts/reset_book.py --yes           # do it (keeps push subs + cash)
    python scripts/reset_book.py --yes --wipe-all  # bare state, keep nothing

DATA_DIR controls which store is touched (defaults to the backend dir locally,
/data on Fly). Set CFM demo mode via mode.json as usual to target the demo store.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
# Never let importing the app machinery start the scheduler or the startup check.
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import backups          # noqa: E402
import config           # noqa: E402
import logging_handler as log  # noqa: E402


def _summary(state: dict) -> str:
    positions = state.get("positions") or []
    execs = state.get("executions") or []
    theta = ((state.get("theta_ledger") or {}).get("totals") or {})
    subs = ((state.get("alerts") or {}).get("push_subscriptions") or [])
    payouts = ((state.get("payouts") or {}).get("records") or {})
    open_pos = [p for p in positions if p.get("status") != "closed"]
    lines = [
        f"  schema_version : {state.get('schema_version')}",
        f"  positions      : {len(positions)} ({len(open_pos)} open)",
        f"  executions     : {len(execs)}",
        f"  theta YTD      : {theta.get('ytd', 0)}",
        f"  payout records : {len(payouts)}",
        f"  push subs      : {len(subs)}",
    ]
    if open_pos:
        tickers = ", ".join(sorted(p.get("ticker", "?") for p in open_pos))
        lines.append(f"  open tickers   : {tickers}")
    return "\n".join(lines)


def _fresh_state(old: dict, wipe_all: bool) -> dict:
    """A clean default state, carrying forward only the non-position settings.
    Shared with the /api/admin/reset-book endpoint via logging_handler so the CLI
    and the API reset keep exactly the same things."""
    return log.book_fresh_state(old, wipe_all)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset the trading book to a clean empty state (STOP THE APP FIRST).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--yes", action="store_true",
                        help="confirm the reset (required to actually write)")
    parser.add_argument("--wipe-all", action="store_true",
                        help="also clear push subscriptions and account cash settings")
    parser.add_argument("--account", metavar="ID",
                        help="which account's book to reset (default: the active "
                             "one). Each account has its own state file — see "
                             "docs/accounts.md")
    args = parser.parse_args(argv)

    if args.account:
        import accounts
        try:
            accounts.require(args.account)
        except accounts.UnknownAccount as e:
            known = ", ".join(a["id"] for a in accounts.list_accounts(include_archived=True))
            print(f"{e} (known accounts: {known})", file=sys.stderr)
            return 2
        # Bind for the rest of the run — every state/backup call below resolves
        # through the active account.
        accounts.set_override(args.account)

    target = config.active_state_path()
    if not os.path.exists(target):
        print(f"No state file at {target} — nothing to reset.\n"
              f"  (DATA_DIR={config.DATA_DIR}. On Fly the book lives at /data/state.json; "
              f"run this on the machine, not a fresh clone.)", file=sys.stderr)
        return 2

    old = log.load_state()
    print(f"Active state file: {target}\n")
    print("CURRENT BOOK:")
    print(_summary(old))
    kept = "nothing (bare state)" if args.wipe_all else "push subscriptions + account cash"
    print(f"\nA reset would CLEAR the book above and KEEP: {kept}.")

    if not args.yes:
        print("\nDRY RUN — nothing written. Re-run with --yes to reset.\n"
              "  STOP THE APP FIRST (fly scale count 0) — state.json is single-writer.\n"
              f"  To proceed:  python scripts/reset_book.py --yes"
              f"{' --wipe-all' if args.wipe_all else ''}", file=sys.stderr)
        return 1

    # Recoverable backup FIRST — a rotating copy in the backups dir (visible to
    # restore_state.py --latest), then a copy shipped off-machine if configured. A
    # failed off-machine copy is reported, not fatal: the backups-dir copy and the
    # pre-reset aside copy (written by log.reset_book) are both still recoverable.
    backup = backups.make_nightly_backup(target)
    print(f"\nBacked up current book -> {backup}")
    off = backups.send_offmachine_copy(backup)
    if off.get("ok"):
        print(f"  off-machine copy: {off.get('method')} ok")
    else:
        why = off.get("detail") or off.get("error") or "unavailable"
        print(f"  off-machine copy: NOT sent ({why}) — local backup still kept")

    # The write itself goes through the atomic save path and writes the current
    # file aside as state.json.pre-reset.<ts> before overwriting (see reset_book).
    report = log.reset_book(build_fresh=lambda prior: _fresh_state(prior, args.wipe_all))

    print("\nBOOK RESET.")
    print(_summary(log.load_state()))
    if report["pre_reset"]:
        print(f"\n  pre-reset snapshot : {report['pre_reset']}")
    print(f"  backups-dir copy   : {backup}")
    print("\nRestart the app now (fly scale count 1).")
    print("Undo with:  python scripts/restore_state.py --latest --yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
