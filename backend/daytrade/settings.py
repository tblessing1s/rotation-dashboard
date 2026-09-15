"""Per-account enable/disable toggle for the day-trade sleeve.

Default: ON for the primary book (this preserves existing behaviour — the
sleeve already ran, funded by the primary book's dry powder, before it
became per-account, so nothing silently stops for whoever was already
running it), OFF for every other book (an account must be deliberately
opted in — "one account I want this, another I don't").

Persisted as one small side-channel file
(``daytrade_log/enabled.json``, account_id -> bool) — NOT the accounts
registry itself (``DATA_DIR/accounts.json``): this is a day-trade-sleeve
setting, not an account-identity fact, so it stays inside daytrade's own
zero-authority storage rather than touching accounts.py's schema.
"""
from __future__ import annotations

import json
import os
import threading

import accounts
import config

PATH = os.path.join(config.DATA_DIR, "daytrade_log", "enabled.json")
_lock = threading.RLock()


def _load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def is_enabled(account_id: str) -> bool:
    settings = _load()
    if account_id in settings:
        return bool(settings[account_id])
    return account_id == accounts.DEFAULT_ID


def set_enabled(account_id: str, on: bool) -> None:
    with _lock:
        settings = _load()
        settings[account_id] = bool(on)
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, PATH)


def enabled_account_ids() -> list[str]:
    """Every non-archived account (accounts.scheduled_ids()) with day-trading
    turned on, for the scheduler to iterate."""
    return [aid for aid in accounts.scheduled_ids() if is_enabled(aid)]
