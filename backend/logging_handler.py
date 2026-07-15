"""state.json is the single source of truth.

Every execution is appended here with a timestamp and captured prices; the theta
ledger and extrinsic-payback meters are *derived* from the executions/positions
so nothing is ever hand-maintained. Writes are atomic (temp file + fsync +
rename + dir fsync) and guarded by a process lock — this is a single-writer
store (run one machine). See ``_atomic_write`` for the durability contract and
``docs/recovery.md`` for the corrupt-state runbook.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import config
import migrations

logger = logging.getLogger("cfm.alerts")

_lock = threading.RLock()


class StateCorruptError(RuntimeError):
    """Raised on load when an existing state file can't be parsed. We refuse to
    silently re-initialize empty state over a live trading record — the operator
    must restore from a backup (see docs/recovery.md)."""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_state() -> dict:
    return {
        "schema_version": migrations.CURRENT_VERSION,
        "metadata": {
            "last_updated": utcnow(),
            "reserve_required": config.RESERVE_REQUIRED,
            "capital_deployed": 0,
            "operating_cash": 0,
            # Trust-layer activation boundary: executions before this instant
            # predate the recommendation engine and are excluded from coverage
            # matching (they would otherwise all read as coverage misses).
            "trust_layer_since": utcnow(),
        },
        "positions": [],
        "executions": [],
        "theta_ledger": {"weeks": [], "totals": {"this_week": 0, "this_month": 0, "ytd": 0, "pct_deployed": 0}},
        "extrinsic_payback": {},
        "roll_ledger": {"rolls": [], "by_ticker": {}},
        "cycles": [],
        # Monthly payout bookkeeping: month ('YYYY-MM') -> {paid, paid_at,
        # paid_amount, note}. Income per month is DERIVED from executions
        # (payouts.py); only the operator's withdrawal record lives here.
        "payouts": {"records": {}},
        # Live orders placed at the broker but not yet filled. Keyed by Schwab
        # order id; an entry is removed when the order fills (then committed as an
        # execution) or is cancelled/rejected.
        "pending_orders": {},
        # Append-only order-lifecycle event log: one record per state transition
        # (SUBMITTED->WORKING->…terminal) with the Schwab orderId, prior/new coded
        # state, and raw broker status. recompute_derived() derives order_state
        # from this; the log itself is never mutated. See order_lifecycle.py.
        "order_events": [],
        # Per-position-intent order lock (the resubmission gate, rule 5). Keyed by
        # "TICKER:intent"; survives restart so a crash mid-cancel can't orphan a
        # working broker order invisibly or let a double order through.
        "order_locks": {},
        "alerts": migrations.default_alert_state(),
        # Position reconciliation vs Schwab: last report + capped history.
        "reconciliation": {"last": None, "history": [], "last_success": None},
        # Execution ingestion from the Schwab transactions endpoint (spec §4).
        # ingested_transactions is the dedupe ledger (Schwab transaction id ->
        # record) so re-runs are idempotent; ingestion holds the last report
        # summary + open out-of-band adoption proposals. See transaction_ingest.py.
        "ingested_transactions": {},
        "ingestion": {"last": None, "last_success": None, "proposals": []},
        # Recommendation trust layer (schema v17). recommendations and
        # recommendation_overrides are append-only and immutable once written;
        # recommendation_resolutions and trust_scoreboard are DERIVED by
        # recompute_derived; order_fidelity is derived-then-retained (the
        # order_events source is capped, graded verdicts must outlive it).
        "recommendations": [],
        "recommendation_overrides": [],
        "order_fidelity": {},
    }


def leap_legs(position: dict) -> list[dict]:
    """The position's LEAP legs as a list — the multi-tranche source of truth.
    Falls back to wrapping the legacy single ``leap`` so states built before
    v10 (and test fixtures that never went through load_state) still work."""
    legs = position.get("leap_legs")
    if legs is not None:
        return legs
    return [position["leap"]] if position.get("leap") else []


def _sync_leap_alias(state: dict) -> None:
    """Re-bind position["leap"] to leap_legs[0] AFTER a JSON round-trip.

    In memory ``leap`` and ``leap_legs[0]`` are the same dict, so mutations
    through either name stay coherent — but serialization writes them as two
    independent copies, so every load must restore the aliasing or a mark
    refresh through one name would silently diverge from the other."""
    for p in state.get("positions", []):
        legs = p.get("leap_legs")
        if legs is None:
            p["leap_legs"] = [p["leap"]] if p.get("leap") else []
        else:
            p["leap"] = legs[0] if legs else None


def load_state() -> dict:
    with _lock:
        path = config.active_state_path()
        if not os.path.exists(path):
            state = _default_state()
            # Seed the derived keys (trust scoreboard, order_state, ledgers) so
            # a fresh store's readers never key-error before the first append.
            recompute_derived(state)
            _write(state)
            return state
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (ValueError, OSError) as e:
            # DO NOT silently re-initialize: empty state over a live trading
            # record is unrecoverable. Log CRITICAL, point at the newest backup,
            # and refuse to continue (crashes the worker == the app won't serve
            # bad state). Recovery: scripts/restore_state.py — see docs/recovery.md.
            import backups
            latest = backups.latest_backup()
            hint = (f"most recent backup: {latest}" if latest
                    else "NO backups found in " + backups.backups_dir())
            logger.critical("state file %s is corrupt/unreadable (%s); refusing to "
                            "start with empty state — %s", path, e, hint)
            raise StateCorruptError(
                f"{path} is corrupt or unreadable ({e}). Refusing to overwrite a "
                f"live trading record with empty state. {hint}. "
                f"Restore with scripts/restore_state.py (see docs/recovery.md).") from e
        # Versioned migrations first (they add structure old files lack), then
        # forward-fill any still-missing top-level keys so older state files load.
        # migrate() snapshots the pre-migration file to backups/ before touching
        # it and aborts (raises) if that snapshot can't be written.
        state, migrated = migrations.migrate(state, state_path=path)
        for k, v in _default_state().items():
            state.setdefault(k, v)
        _sync_leap_alias(state)
        if migrated:
            # Rebuild the derived ledgers so migration-added derived structures
            # (e.g. the roll ledger) are populated from day one, not first-write.
            recompute_derived(state)
            _write(state)
        return state


def _serialize(state: dict) -> str:
    """Serialize to a string FIRST, so a serialization error (unencodable value)
    raises before any file is touched and can never truncate the real file."""
    return json.dumps(state, indent=2)


def _atomic_write(path: str, payload: str) -> None:
    """Durable, crash-safe replace of ``path`` with ``payload``.

    Contract (POSIX / ext4): write to a uniquely-named temp file *in the same
    directory* (same filesystem — cross-fs os.replace is not atomic), flush +
    fsync the temp file so its bytes hit disk, os.replace() it over the target
    (atomic rename), then fsync the *directory* so the rename itself is durable.
    On any error the temp file is removed and the original is left untouched.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory,
                               prefix=os.path.basename(path) + ".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # fsync the directory so the rename (a directory-metadata change) is durable
    # across a crash — without this, ext4 can lose the rename after a power cut.
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:  # some filesystems disallow directory fsync — best effort
        pass


