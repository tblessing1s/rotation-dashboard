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
        },
        "positions": [],
        "executions": [],
        "theta_ledger": {"weeks": [], "totals": {"this_week": 0, "this_month": 0, "ytd": 0, "pct_deployed": 0}},
        "extrinsic_payback": {},
        "roll_ledger": {"rolls": [], "by_ticker": {}},
        "cycles": [],
        # Live orders placed at the broker but not yet filled. Keyed by Schwab
        # order id; an entry is removed when the order fills (then committed as an
        # execution) or is cancelled/rejected.
        "pending_orders": {},
        "alerts": migrations.default_alert_state(),
    }


def load_state() -> dict:
    with _lock:
        path = config.active_state_path()
        if not os.path.exists(path):
            state = _default_state()
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


def get_pending_order(order_id: str) -> dict | None:
    return load_state().get("pending_orders", {}).get(str(order_id))


def pop_pending_order(order_id: str) -> dict | None:
    with _lock:
        state = load_state()
        rec = state.get("pending_orders", {}).pop(str(order_id), None)
        save_state(state)
        return rec


# ---------------------------------------------------------------------------
# Derived ledgers (theta ledger + extrinsic payback)
# ---------------------------------------------------------------------------
def _iso_week(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        dt = datetime.now(timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def recompute_derived(state: dict) -> dict:
    """Rebuild theta_ledger + extrinsic_payback from executions/positions."""
    execs = state.get("executions", [])
    now = datetime.now(timezone.utc)
    cur_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    cur_month = now.strftime("%Y-%m")
    cur_year = now.strftime("%Y")

    weeks: dict[tuple[str, str], dict] = {}
    totals = {"this_week": 0.0, "this_month": 0.0, "ytd": 0.0}
    collected_by_ticker: dict[str, float] = {}

    for e in execs:
        if e.get("action") != "close_short":
            continue
        ticker = e.get("ticker", "")
        wk = _iso_week(e.get("date", ""))
        net = float(e.get("net_juice_total") or 0)
        key = (wk, ticker)
        row = weeks.setdefault(key, {"week": wk, "ticker": ticker,
                                     "extrinsic_sold": 0.0, "extrinsic_paid_back": 0.0, "net_juice": 0.0})
        sold = float(e.get("extrinsic_sold") or 0) * int(e.get("contracts") or 0) * 100
        paid = float(e.get("extrinsic_paid_back") or 0) * int(e.get("contracts") or 0) * 100
        row["extrinsic_sold"] += sold
        row["extrinsic_paid_back"] += paid
        row["net_juice"] += net
        collected_by_ticker[ticker] = collected_by_ticker.get(ticker, 0.0) + net

        d = e.get("date", "")[:10]
        if wk == cur_week:
            totals["this_week"] += net
        if d[:7] == cur_month:
            totals["this_month"] += net
        if d[:4] == cur_year:
            totals["ytd"] += net

    deployed = float(state.get("metadata", {}).get("capital_deployed") or 0)
    totals["pct_deployed"] = round(totals["ytd"] / deployed, 4) if deployed else 0
    for k in ("this_week", "this_month", "ytd"):
        totals[k] = round(totals[k], 2)
    state["theta_ledger"] = {
        "weeks": sorted(weeks.values(), key=lambda r: (r["week"], r["ticker"])),
        "totals": totals,
    }

    # Extrinsic payback meter per position: how much of the LEAP's entry
    # extrinsic the collected short juice has paid back.
    payback: dict[str, dict] = {}
    agg_at_entry = agg_collected = agg_remaining = 0.0
    for p in state.get("positions", []):
        ticker = p.get("ticker", "")
        leap = p.get("leap") or {}
        at_entry = float(leap.get("extrinsic_at_entry") or 0)
        collected = collected_by_ticker.get(ticker, 0.0)
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
    # scorecard snapshot and the exit reason live ON those executions (captured
    # at trade time; they can't be reconstructed later), so this recompute is
    # deterministic and idempotent.
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
                "exit_reason": e.get("exit_reason") or "discretionary",
                "entry_snapshot": entry.get("entry_snapshot"),
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
    return state
