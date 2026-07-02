"""state.json is the single source of truth.

Every execution is appended here with a timestamp and captured prices; the theta
ledger and extrinsic-payback meters are *derived* from the executions/positions
so nothing is ever hand-maintained. Writes are atomic (temp file + rename) and
guarded by a process lock — this is a single-writer store (run one machine).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config
import migrations

_lock = threading.RLock()


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
        # Live orders placed at the broker but not yet filled. Keyed by Schwab
        # order id; an entry is removed when the order fills (then committed as an
        # execution) or is cancelled/rejected.
        "pending_orders": {},
        "alerts": migrations.default_alert_state(),
    }


def load_state() -> dict:
    with _lock:
        if not os.path.exists(config.active_state_path()):
            state = _default_state()
            _write(state)
            return state
        try:
            with open(config.active_state_path(), encoding="utf-8") as fh:
                state = json.load(fh)
        except (ValueError, OSError):
            state = _default_state()
        # Versioned migrations first (they add structure old files lack), then
        # forward-fill any still-missing top-level keys so older state files load.
        state, migrated = migrations.migrate(state)
        for k, v in _default_state().items():
            state.setdefault(k, v)
        if migrated:
            # Rebuild the derived ledgers so migration-added derived structures
            # (e.g. the roll ledger) are populated from day one, not first-write.
            recompute_derived(state)
            _write(state)
        return state


def _write(state: dict) -> None:
    path = config.active_state_path()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def save_state(state: dict) -> dict:
    with _lock:
        state.setdefault("metadata", {})["last_updated"] = utcnow()
        _write(state)
        return state


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
    return state