def _write(state: dict) -> None:
    # Serialize BEFORE opening any file so an unserializable value aborts cleanly.
    _atomic_write(config.active_state_path(), _serialize(state))


def cleanup_orphan_temp_files() -> list[str]:
    """Remove ``*.tmp.*`` temp files left by a write that crashed between
    mkstemp and os.replace. Safe: a live temp file only exists for microseconds
    inside a held lock, so anything on disk at startup is orphaned. Returns the
    paths removed (logged by the caller)."""
    removed: list[str] = []
    for base in (config.STATE_PATH, config.DEMO_STATE_PATH):
        directory = os.path.dirname(base) or "."
        pattern = os.path.join(directory, os.path.basename(base) + ".tmp.*")
        for orphan in glob.glob(pattern):
            try:
                os.remove(orphan)
                removed.append(orphan)
            except OSError:
                pass
    if removed:
        logger.warning("startup: removed %d orphaned state temp file(s): %s",
                       len(removed), ", ".join(removed))
    return removed


def startup_check() -> None:
    """Run once at app startup: clear orphaned temp files, then eagerly load the
    active store so a corrupt file fails fast (StateCorruptError) instead of on
    the first request. Only touches the ACTIVE store."""
    cleanup_orphan_temp_files()
    load_state()  # raises StateCorruptError on a corrupt live file → refuse to start


def save_state(state: dict) -> dict:
    with _lock:
        state.setdefault("metadata", {})["last_updated"] = utcnow()
        _write(state)
        return state


def restore_from_backup(backup_path: str) -> dict:
    """Restore ``backup_path`` onto the active state file via the atomic save
    path (never a raw copy), writing the current (possibly corrupt) file aside
    first. Used by scripts/restore_state.py. Returns a small report."""
    with _lock:
        target = config.active_state_path()
        with open(backup_path, encoding="utf-8") as fh:
            payload = fh.read()
        json.loads(payload)  # validate the backup parses before we touch anything
        pre_restore = None
        if os.path.exists(target):
            pre_restore = f"{target}.pre-restore.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(target, pre_restore)
        _atomic_write(target, payload)
        logger.warning("restored state from %s -> %s (previous saved aside as %s)",
                       backup_path, target, pre_restore)
        return {"restored": target, "from": backup_path, "pre_restore": pre_restore}


def _next_exec_id(state: dict) -> str:
    return f"exec_{len(state.get('executions', [])) + 1:03d}"


def append_execution(execution: dict) -> dict:
    """Append one execution, assign id/timestamp, then recompute all derived
    ledgers. Returns the stored execution record."""
    with _lock:
        state = load_state()
        execution = dict(execution)
        execution.setdefault("id", _next_exec_id(state))
        execution.setdefault("date", utcnow())
        # Explicit live-transmission provenance for the reconciler's expected-view
        # (paper positions won't exist at the broker). Derived from the execution's
        # own mode: live -> True, logged -> False, anything else -> None (unknown).
        if "live_transmitted" not in execution:
            mode = execution.get("mode")
            execution["live_transmitted"] = (
                True if mode == "live" else False if mode == "logged" else None)
        state["executions"].append(execution)
        recompute_derived(state)
        save_state(state)
        return execution


def find_position(state: dict, ticker: str) -> dict | None:
    for p in state.get("positions", []):
        if p.get("ticker", "").upper() == ticker.upper():
            return p
    return None


# ---------------------------------------------------------------------------
# Pending (live, unfilled) orders
# ---------------------------------------------------------------------------
def save_pending_order(order_id: str, record: dict) -> None:
    with _lock:
        state = load_state()
        state.setdefault("pending_orders", {})[str(order_id)] = record
        save_state(state)


def save_order_receipt(receipt: dict, cap: int = 200) -> None:
    """Append one broker fill receipt (order_id + committed execution ids) to a
    capped list. Written at fill time so the live-order path can later be
    verified against Schwab's own record (see fill_verify.py)."""
    with _lock:
        state = load_state()
        receipts = state.setdefault("order_receipts", [])
        receipts.append(receipt)
        del receipts[:-cap]  # keep newest
        save_state(state)


def get_pending_order(order_id: str) -> dict | None:
    return load_state().get("pending_orders", {}).get(str(order_id))


