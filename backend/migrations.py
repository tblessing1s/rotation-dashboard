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

CURRENT_VERSION = 19


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


def _v9_to_v10(state: dict) -> dict:
    """v10 (multi-tranche LEAPs): positions gain ``leap_legs`` — a list of LEAP
    leg dicts keyed by (strike, expiration), with the legacy single ``leap``
    becoming legs[0]. ``leap`` stays in the schema as a mirror of the first leg
    (re-aliased to the same object on every load) so single-leg positions and
    older readers behave exactly as before. Additive only."""
    for p in state.get("positions", []):
        if "leap_legs" not in p:
            p["leap_legs"] = [p["leap"]] if p.get("leap") else []
    return state


def _v10_to_v11(state: dict) -> dict:
    """v11 (multi-condition circuit breaker): the circuit breaker now trips on
    whichever comes first — a 15% drop from entry, 3 closes below the 50-day MA,
    or a close below the 200-day MA (backend/circuit_breaker.py). The drawdown
    leg needs the underlying's ENTRY price; backfill it onto each position's
    stored circuit_breaker from the earliest buy_leap execution's stock_price.

    Additive. Positions with no circuit_breaker, or no locatable entry fill, are
    left as-is — the drawdown leg stays inert (None) while the MA legs still work,
    and the field fills in naturally on the next fresh entry."""
    entry_by_ticker: dict[str, float] = {}
    for e in state.get("executions", []):  # append-only, oldest first
        if e.get("action") != "buy_leap":
            continue
        t = e.get("ticker")
        if t and t not in entry_by_ticker and e.get("stock_price") is not None:
            entry_by_ticker[t] = float(e["stock_price"])
    for p in state.get("positions", []):
        cb = p.get("circuit_breaker")
        if isinstance(cb, dict) and cb.get("entry_price") is None:
            ep = entry_by_ticker.get(p.get("ticker"))
            if ep is not None:
                cb["entry_price"] = round(ep, 2)
    return state


def _v11_to_v12(state: dict) -> dict:
    """v12 (atomic spread roll): the short-call roll's two legs now carry a
    ``roll_group_id`` (the spec's name for the roll linkage) in addition to the
    ledger's ``roll_id``. Backfill roll_group_id = roll_id on historical roll
    executions so legacy legged rolls and new atomic rolls read identically.

    Additive and idempotent: executions that already carry roll_group_id (or have
    no roll_id) are untouched. pending_orders is a free-form dict keyed by order
    id and already represents a two-leg order, so it needs no structural change."""
    for e in state.get("executions", []):
        if e.get("roll_group_id") is None and e.get("roll_id") is not None:
            e["roll_group_id"] = e["roll_id"]
    return state


def _v12_to_v13(state: dict) -> dict:
    """v13 (entry-context snapshots + coded exit reasons): positions gain an
    immutable ``entry_context`` snapshot, frozen at the next FRESH entry. Existing
    positions are seeded None — no snapshot is fabricated from historical bars
    (fabricated training data is worse than missing data). Closed cycles are
    DERIVED (recompute_derived): a cycle whose close_leap carries no recognized
    coded exit_reason becomes ``LEGACY_UNRECORDED`` at the post-migration
    recompute — never backfilled. Additive; executions are never rewritten."""
    for p in state.get("positions", []):
        p.setdefault("entry_context", None)
    return state


def _v13_to_v14(state: dict) -> dict:
    """v14 (weekly theta burn & net juice): every position gains a
    ``planned_exit_dte`` — the DTE the LEAP is planned to be exited/rolled at
    (~130-140 DTE band), off which all burn math keys instead of LEAP expiration.
    Existing positions get ``config.PLANNED_EXIT_DTE`` (the proposed default);
    the operator can override per position later. Additive; executions are never
    rewritten. The realized/projected burn MARK series is telemetry and lives in
    ``DATA_DIR/burn_marks.json`` (like iv_history), NOT in state.json, so nothing
    else is migrated here."""
    import config
    for p in state.get("positions", []):
        p.setdefault("planned_exit_dte", config.PLANNED_EXIT_DTE)
    return state


