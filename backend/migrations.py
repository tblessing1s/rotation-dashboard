"""Versioned state.json migrations.

state.json gains fields over time (alert log, circuit-breaker price, dividend
data, roll ledger, cycle records). Old state files are upgraded in place on
load: each migration takes the state dict at version N and returns it at N+1,
and ``migrate`` walks the chain up to CURRENT_VERSION. Migrations only ADD
structure — they never rewrite executions (those are immutable) and never
delete user data, so upgrading is always safe.

Files that predate versioning carry no ``schema_version`` key and are treated
as version 1 (the original schema: metadata / positions / executions /
theta_ledger / extrinsic_payback / pending_orders).
"""
from __future__ import annotations

CURRENT_VERSION = 3


def default_alert_state() -> dict:
    return {
        # fingerprint -> alert record for every condition currently firing;
        # dedup works against this set so a condition alerts once, not per run.
        "active": {},
        # append-only history of fired alerts (capped, newest last).
        "log": [],
        # operator-editable: per-type enable/disable, channel toggles, dry-run.
        "settings": {},
        "last_run": None,
    }


def _v1_to_v2(state: dict) -> dict:
    """v2 (Phase 0): persisted alert log + per-run dedup state."""
    state.setdefault("alerts", default_alert_state())
    for key, value in default_alert_state().items():
        state["alerts"].setdefault(key, value)
    return state


def _v2_to_v3(state: dict) -> dict:
    """v3 (Phase 1): per-position circuit breaker (line-in-the-sand exit price,
    required at entry from now on) and cached dividend event (ex-date/amount,
    the ASSIGNMENT_RISK input). Pre-existing positions get None — the UI and
    alerts treat that as "not set" and prompt the operator."""
    for p in state.get("positions", []):
        p.setdefault("circuit_breaker", None)
        p.setdefault("dividend", None)
    return state


MIGRATIONS = {
    1: _v1_to_v2,
    2: _v2_to_v3,
}


def migrate(state: dict) -> tuple[dict, bool]:
    """Upgrade a loaded state dict to CURRENT_VERSION.

    Returns (state, changed) — ``changed`` tells the caller to persist the
    upgraded file so the migration runs once, not on every load.
    """
    version = int(state.get("schema_version") or 1)
    changed = False
    while version < CURRENT_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:  # unknown gap — stamp and stop rather than loop
            break
        state = migration(state)
        version += 1
        state["schema_version"] = version
        changed = True
    if "schema_version" not in state:
        state["schema_version"] = version
        changed = True
    return state, changed
