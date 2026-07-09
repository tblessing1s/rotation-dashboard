"""Monthly payout tracking — the income-withdrawal view over the juice ledger.

The CFM strategy books income as *net juice* (premium sold − buyback), captured
on every ``close_short`` execution as ``net_juice_total``. The theta ledger sums
that into this-week / this-month / YTD live totals, but it keeps no month-by-month
record and has no notion of an operator *paying themselves out* each month.

This module adds exactly that, WITHOUT duplicating the source of truth:

* Net juice per calendar month is **derived** from the immutable executions
  (never stored) — recompute is idempotent, same as the theta ledger.
* The only thing persisted is the operator's *payout bookkeeping*: which months
  have been marked paid, when, the amount snapshotted at that moment, and an
  optional note. Snapshotting the amount freezes what was actually withdrawn even
  if a later reconciliation adjusts historical executions.

The "current month" figure is an **estimate** (the month is still accruing); a
completed month is **final**. The ``PAYOUT_READY`` alert (see alerts.py) fires
once a month rolls over so the just-finalized payout can be withdrawn and marked.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import logging_handler as log

ET = ZoneInfo("America/New_York")

# Month labels for display, e.g. "2026-06" -> "June 2026".
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]


def _month_key(date_str: str) -> str | None:
    """The 'YYYY-MM' bucket for an execution date, or None if unparseable."""
    s = str(date_str or "")[:7]
    if len(s) == 7 and s[4] == "-":
        return s
    return None


def month_label(month: str) -> str:
    """'2026-06' -> 'June 2026'."""
    try:
        y, m = month.split("-")
        return f"{_MONTH_NAMES[int(m)]} {y}"
    except (ValueError, IndexError):
        return month


def _cur_month() -> str:
    """Current calendar month in ET (the operator's trading day boundary)."""
    return datetime.now(ET).strftime("%Y-%m")


def _prev_month(month: str) -> str:
    """The calendar month before 'YYYY-MM'."""
    y, m = (int(x) for x in month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def monthly_net_juice(state: dict) -> dict[str, dict]:
    """Per-month income derived from the close_short executions.

    Returns month -> {"net_juice", "closes"} where ``net_juice`` sums
    ``net_juice_total`` (the same figure the theta ledger keys off) and ``closes``
    counts the contributing close_short tickets. Months with no closes are absent.
    """
    buckets: dict[str, dict] = {}
    for e in state.get("executions", []):
        if e.get("action") != "close_short":
            continue
        mk = _month_key(e.get("date", ""))
        if not mk:
            continue
        row = buckets.setdefault(mk, {"net_juice": 0.0, "closes": 0})
        row["net_juice"] += float(e.get("net_juice_total") or 0)
        row["closes"] += 1
    for row in buckets.values():
        row["net_juice"] = round(row["net_juice"], 2)
    return buckets


def _records(state: dict) -> dict[str, dict]:
    return (state.get("payouts") or {}).get("records") or {}


def _month_entry(month: str, net: dict | None, record: dict | None,
                 cur_month: str) -> dict:
    """One month's row for the view: derived income merged with paid bookkeeping."""
    net = net or {"net_juice": 0.0, "closes": 0}
    record = record or {}
    is_current = month == cur_month
    paid = bool(record.get("paid"))
    return {
        "month": month,
        "label": month_label(month),
        "net_juice": round(float(net.get("net_juice") or 0), 2),
        "closes": int(net.get("closes") or 0),
        # A month still in progress is an ESTIMATE; a completed month is FINAL.
        "estimated": is_current,
        "status": "in_progress" if is_current else ("paid" if paid else "unpaid"),
        "paid": paid,
        "paid_at": record.get("paid_at"),
        # The amount snapshotted when marked paid — what was actually withdrawn,
        # which can differ from a later-recomputed net_juice. Falls back to the
        # live figure for display when unpaid.
        "paid_amount": record.get("paid_amount"),
        "note": record.get("note"),
    }


def view(state: dict | None = None) -> dict:
    """The Payouts page payload: current-month estimate, last-month final payout,
    the full month-by-month history, and roll-up totals."""
    state = state if state is not None else log.load_state()
    net_by_month = monthly_net_juice(state)
    records = _records(state)
    cur = _cur_month()
    prev = _prev_month(cur)

    # Union of every month that has income OR a payout record, newest first.
    months = sorted(set(net_by_month) | set(records) | {cur, prev}, reverse=True)
    history = [_month_entry(m, net_by_month.get(m), records.get(m), cur) for m in months]

    current = _month_entry(cur, net_by_month.get(cur), records.get(cur), cur)
    previous = _month_entry(prev, net_by_month.get(prev), records.get(prev), cur)

    year = cur[:4]
    ytd = round(sum(r["net_juice"] for m, r in net_by_month.items() if m[:4] == year), 2)
    all_time = round(sum(r["net_juice"] for r in net_by_month.values()), 2)
    paid_out = round(sum(float(rec.get("paid_amount") or 0)
                         for rec in records.values() if rec.get("paid")), 2)
    # Income from FINALIZED (past) months that hasn't been marked paid yet.
    unpaid = round(sum(r["net_juice"] for r in history
                       if r["status"] == "unpaid" and r["net_juice"] > 0), 2)

    return {
        "current": current,
        "previous": previous,
        "history": history,
        "totals": {
            "ytd": ytd,
            "all_time": all_time,
            "paid_out": paid_out,
            "unpaid": unpaid,
            "year": year,
        },
    }


def mark_paid(month: str, note: str | None = None,
              amount: float | None = None) -> dict:
    """Record that a month's payout has been withdrawn.

    Snapshots the month's net juice as ``paid_amount`` (unless an explicit amount
    is given) so the record is immutable against later execution corrections.
    Refuses to mark the still-accruing current month (its figure isn't final).
    Returns the refreshed view.
    """
    month = str(month or "").strip()
    if not (len(month) == 7 and month[4] == "-"):
        raise ValueError(f"invalid month '{month}' — expected YYYY-MM")
    if month >= _cur_month():
        raise ValueError("can't finalize the current (or a future) month — its "
                         "payout is still accruing")
    state = log.load_state()
    net = monthly_net_juice(state).get(month, {"net_juice": 0.0, "closes": 0})
    snapshot = float(amount) if amount is not None else float(net["net_juice"])
    payouts = state.setdefault("payouts", {"records": {}})
    records = payouts.setdefault("records", {})
    rec = records.setdefault(month, {"month": month})
    rec.update({
        "paid": True,
        "paid_at": log.utcnow(),
        "paid_amount": round(snapshot, 2),
        "net_juice_at_finalize": round(float(net["net_juice"]), 2),
    })
    if note is not None:
        rec["note"] = str(note)[:500]
    log.save_state(state)
    return view(state)


def unmark_paid(month: str) -> dict:
    """Undo a mark-paid (fat-finger recovery). Keeps any note. Returns the view."""
    month = str(month or "").strip()
    state = log.load_state()
    records = (state.get("payouts") or {}).get("records") or {}
    rec = records.get(month)
    if rec:
        rec["paid"] = False
        rec.pop("paid_at", None)
        rec.pop("paid_amount", None)
        log.save_state(state)
    return view(state)


def pending_payout(state: dict) -> dict | None:
    """The just-finalized previous month IF it earned income and hasn't been
    marked paid — the trigger for the PAYOUT_READY alert. None otherwise."""
    cur = _cur_month()
    prev = _prev_month(cur)
    net = monthly_net_juice(state).get(prev)
    if not net or net["net_juice"] <= 0:
        return None
    if _records(state).get(prev, {}).get("paid"):
        return None
    return {"month": prev, "label": month_label(prev), "net_juice": net["net_juice"]}
