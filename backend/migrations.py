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

import logging

logger = logging.getLogger("cfm.alerts")

CURRENT_VERSION = 9


class MigrationAbortedError(RuntimeError):
    """Raised when a pre-migration snapshot can't be written. We refuse to run a
    schema migration on live data without a rollback point (see docs/recovery.md)."""


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
        # browser/PWA Web Push subscriptions (one per registered device); the
        # "webpush" notifier channel delivers alert batches to these.
        "push_subscriptions": [],
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


def _v3_to_v4(state: dict) -> dict:
    """v4 (Phase 2): roll-cost / whipsaw ledger. Fully DERIVED from executions
    (recompute_derived rebuilds it after every write); the migration just seeds
    the empty structure so readers never key-error on an un-recomputed load."""
    state.setdefault("roll_ledger", {"rolls": [], "by_ticker": {}})
    return state


def _v4_to_v5(state: dict) -> dict:
    """v5 (Phase 3): closed-cycle records. DERIVED from executions (rebuilt by
    recompute_derived after migration/writes); the migration seeds the key."""
    state.setdefault("cycles", [])
    return state


def _v5_to_v6(state: dict) -> dict:
    """v6 (LEAP capital preservation): a per-position rolling snapshot of the
    long leg's daily delta, appended nightly, for the delta-velocity early
    warning. Seed the empty list on existing open positions so readers never
    key-error; it fills in from the first nightly run (ships cold). The other
    LEAP-lifecycle fields (leap_dte, extrinsic remaining/weeks, juice-vs-burn)
    are DERIVED in recompute_derived, and the ``leap_roll_id`` link that ties a
    long-leg roll's close_leap+buy_leap together is an optional field on the
    immutable executions — neither needs migrating."""
    for p in state.get("positions", []):
        p.setdefault("delta_history", [])
    return state


def _v6_to_v7(state: dict) -> dict:
    """v7 (position reconciliation vs Schwab): a ``reconciliation`` store (last
    report + capped history), a per-position ``needs_review`` freeze flag, and an
    explicit ``live_transmitted`` flag on every execution so the reconciler's
    expected-view can exclude paper positions.

    All additive. ``live_transmitted`` is backfilled from each execution's
    historical ``mode`` (live -> True, logged -> False); executions with no
    recognizable mode are marked None (unknown) and the reconciler excludes them
    from the expected-view rather than guessing. Existing open positions default
    to needs_review=False (nothing verified yet, nothing frozen)."""
    state.setdefault("reconciliation", {"last": None, "history": [], "last_success": None})
    for p in state.get("positions", []):
        p.setdefault("needs_review", False)
        p.setdefault("review", None)
    for e in state.get("executions", []):
        if "live_transmitted" in e:
            continue
        mode = e.get("mode")
        e["live_transmitted"] = True if mode == "live" else False if mode == "logged" else None
    return state


def _v7_to_v8(state: dict) -> dict:
    """v8 (native Web Push): a list of browser/PWA push subscriptions under
    ``alerts.push_subscriptions``, delivered to by the new ``webpush`` channel.
    Additive — seed the empty list so readers never key-error (it fills in as
    devices register via /api/push/subscribe)."""
    state.setdefault("alerts", default_alert_state()).setdefault("push_subscriptions", [])
    return state


def _v8_to_v9(state: dict) -> dict:
    """v9 (live-fill verification): a capped ``order_receipts`` list — one entry
    per filled live order linking its Schwab order id to the committed execution
    ids, written at fill time so fill_verify can diff our record against the
    broker's. Additive; seed the empty list so readers never key-error."""
    state.setdefault("order_receipts", [])
    return state


MIGRATIONS = {
    1: _v1_to_v2,
    2: _v2_to_v3,
    3: _v3_to_v4,
    4: _v4_to_v5,
    5: _v5_to_v6,
    6: _v6_to_v7,
    7: _v7_to_v8,
    8: _v8_to_v9,
}


def migrate(state: dict, state_path: str | None = None) -> tuple[dict, bool]:
    """Upgrade a loaded state dict to CURRENT_VERSION.

    Returns (state, changed) — ``changed`` tells the caller to persist the
    upgraded file so the migration runs once, not on every load.

    When ``state_path`` is given and at least one migration will run, a snapshot
    of the pre-migration file is written to backups/ FIRST. If that snapshot
    can't be written the migration is ABORTED (MigrationAbortedError) and the
    on-disk file is left untouched at its original version — a migration bug on
    live data must always have a rollback point.
    """
    version = int(state.get("schema_version") or 1)
    changed = False
    if version < CURRENT_VERSION and MIGRATIONS.get(version) is not None and state_path is not None:
        import backups
        try:
            snapshot = backups.snapshot_before_migration(state_path, version,
                                                         CURRENT_VERSION, state=state)
        except Exception as e:  # noqa: BLE001 — no rollback point => do not migrate
            logger.critical("aborting migration v%s->v%s: pre-migration snapshot "
                            "failed: %s", version, CURRENT_VERSION, e)
            raise MigrationAbortedError(
                f"pre-migration snapshot failed ({e}); refusing to migrate "
                f"v{version}->v{CURRENT_VERSION} without a rollback point") from e
        logger.info("migrating state v%s->v%s (pre-migration snapshot: %s)",
                    version, CURRENT_VERSION, snapshot)
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
