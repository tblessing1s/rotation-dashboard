#!/usr/bin/env python3
"""Restore state.json from a backup.

    ################################################################
    #  STOP THE APP FIRST.                                          #
    #                                                              #
    #  state.json is a single-writer store. This script writes     #
    #  the ACTIVE state file. If the Fly machine (or a local dev    #
    #  server) is running, its in-process writer can clobber your   #
    #  restore a second later. On Fly:  `fly scale count 0`,        #
    #  restore, then `fly scale count 1`.                          #
    ################################################################

There is no cross-process lock to detect a running app, so this script cannot
stop you from restoring under a live writer — it only refuses to run without an
explicit --yes confirmation.

Usage:
    python scripts/restore_state.py --list
    python scripts/restore_state.py --restore <backup-file> --yes
    python scripts/restore_state.py --latest --yes

The restore goes through the app's ATOMIC save path (never a raw copy), and the
current (possibly corrupt) state file is written aside as
``state.json.pre-restore.<timestamp>`` before anything is overwritten, so a
mistaken restore is itself recoverable.

DATA_DIR controls which store is touched (defaults to the backend dir locally,
/data on Fly). Set CFM demo mode via mode.json as usual to target the demo store.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
# Never let importing the app machinery start the scheduler or the startup check.
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")

import backups          # noqa: E402
import config           # noqa: E402
import logging_handler as log  # noqa: E402


def _fmt_ts(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


def cmd_list() -> int:
    rows = backups.list_backups()
    if not rows:
        print(f"No backups found in {backups.backups_dir()}")
        return 0
    print(f"Backups in {backups.backups_dir()} (newest first):\n")
    print(f"{'#':>3}  {'MODIFIED':<19}  {'VER':>3}  {'KIND':<13}  NAME")
    for i, b in enumerate(rows):
        kind = "pre-migration" if b["pre_migration"] else "nightly"
        ver = b["schema_version"] if b["schema_version"] is not None else "?"
        print(f"{i:>3}  {_fmt_ts(b['mtime']):<19}  {str(ver):>3}  {kind:<13}  {b['name']}")
    print(f"\nActive state file: {config.active_state_path()}")
    return 0


def cmd_restore(backup_path: str, confirmed: bool) -> int:
    if not os.path.exists(backup_path):
        # allow passing a bare backup name
        candidate = os.path.join(backups.backups_dir(), backup_path)
        if os.path.exists(candidate):
            backup_path = candidate
        else:
            print(f"ERROR: backup not found: {backup_path}", file=sys.stderr)
            return 2
    if not confirmed:
        print("REFUSING to restore without --yes.\n"
              "  Make sure the app is STOPPED (fly scale count 0) first — this "
              "overwrites the live state file.\n"
              f"  To proceed:  python scripts/restore_state.py --restore {backup_path} --yes",
              file=sys.stderr)
        return 1
    report = log.restore_from_backup(backup_path)
    print(f"Restored {report['from']}")
    print(f"      -> {report['restored']}")
    if report["pre_restore"]:
        print(f"  previous file saved aside as: {report['pre_restore']}")
    print("\nRestart the app now (fly scale count 1).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore state.json from a backup (STOP THE APP FIRST).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list available backups")
    g.add_argument("--restore", metavar="BACKUP", help="restore this backup file")
    g.add_argument("--latest", action="store_true", help="restore the most recent backup")
    parser.add_argument("--yes", action="store_true",
                        help="confirm the overwrite (required to actually restore)")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.latest:
        latest = backups.latest_backup()
        if not latest:
            print(f"No backups found in {backups.backups_dir()}", file=sys.stderr)
            return 2
        return cmd_restore(latest, args.yes)
    return cmd_restore(args.restore, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