def pop_pending_order(order_id: str) -> dict | None:
    with _lock:
        state = load_state()
        rec = state.get("pending_orders", {}).pop(str(order_id), None)
        save_state(state)
        return rec


def list_pending_orders() -> dict:
    """A shallow copy of the live pending-orders map (order_id -> record). Used by
    startup reconciliation to re-poll every unresolved order against Schwab."""
    return dict(load_state().get("pending_orders", {}))


# ---------------------------------------------------------------------------
# Durable order-submission records (incident hotfix, D2/D4/F3)
# ---------------------------------------------------------------------------
# Keyed by an app-generated client_order_ref created BEFORE the broker call, so a
# record exists even when the ack has no orderId or the response is lost — the only
# structure that survives a header-less ack or a timeout. This is the F3 idempotency
# key (a repeat submit for the same ref returns this record, never re-submits) and
# the durable home for the orderId (ORDERID_PERSIST_FIRST). Co-located in state.json
# next to pending_orders/order_events/order_locks (all operational, not execution,
# records); the forthcoming lifecycle system adopts it as its client-ref index.
def save_order_submission(client_order_ref: str, record: dict) -> None:
    """Create/replace the durable submission record for ``client_order_ref``. Written
    BEFORE the broker call (status SUBMITTING) so nothing is lost if the call faults."""
    with _lock:
        state = load_state()
        record = dict(record)
        record.setdefault("created_at", utcnow())
        record["updated_at"] = utcnow()
        state.setdefault("order_submissions", {})[str(client_order_ref)] = record
        save_state(state)


def update_order_submission(client_order_ref: str, **fields) -> dict | None:
    """Merge ``fields`` into an existing submission record and re-stamp updated_at.
    Returns the updated record, or None if the ref is unknown. Used to write the
    orderId FIRST on a response, then the resolved status."""
    with _lock:
        state = load_state()
        subs = state.setdefault("order_submissions", {})
        rec = subs.get(str(client_order_ref))
        if rec is None:
            return None
        rec = dict(rec)
        rec.update(fields)
        rec["updated_at"] = utcnow()
        subs[str(client_order_ref)] = rec
        save_state(state)
        return rec


def get_order_submission(client_order_ref: str) -> dict | None:
    return load_state().get("order_submissions", {}).get(str(client_order_ref))


def list_order_submissions() -> dict:
    return dict(load_state().get("order_submissions", {}))


# ---------------------------------------------------------------------------
# Order-lifecycle event log + per-position resubmission lock
# ---------------------------------------------------------------------------
def append_order_event(event: dict) -> dict:
    """Append one immutable order-lifecycle transition and recompute derived state.

    Every state change (place, poll, cancel-request, terminal) writes one of these
    with the Schwab orderId, prior/new coded state, and raw broker status, so the
    current order state is a pure replay of the log (never a mutated field). A crash
    between two events leaves a consistent prefix, not a half-written status."""
    with _lock:
        state = load_state()
        event = dict(event)
        event.setdefault("at", utcnow())
        events = state.setdefault("order_events", [])
        event.setdefault("seq", len(events) + 1)
        events.append(event)
        del events[:-_ORDER_EVENT_CAP]  # bounded; order_receipts is the long audit trail
        recompute_derived(state)
        save_state(state)
        return event


_ORDER_EVENT_CAP = 1000


# ---------------------------------------------------------------------------
# Recommendation trust layer (append-only writers; resolutions are derived)
# ---------------------------------------------------------------------------
def _next_rec_id(state: dict) -> str:
    return f"rec_{len(state.get('recommendations', [])) + 1:05d}"


def append_recommendations(recs: list[dict]) -> list[dict]:
    """Append one evaluation pass's Recommendation records (already fully built
    by the pure engine), assign monotonic ids, then recompute derived state so
    resolutions/scoreboard reflect them immediately. Records are IMMUTABLE once
    written — resolution status lives in the derived recommendation_resolutions,
    never on the record. Returns the stored records (with ids)."""
    if not recs:
        return []
    with _lock:
        state = load_state()
        stored = []
        for rec in recs:
            rec = dict(rec)
            rec.setdefault("rec_id", _next_rec_id(state))
            rec.setdefault("emitted_at", utcnow())
            state.setdefault("recommendations", []).append(rec)
            stored.append(rec)
        recompute_derived(state)
        save_state(state)
        return stored


def append_recommendation_override(override: dict) -> dict:
    """Append one operator dismissal (coded reason + optional note) — the only
    hand-authored input to resolution status, mirroring the typed-override /
    exit_reason pattern. Validation of the coded reason happens at the API
    layer; this writer only persists and recomputes."""
    with _lock:
        state = load_state()
        override = dict(override)
        override.setdefault("id", f"rov_{len(state.get('recommendation_overrides', [])) + 1:05d}")
        override.setdefault("at", utcnow())
        state.setdefault("recommendation_overrides", []).append(override)
        recompute_derived(state)
        save_state(state)
        return override


def get_order_lock(intent_key: str) -> dict | None:
    return load_state().get("order_locks", {}).get(str(intent_key))


def save_order_lock(intent_key: str, lock: dict) -> None:
    with _lock:
        state = load_state()
        state.setdefault("order_locks", {})[str(intent_key)] = lock
        save_state(state)


# ---------------------------------------------------------------------------
# Derived ledgers (theta ledger + extrinsic payback)
# ---------------------------------------------------------------------------
UNDATED = "undated"  # week/period label for a fill whose date can't be parsed