def _v14_to_v15(state: dict) -> dict:
    """v15 (monthly payout tracking): a ``payouts`` store keyed by month
    ('YYYY-MM') holding only the operator's withdrawal bookkeeping — which months
    were marked paid, when, the amount snapshotted at that moment, and an optional
    note. Net juice per month stays DERIVED from the immutable executions
    (payouts.monthly_net_juice); nothing about income is copied here. Additive —
    seed the empty store so readers never key-error."""
    state.setdefault("payouts", {"records": {}})
    return state


def _v15_to_v16(state: dict) -> dict:
    """v16 (order lifecycle: broker-side cancel state machine): two additive stores
    — an append-only ``order_events`` log (one record per SUBMITTED->…->terminal
    transition, from which recompute_derived derives ``order_state``) and an
    ``order_locks`` map (the per-position-intent resubmission gate, which must
    survive restart so a crash mid-cancel can't orphan a working broker order).
    No live orders exist historically, so both seed empty; executions and the
    existing pending_orders/order_receipts are untouched."""
    state.setdefault("order_events", [])
    state.setdefault("order_locks", {})
    return state


def _v16_to_v17(state: dict) -> dict:
    """v17 (recommendation trust layer): three additive stores —

    - ``recommendations``: append-only, immutable Recommendation records emitted
      by the evaluation pass BEFORE the operator acts (recommendation_engine.py).
    - ``recommendation_overrides``: append-only operator dismissals (coded
      reason + optional note) — the one resolution input that is raw record,
      not derivation, mirroring exit_reason on a close.
    - ``order_fidelity``: per-ticket lifecycle grades. Derived by
      recompute_derived from order_events/receipts/executions, but PERSISTED
      (merge-retain) because order_events is capped at 1000 — a graded verdict
      must survive its events rolling off the log.

    ``metadata.trust_layer_since`` marks activation: executions BEFORE it have
    no recommendations by construction and are excluded from coverage matching
    (they would otherwise all read as coverage misses). Derived keys
    (``recommendation_resolutions``, ``trust_scoreboard``) are rebuilt by the
    post-migration recompute, not seeded here. Executions untouched."""
    state.setdefault("recommendations", [])
    state.setdefault("recommendation_overrides", [])
    state.setdefault("order_fidelity", {})
    meta = state.setdefault("metadata", {})
    if not meta.get("trust_layer_since"):
        from datetime import datetime, timezone
        meta["trust_layer_since"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return state


def _v17_to_v18(state: dict) -> dict:
    """v18 (market-settle execution gate): one additive store —

    - ``spread_baselines``: per-contract trailing bid-ask spread samples, fed from
      quotes the data layer already fetches (no new polling). The gate's
      spread-quality check compares the current spread to this trailing mean;
      until enough samples exist it reports "no baseline" rather than fabricating
      one (spread_monitor.py).

    The PENDING_SETTLE lifecycle lives in an additive, nullable ``settle`` block on
    individual (immutable-core) Recommendation records — a purely additive field
    that needs no per-record rewrite, so it is not seeded here. ``metadata`` gets a
    ``market_settle_gate_since`` marker for parity with the trust-layer marker.
    Executions untouched."""
    state.setdefault("spread_baselines", {})
    meta = state.setdefault("metadata", {})
    if not meta.get("market_settle_gate_since"):
        from datetime import datetime, timezone
        meta["market_settle_gate_since"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return state


def _v18_to_v19(state: dict) -> dict:
    """v19 (transaction ingestion): two additive stores —

    - ``ingested_transactions``: the dedupe ledger, Schwab transaction id -> a
      small record (source app|broker_manual, order_id, execution_ids,
      ingested_at). Makes re-running ingestion idempotent — a transaction is
      ingested at most once (INGESTION_IS_GROUND_TRUTH).
    - ``ingestion``: the last ingestion report summary + the list of open
      out-of-band ``proposals`` awaiting one-click operator adoption
      (NO_AUTO_REMEDIATION — the app never auto-books an out-of-band trade).

    Executions untouched; the new ``source``/``transaction_id`` execution fields
    are additive and nullable, so no per-record rewrite is needed."""
    state.setdefault("ingested_transactions", {})
    state.setdefault("ingestion", {"last": None, "last_success": None, "proposals": []})
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
    9: _v9_to_v10,
    10: _v10_to_v11,
    11: _v11_to_v12,
    12: _v12_to_v13,
    13: _v13_to_v14,
    14: _v14_to_v15,
    15: _v15_to_v16,
    16: _v16_to_v17,
    17: _v17_to_v18,
    18: _v18_to_v19,
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