def _parse_ymd(date_str) -> datetime | None:
    """Parse a ``YYYY-MM-DD`` prefix to a datetime, or None when it isn't one.
    Strict on purpose: it never substitutes a fallback date, so an undated fill
    stays visibly undated instead of being silently attributed to today."""
    try:
        return datetime.strptime(str(date_str or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def bucket_datetime(e: dict) -> datetime | None:
    """The datetime an execution is bucketed under for the week/month ledgers: its
    stamped ``date``, falling back to its option ``expiration`` (a close belongs to
    the period it expired in), else None when neither parses. The theta ledger AND
    the payout view both key off this one helper, so they can never disagree about
    which period a fill lands in."""
    return _parse_ymd(e.get("date")) or _parse_ymd(e.get("expiration"))


def _iso_week(date_str: str) -> str:
    """ISO ``YYYY-Www`` for a date string, or ``UNDATED`` when it can't be parsed.
    For executions prefer ``bucket_datetime(e)`` so the date->expiration fallback
    applies; this raw-string form has no fallback."""
    dt = _parse_ymd(date_str)
    return f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}" if dt else UNDATED


def _strike_key(strike):
    """Normalize a strike for FIFO short-leg pairing (float-safe, str/None tolerant)."""
    try:
        return round(float(strike), 4)
    except (TypeError, ValueError):
        return None


def close_economics(e: dict) -> tuple[float, float, float]:
    """(extrinsic_sold_ps, extrinsic_paid_back_ps, net_juice_total) for a
    close_short, DERIVED from its stored facts — close price, underlying, strike,
    entry extrinsic — so net juice can never go stale against the editable stock
    price (the bug where a close whose stock was later set OTM still showed the
    buyback as $0 paid back, booking full extrinsic as juice).

    extrinsic paid back = close price − intrinsic at the close (0 when OTM);
    net juice = (entry extrinsic − extrinsic paid back) × contracts × 100. Falls
    back to the stored fields when the facts needed to derive aren't all present
    (legacy/partial rows), so nothing regresses."""
    c = int(e.get("contracts") or 0)
    sold_ps = float(e.get("extrinsic_sold") or 0)
    cps = e.get("close_price_per_share")
    stock = e.get("stock_price")
    strike = e.get("strike")
    if (e.get("extrinsic_sold") is not None and cps is not None
            and stock is not None and strike is not None and c):
        paid_ps = max(float(cps) - max(float(stock) - float(strike), 0.0), 0.0)
        return sold_ps, round(paid_ps, 4), round((sold_ps - paid_ps) * c * 100, 2)
    return sold_ps, float(e.get("extrinsic_paid_back") or 0), float(e.get("net_juice_total") or 0)


def intrinsic_melt_by_close(execs: list[dict]) -> dict[int, float]:
    """The intrinsic that melted per ``close_short``, keyed by the close's index in
    ``execs`` — the ONE source of truth the theta ledger AND the payout view both
    call, so a close's intrinsic can never read one way in History and another in
    Payouts.

    A short sold ITM (entry stock above the strike) hedges the covering LEAP: the
    short's intrinsic offsets the LEAP's dollar-for-dollar as the stock moves. If
    the stock then falls THROUGH the strike and the short closes OTM, the part of
    the drop BELOW the strike is no longer hedged — the LEAP kept losing intrinsic
    but the (now-worthless) short stopped offsetting it. That unhedged loss is
    ``max(strike − exit_stock, 0) × contracts × 100`` — how far past the strike the
    stock closed — and net juice must cover it before it's income.

    Only counts when the short was ITM at entry (paired FIFO from the matching
    ``sell_short`` per (ticker, strike)) AND closed OTM: a leg sold OTM never had a
    hedge to lose, and a leg that closed still ITM hasn't given anything back. A
    close with no pairable open or no exit stock contributes 0."""
    open_shorts: dict[tuple, list[list]] = {}   # (ticker,strike) -> [[entry_stock, contracts], ...]
    out: dict[int, float] = {}
    for i, e in enumerate(execs):
        action = e.get("action")
        ticker = e.get("ticker")
        sk = _strike_key(e.get("strike"))
        if action == "sell_short":
            if sk is None:
                continue
            n = int(e.get("contracts") or 0)
            if n > 0:
                open_shorts.setdefault((ticker, sk), []).append([e.get("stock_price"), n])
            continue
        if action != "close_short" or sk is None:
            continue
        strike = float(sk)
        exit_stock = e.get("stock_price")
        # OTM at close, and how far past the strike it landed (the unhedged part).
        otm = exit_stock is not None and float(exit_stock) < strike
        overshoot = (strike - float(exit_stock)) if otm else 0.0
        need = int(e.get("contracts") or 0)
        queue = open_shorts.get((ticker, sk)) or []
        melted = 0.0
        while need > 0 and queue:
            entry_stock, avail = queue[0]
            take = min(need, avail)
            # Was ITM at entry (a hedge existed) and closed OTM (gave it back).
            if otm and entry_stock is not None and float(entry_stock) > strike:
                melted += overshoot * take * 100
            avail -= take
            need -= take
            if avail <= 0:
                queue.pop(0)
            else:
                queue[0][1] = avail
        if melted > 0:
            out[i] = round(melted, 2)
    return out


def validate_payback(execs: list[dict]) -> list[dict]:
    """Integrity check on the cycle-scoped payback state machine's INPUTS, so a
    corrupt/mislabeled execution log can't silently produce a plausible-but-wrong
    payback target. Pure over the executions; returns a list of issue dicts
    (empty == clean). VALIDATION ONLY — it does not change the meter or the state
    machine (R5).

    Detects the three ways the replay (recompute_derived) goes silently wrong:
      * dangling_leap_roll — a close_leap latched a leap_roll_id that NO following
        buy_leap consumed (an aborted/half-logged roll, or a dropped buy leg).
        The latch never clears, so cycle_collected/target carry a phantom target
        forever.
      * orphan_roll_buy — a buy_leap carried a leap_roll_id with no matching
        pending close (out-of-order or a mislabeled leg); the replay silently
        demotes it to a fresh cycle / add, dropping the carry.
      * legs_remaining_mismatch — a close_leap's stamped legs_remaining disagrees
        with the leg count derived from the execution history, so the true-exit
        branch fires early (wipes a live cycle) or never (cycle never ends).
    """
    issues: list[dict] = []
    pending_close_roll: dict[str, tuple[str, int]] = {}   # ticker -> (rid, set-at index)
    last_idx: dict[str, int] = {}                         # ticker -> index of its last execution
    open_legs: dict[str, int] = {}   # per-ticker LEAP leg COUNT (merge adds no leg)

    def _as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    for i, e in enumerate(execs):
        t = e.get("ticker", "")
        a = e.get("action")
        last_idx[t] = i
        if a == "buy_leap":
            rid = e.get("leap_roll_id")
            if rid:
                if pending_close_roll.get(t, (None,))[0] == rid:
                    pending_close_roll.pop(t, None)
                    open_legs[t] = open_legs.get(t, 0) + 1   # roll: close(-1)+buy(+1)
                else:
                    issues.append({"type": "orphan_roll_buy", "ticker": t,
                                   "detail": f"buy_leap leap_roll_id={rid} has no matching close_leap latch"})
                    open_legs[t] = open_legs.get(t, 0) + 1
            elif e.get("leap_add") == "merge":
                open_legs[t] = open_legs.get(t, 0) or 1      # scale-in: no new leg
            else:
                open_legs[t] = open_legs.get(t, 0) + 1       # fresh cycle or new leg (add)
        elif a == "close_leap":
            rid = e.get("leap_roll_id")
            before = open_legs.get(t, 0)
            expected = max(before - 1, 0)
            if rid:
                pending_close_roll[t] = (rid, i)             # a roll IF a buy follows
            else:
                # A true/partial close: the stamped legs_remaining must match the
                # count the history implies. (Roll closes carry a leap_roll_id and
                # are validated by the latch, not this count.) Non-numeric stamps
                # are skipped, never raised on.
                stamped = e.get("legs_remaining")
                stamped_int = _as_int(stamped)
                if stamped_int is not None and stamped_int != expected:
                    issues.append({"type": "legs_remaining_mismatch", "ticker": t,
                                   "detail": (f"close_leap stamped legs_remaining={stamped} but the "
                                              f"execution history implies {expected}")})
            open_legs[t] = expected
    # A latch still set at the end is a roll whose buy leg never arrived. BUT the
    # executor appends the roll's close_leap and buy_leap as two separate appends
    # (each recomputes), so a legitimately IN-PROGRESS roll is momentarily latched
    # with the close as the last execution for that ticker — that is not a
    # corruption. Only flag when later activity for the SAME ticker exists without
    # the buy ever consuming the latch (a genuinely orphaned/aborted roll).
    for t, (rid, idx) in pending_close_roll.items():
        if last_idx.get(t, idx) > idx:
            issues.append({"type": "dangling_leap_roll", "ticker": t,
                           "detail": f"close_leap leap_roll_id={rid} was never consumed by a buy_leap"})
    return issues


def derived_executions(state: dict) -> list[dict]:
    """The executions that feed the DERIVED views — the theta ledger AND the payout
    view — with the ones that must leave no trace filtered out:

    * ``reversed_by`` — an adoption that was later undone, and
    * ``reverses_execution_id`` — the ``adoption_reversal`` marker itself, and
    * ``excluded`` — a fill the operator has manually excluded.

    A reversed/excluded execution must read the same everywhere: absent from the
    per-week theta ledger AND absent from the monthly payout, so History and Payouts
    can't disagree about it. Its immutable record stays on the log for the audit
    trail (append-only, never rewritten)."""
    return [e for e in state.get("executions", [])
            if not e.get("reversed_by") and not e.get("reverses_execution_id")
            and not e.get("excluded")]


def recompute_derived(state: dict) -> dict:
    """Rebuild theta_ledger + extrinsic_payback from executions/positions.

    Executions annotated ``reversed_by`` (an adoption that was undone) and the
    ``adoption_reversal`` markers themselves are excluded from the derived replay —
    a reversed adoption must leave no trace in the ledgers, exactly as if it never
    happened, while the immutable records of both the adoption and its reversal are
    preserved on the log for the audit trail (append-only, never rewritten)."""
    execs = derived_executions(state)
    now = datetime.now(timezone.utc)
    cur_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    cur_month = now.strftime("%Y-%m")
    cur_year = now.strftime("%Y")

    weeks: dict[tuple[str, str], dict] = {}
    totals = {"this_week": 0.0, "this_month": 0.0, "ytd": 0.0}

    # Per-close melted intrinsic (ITM→OTM), shared with the payout view so the two
    # tabs agree line for line. Gated by config so the whole feature toggles as one.
    import config as _config
    melt_by_idx = (intrinsic_melt_by_close(execs)
                   if getattr(_config, "PAYOUT_INTRINSIC_REPAYMENT", True) else {})

    for idx, e in enumerate(execs):
        if e.get("action") != "close_short":
            continue
        ticker = e.get("ticker", "")
        # Bucket by the fill's date, falling back to its expiration; an undated
        # close lands in the UNDATED bucket (visible in the per-week table) rather
        # than being silently counted as this week's juice.
        when = bucket_datetime(e)
        wk = f"{when.isocalendar()[0]}-W{when.isocalendar()[1]:02d}" if when else UNDATED
        # Re-derive the close economics from its stored facts so a stale stored
        # net juice (e.g. a roll whose buyback booked as $0 paid back) can't leak
        # into the ledger — the editable stock price is the source of truth.
        sold_ps, paid_ps, net = close_economics(e)
        c = int(e.get("contracts") or 0)
        key = (wk, ticker)
        row = weeks.setdefault(key, {"week": wk, "ticker": ticker,
                                     "extrinsic_sold": 0.0, "extrinsic_paid_back": 0.0,
                                     "net_juice": 0.0, "intrinsic_covered": 0.0,
                                     "net_juice_after_intrinsic": 0.0})
        row["extrinsic_sold"] += sold_ps * c * 100
        row["extrinsic_paid_back"] += paid_ps * c * 100
        row["net_juice"] += net
        # When this short went ITM→OTM, the melted intrinsic must be covered before
        # the week's extrinsic juice is income — so the per-week net juice nets it
        # out (can go negative). net_juice itself stays the raw extrinsic capture so
        # coverage/target metrics elsewhere are unaffected.
        row["intrinsic_covered"] += melt_by_idx.get(idx, 0.0)

        # Live totals count only fills we can place in time (same date->expiration
        # rule), so they stay consistent with the payout view.
        if when:
            if wk == cur_week:
                totals["this_week"] += net
            if when.strftime("%Y-%m") == cur_month:
                totals["this_month"] += net
            if when.strftime("%Y") == cur_year:
                totals["ytd"] += net

    import position_manager  # deferred: derive deployed from open positions
    deployed = position_manager.deployed_capital(state)
    totals["pct_deployed"] = round(totals["ytd"] / deployed, 4) if deployed else 0
    for k in ("this_week", "this_month", "ytd"):
        totals[k] = round(totals[k], 2)
    for row in weeks.values():
        row["intrinsic_covered"] = round(row["intrinsic_covered"], 2)
        # The per-week bottom line after covering ITM→OTM intrinsic (may be < 0).
        row["net_juice_after_intrinsic"] = round(row["net_juice"] - row["intrinsic_covered"], 2)
    state["theta_ledger"] = {
        "weeks": sorted(weeks.values(), key=lambda r: (r["week"], r["ticker"])),
        "totals": totals,
    }

    # LEAP-cycle payback accounting (respecting long-leg rolls). A LEAP roll is
    # logged as close_leap + buy_leap sharing a leap_roll_id; across a roll the
    # position's capital story is CONTINUOUS — collected juice carries and the
    # new LEAP's entry extrinsic is ADDED to the outstanding payback target. A
    # true exit + re-entry (no shared id) starts a fresh cycle. This is why the
    # meter is cycle-scoped rather than summing a ticker's whole juice history
    # against only the current LEAP's entry extrinsic. See docs/leap-lifecycle.md.
    cycle_collected: dict[str, float] = {}
    cycle_target: dict[str, float] = {}
    _pending_close_roll: dict[str, str] = {}
    for e in execs:
        t = e.get("ticker", "")
        a = e.get("action")
        if a == "buy_leap":
            rid = e.get("leap_roll_id")
            extr = float(e.get("extrinsic_captured") or 0)
            if rid and _pending_close_roll.get(t) == rid:
                cycle_target[t] = cycle_target.get(t, 0.0) + extr   # roll: add extrinsic, carry juice
            elif e.get("leap_add") in ("merge", "add"):
                # Multi-tranche add to a running engine (scale-in or a new leg):
                # the payback target grows by the new extrinsic bought; collected
                # juice carries — the cycle is continuous, exactly like a roll.
                cycle_collected.setdefault(t, 0.0)
                cycle_target[t] = cycle_target.get(t, 0.0) + extr
            else:
                cycle_collected[t] = 0.0                            # fresh cycle
                cycle_target[t] = extr
            _pending_close_roll.pop(t, None)
        elif a == "close_short":
            cycle_collected[t] = cycle_collected.get(t, 0.0) + float(e.get("net_juice_total") or 0)
        elif a == "close_leap":
            rid = e.get("leap_roll_id")
            if rid:
                _pending_close_roll[t] = rid    # a roll IF a matching buy_leap follows
            elif int(e.get("legs_remaining") or 0) > 0:
                # One leg of a multi-tranche engine closed; others still run, so
                # the cycle (and its target — that extrinsic WAS bought this
                # cycle) carries on the surviving legs.
                pass
            else:
                cycle_collected.pop(t, None)     # true exit — cycle ends, juice does not carry
                cycle_target.pop(t, None)
                _pending_close_roll.pop(t, None)

    # Extrinsic payback meter per position: how much of the LEAP's entry
    # extrinsic the collected short juice has paid back (current cycle only).
    payback: dict[str, dict] = {}
    agg_at_entry = agg_collected = agg_remaining = 0.0
    for p in state.get("positions", []):
        ticker = p.get("ticker", "")
        leap = p.get("leap") or {}
        at_entry = float(cycle_target.get(ticker, leap.get("extrinsic_at_entry") or 0))
        collected = float(cycle_collected.get(ticker, 0.0))
        remaining = max(at_entry - collected, 0.0)
        payback[ticker] = {
            "leap_extrinsic_at_entry": round(at_entry, 2),
            "collected_to_date": round(collected, 2),
            "remaining_to_payback": round(remaining, 2),
            "pct_complete": round(collected / at_entry * 100, 1) if at_entry else 0,
        }
        # keep the position's own running tally in sync
        if leap:
            leap["extrinsic_collected_to_date"] = round(collected, 2)
        # Aggregate only positions still carrying LEAP extrinsic to recover —
        # this is the income hurdle the book must clear to be net-positive.
        if at_entry > 0:
            agg_at_entry += at_entry
            agg_collected += collected
            agg_remaining += remaining
    state["extrinsic_payback"] = payback

    # Payback-input integrity: flag (never raise into the recompute path) any
    # corrupt/mislabeled execution log that would make the meter above
    # plausible-but-wrong. Loud, not silent — surfaced on the derived state and
    # logged; resolution is the operator's (validation only, R5). The whole check
    # is guarded so a pathological execution can never break recompute_derived.
    try:
        payback_issues = validate_payback(execs)
    except Exception as exc:  # noqa: BLE001 — validation must never break the recompute
        logger.warning("payback validation raised (%s); treating as unvalidated", exc)
        payback_issues = []
    state["payback_reconciliation"] = {"ok": not payback_issues, "issues": payback_issues}
    if payback_issues:
        logger.warning("payback state-machine reconciliation found %d issue(s): %s",
                       len(payback_issues), payback_issues)

    # Book-wide income hurdle: the LEAP extrinsic folded into the ledger so the
    # net juice is only "real" income once the LEAP extrinsic is paid off.
    state["theta_ledger"]["extrinsic_summary"] = {
        "leap_extrinsic_at_entry": round(agg_at_entry, 2),
        "collected_to_date": round(agg_collected, 2),
        "remaining_to_payback": round(agg_remaining, 2),
        "net_income": round(agg_collected - agg_at_entry, 2),
        "income_positive": agg_at_entry > 0 and agg_remaining <= 0,
    }

    # Roll-cost / whipsaw ledger — derived from the paired roll executions
    # (executor stamps both legs with roll_id + roll_reason). This is the data
    # that later validates 1.5x vs 2x ATR strike placement.
    rolls: dict[str, dict] = {}
    for e in execs:
        rid = e.get("roll_id")
        if not rid:
            continue
        entry = rolls.setdefault(rid, {
            "roll_id": rid, "ticker": e.get("ticker", ""), "date": e.get("date", ""),
            "reason": e.get("roll_reason") or "scheduled",
            "from_strike": None, "to_strike": None,
            "buyback_cost": None, "new_premium": None, "net": None,
        })
        if e.get("action") == "close_short":
            entry["buyback_cost"] = float(e.get("close_total") or 0)
            entry["from_strike"] = e.get("strike")
            entry["date"] = e.get("date", entry["date"])
        elif e.get("action") == "sell_short":
            entry["new_premium"] = float(e.get("premium_total") or 0)
            entry["to_strike"] = e.get("strike")
    by_ticker: dict[str, dict] = {}
    for entry in rolls.values():
        if entry["buyback_cost"] is not None and entry["new_premium"] is not None:
            entry["net"] = round(entry["new_premium"] - entry["buyback_cost"], 2)
        agg = by_ticker.setdefault(entry["ticker"], {"count": 0, "net_total": 0.0,
                                                     "drag_total": 0.0})
        agg["count"] += 1
        if entry["net"] is not None:
            agg["net_total"] = round(agg["net_total"] + entry["net"], 2)
            if entry["net"] < 0:  # drag = the debits paid rolling defensively
                agg["drag_total"] = round(agg["drag_total"] + entry["net"], 2)
    state["roll_ledger"] = {
        "rolls": sorted(rolls.values(), key=lambda r: r["date"]),
        "by_ticker": by_ticker,
    }

    # Closed-cycle records — one immutable summary per buy_leap -> close_leap
    # window, entirely derived from the executions between them. The entry
    # context snapshot and the coded exit reason live ON those executions
    # (frozen at trade time; they can't be reconstructed later), so this
    # recompute is deterministic and idempotent — it only COPIES them onto the
    # derived cycle, never regenerates them (they are raw record, like the
    # executions themselves). See docs/entry-context-audit.md §5.
    import exit_reasons  # deferred: coded exit-reason enum + validation
    import entry_context  # deferred: compact snapshot summary for the cycle

    def _parse_day(s):
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    cycles: list[dict] = []
    open_cycle: dict[str, dict] = {}
    for e in execs:
        t = e.get("ticker", "")
        a = e.get("action")
        if a == "buy_leap":
            open_cycle[t] = {"entry": e, "juice": 0.0, "roll_pairs": {}}
            continue
        cyc = open_cycle.get(t)
        if not cyc:
            continue
        rid = e.get("roll_id")
        if a == "close_short":
            cyc["juice"] += float(e.get("net_juice_total") or 0)
            if rid:
                cyc["roll_pairs"].setdefault(rid, {})["buy"] = float(e.get("close_total") or 0)
        elif a == "sell_short" and rid:
            cyc["roll_pairs"].setdefault(rid, {})["sell"] = float(e.get("premium_total") or 0)
        elif a == "close_leap":
            entry = cyc["entry"]
            capital = float(entry.get("execution_total") or 0)
            leap_pnl = float(e.get("realized_pnl") or 0)
            gross_juice = round(cyc["juice"], 2)
            roll_nets = [p["sell"] - p["buy"] for p in cyc["roll_pairs"].values()
                         if "sell" in p and "buy" in p]
            roll_net = round(sum(roll_nets), 2)
            roll_drag = round(sum(n for n in roll_nets if n < 0), 2)
            net_result = round(leap_pnl + gross_juice, 2)
            d_in, d_out = _parse_day(entry.get("date")), _parse_day(e.get("date"))
            days_held = (d_out - d_in).days if d_in and d_out else None
            net_return_pct = round(net_result / capital * 100, 2) if capital else None
            cycles.append({
                "id": f"cycle_{len(cycles) + 1:03d}",
                "ticker": t,
                "entry_date": str(entry.get("date", ""))[:10],
                "exit_date": str(e.get("date", ""))[:10],
                "days_held": days_held,
                "capital_deployed": round(capital, 2),
                "gross_juice": gross_juice,
                "roll_count": len(cyc["roll_pairs"]),
                "roll_net": roll_net,
                "roll_drag": roll_drag,
                "leap_pnl": round(leap_pnl, 2),
                "net_result": net_result,
                "net_return_pct": net_return_pct,
                # HARD_CFM_RULE: the 15-25% per 4-8 week cycle target.
                "target_range_pct": [config.CYCLE_RETURN_MIN * 100, config.CYCLE_RETURN_MAX * 100],
                "target_met": (net_return_pct is not None
                               and net_return_pct >= config.CYCLE_RETURN_MIN * 100),
                # Coded exit reason copied from the close_leap execution. A close
                # with no recognized coded reason is a pre-feature cycle -> it is
                # permanently LEGACY_UNRECORDED (never fabricated). exit_note and
                # exit_metrics ride along for calibration's entry->exit deltas.
                "exit_reason": (e.get("exit_reason")
                                if exit_reasons.is_valid(e.get("exit_reason"))
                                else exit_reasons.ExitReason.LEGACY_UNRECORDED),
                "exit_note": e.get("exit_note"),
                "exit_metrics": e.get("exit_metrics"),
                # Immutable entry snapshot (full) + a compact summary for the
                # History tab / juice-journal CSV. Copied from the buy_leap.
                "entry_context": entry.get("entry_context"),
                "entry_summary": entry_context.summary(entry.get("entry_context")),
                "wash_sale": None,
            })
            del open_cycle[t]

    # Wash-sale flagging (visibility only, not tax software): a loss-closing
    # cycle re-entered in the same underlying within the window is flagged;
    # a recent loss with the window still open is marked so a NEW entry knows.
    def _parse_ts(s):
        try:
            return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    window = timedelta(days=config.WASH_SALE_WINDOW_DAYS)
    buys_by_ticker: dict[str, list] = {}      # ticker -> [(exec_index, ts)]
    close_by_cycle: dict[str, tuple] = {}     # "ticker|exit_date" -> (exec_index, ts)
    for i, e in enumerate(execs):
        ts = _parse_ts(e.get("date"))
        if ts is None:
            continue
        if e.get("action") == "buy_leap":
            buys_by_ticker.setdefault(e.get("ticker", ""), []).append((i, ts))
        elif e.get("action") == "close_leap":
            close_by_cycle[f"{e.get('ticker', '')}|{e.get('date', '')[:10]}"] = (i, ts)
    for c in cycles:
        if c["leap_pnl"] >= 0:
            continue
        hit = close_by_cycle.get(f"{c['ticker']}|{c['exit_date']}")
        if hit is None:
            continue
        close_idx, exit_ts = hit
        # "After the exit" is decided by append order (the log is append-only),
        # so a same-day rebuy counts while the cycle's own entry never does;
        # the 30-day window is a timestamp comparison.
        reentry = next((ts for bi, ts in buys_by_ticker.get(c["ticker"], [])
                        if bi > close_idx and ts <= exit_ts + window), None)
        if reentry:
            c["wash_sale"] = {"status": "flagged", "loss": c["leap_pnl"],
                              "reentry_date": reentry.date().isoformat()}
        elif datetime.now(timezone.utc) <= exit_ts + window:
            c["wash_sale"] = {"status": "window_open", "loss": c["leap_pnl"],
                              "window_ends": (exit_ts + window).date().isoformat()}
    state["cycles"] = cycles

    # Per-position LEAP-lifecycle derived fields that need only stored data +
    # today's date. The price-dependent health (extrinsic remaining, weekly
    # burn, net maintenance, delta velocity) is layered on at view time in
    # leap_policy, where the live stock price and option mark are available.
    weeks_rows = state["theta_ledger"]["weeks"]
    today = now.date()
    for p in state.get("positions", []):
        ticker = p.get("ticker", "")
        if p.get("status") == "closed" or not (p.get("leap") or {}):
            p["leap_dte"] = None
            p["trailing_avg_weekly_juice"] = None
            continue
        leap = p["leap"]
        # leap_dte: calendar days to expiry from the stored expiration; fall
        # back to the static entry-time snapshot when no expiration is stored.
        dte = None
        exp = leap.get("expiration")
        if exp:
            try:
                dte = (datetime.strptime(str(exp)[:10], "%Y-%m-%d").date() - today).days
            except ValueError:
                dte = None
        p["leap_dte"] = dte if dte is not None else leap.get("dte")
        # trailing_avg_weekly_juice: mean net juice over the last N COMPLETED
        # weeks for this ticker (weeks_rows is sorted ascending by week).
        juice_weeks = [r["net_juice"] for r in weeks_rows
                       if r["ticker"] == ticker and r["week"] < cur_week]
        juice_weeks = juice_weeks[-config.JUICE_TRAILING_WEEKS:]
        p["trailing_avg_weekly_juice"] = (round(sum(juice_weeks) / len(juice_weeks), 2)
                                          if juice_weeks else None)

    # Order lifecycle: current coded state per Schwab orderId is DERIVED from the
    # append-only order_events log (never stored imperatively). The last event for
    # an order wins, so recompute is a pure replay — a crash mid-cancel can't leave
    # a half-written status behind. order_lifecycle.py owns the state vocabulary.
    order_state: dict[str, dict] = {}
    for ev in state.get("order_events", []):
        oid = str(ev.get("order_id") or "")
        if not oid:
            continue
        order_state[oid] = {
            "order_id": oid,
            "state": ev.get("new_state"),
            "ticker": ev.get("ticker"),
            "intent": ev.get("intent"),
            "raw_status": ev.get("raw_status"),
            "at": ev.get("at"),
        }
    state["order_state"] = order_state

    # Trust layer: recommendation resolutions, the trust scoreboard, and the
    # order-fidelity ledger are pure derivations over the immutable records
    # (trust_derive.py). Guarded like validate_payback — a derivation bug must
    # never block an execution append.
    try:
        import trust_derive
        trust_derive.recompute(state, now)
    except Exception:  # noqa: BLE001
        logger.exception("trust derivation failed (non-fatal); scoreboard stale")
    return state
