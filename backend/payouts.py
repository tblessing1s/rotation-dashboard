"""Monthly payout tracking — the income-withdrawal view over the juice ledger.

The CFM strategy books income as *net juice* (premium sold − buyback), captured
on every ``close_short`` execution as ``net_juice_total``. The theta ledger sums
that into this-week / this-month / YTD live totals, but it keeps no month-by-month
record and has no notion of an operator *paying themselves out* each month.

The payout an operator can actually take is not the raw juice: the long LEAP is
decaying underneath it, and that weekly extrinsic burn has to be reserved to
maintain/roll the LEAP. So the headline **payout is the leftover** —

    payout (leftover) = net juice collected − LEAP extrinsic burn

— where the burn is the REALIZED weekly extrinsic decay from ``burn_marks`` (same
whole-position dollars as the juice ledger), summed over the month. When burn
hasn't been marked for a month yet the payout degrades cleanly to juice-only and
says so, rather than pretending the burn is zero.

This module adds exactly that, WITHOUT duplicating the source of truth:

* Net juice per calendar month is **derived** from the immutable executions, and
  the LEAP burn from the weekly burn marks — neither is stored here.
* The only thing persisted is the operator's *payout bookkeeping*: whether a
  month has been finalized and/or marked paid, when, the amount snapshotted at
  each step, and an optional note. Snapshotting freezes what was actually
  finalized/withdrawn even if a later reconciliation adjusts historical
  executions.

A month moves through three states:

  in_progress → finalizable → finalized → paid

* **in_progress** — the current month, still accruing juice.
* **finalizable** — the month's short income is done, so the payout can be
  finalized: the last short *of that month* has closed (no open short leg still
  expires in it), OR the calendar month has ended. This is the trigger for the
  ``PAYOUT_READY`` notification.
* **finalized** — the operator locked the amount in (snapshotted).
* **paid** — the operator recorded the cash as withdrawn.

Rolling the last weekly short of a month into a short that expires *next* month
flips the current month to finalizable immediately — that's the "last short of
the month is closed" moment, not the calendar rollover.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import logging_handler as log

ET = ZoneInfo("America/New_York")

# Month labels for display, e.g. "2026-06" -> "June 2026".
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]


def _month_key(date_str: str) -> str | None:
    """The 'YYYY-MM' bucket for a date string, or None if unparseable."""
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
        # Same date->expiration bucketing the theta ledger uses, so a close can't
        # show up as juice in History yet vanish from the monthly payout. A close
        # with neither a parseable date nor expiration is left out (it surfaces as
        # an 'undated' row in the History per-week table) rather than guessed into
        # a month.
        when = log.bucket_datetime(e)
        mk = when.strftime("%Y-%m") if when else None
        if not mk:
            continue
        row = buckets.setdefault(mk, {"net_juice": 0.0, "closes": 0})
        row["net_juice"] += float(e.get("net_juice_total") or 0)
        row["closes"] += 1
    for row in buckets.values():
        row["net_juice"] = round(row["net_juice"], 2)
    return buckets


def _short_expiry_month(sc: dict) -> str | None:
    """The 'YYYY-MM' a short leg expires in. Uses the stored expiration; falls
    back to open_date + dte (weeklies default to 5) when the expiration wasn't
    captured (paper/demo legs), so a leg with income still to come this month is
    never mistaken for done."""
    exp = _month_key(sc.get("expiration"))
    if exp:
        return exp
    open_date = str(sc.get("open_date") or "")[:10]
    if open_date:
        try:
            d = datetime.strptime(open_date, "%Y-%m-%d").date() + \
                timedelta(days=int(sc.get("dte") or 5))
            return d.strftime("%Y-%m")
        except (ValueError, TypeError):
            return _month_key(open_date)
    return None


def has_open_short_expiring_in(state: dict, month: str) -> bool:
    """True if any OPEN short leg still expires within ``month`` — i.e. this
    month can still earn juice, so it isn't done yet. The last short of the month
    closing (or rolling into a next-month expiry) flips this to False."""
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        for sc in p.get("short_calls") or []:
            if _short_expiry_month(sc) == month:
                return True
    return False


def is_finalizable(state: dict, month: str, net_by_month: dict,
                   cur: str | None = None) -> bool:
    """Can this month's payout be finalized now?

    A month with net income is finalizable once its short income is done: for a
    past (calendar-ended) month always; for the current month once no open short
    still expires in it (the last short of the month has closed). A future month,
    or a month with no positive income, is never finalizable.
    """
    cur = cur or _cur_month()
    if float((net_by_month.get(month) or {}).get("net_juice") or 0) <= 0:
        return False
    if month > cur:
        return False
    if month < cur:
        return True
    return not has_open_short_expiring_in(state, month)


def monthly_leap_burn() -> dict[str, float]:
    """Realized LEAP extrinsic burn per month (whole-position $), from the weekly
    burn marks. Best-effort: any read failure (no marks file yet, telemetry
    unavailable) degrades to {} so the payout falls back to juice-only."""
    try:
        import burn_marks
        return burn_marks.monthly_realized_burn()
    except Exception:  # noqa: BLE001 — burn is an overlay, never breaks the payout
        return {}


def _net_payout(net_juice: float, leap_burn: float | None) -> float:
    """The leftover: juice minus LEAP burn (burn treated as 0 when untracked)."""
    return round(float(net_juice) - float(leap_burn or 0), 2)


def _records(state: dict) -> dict[str, dict]:
    return (state.get("payouts") or {}).get("records") or {}


def _payout_amount(net_payout: float, record: dict) -> float:
    """The figure to show for a month: the paid amount if paid, else the finalized
    amount if finalized, else the live leftover (juice − LEAP burn)."""
    if record.get("paid") and record.get("paid_amount") is not None:
        return float(record["paid_amount"])
    if record.get("finalized") and record.get("finalized_amount") is not None:
        return float(record["finalized_amount"])
    return round(float(net_payout), 2)


def _status(month: str, net: float, record: dict, finalizable: bool,
            cur: str) -> str:
    if record.get("paid"):
        return "paid"
    if record.get("finalized"):
        return "finalized"
    if finalizable:
        return "finalizable"
    if month == cur:
        return "in_progress"
    return "none"  # a past month that never earned income


def _month_entry(month: str, net: dict | None, record: dict | None,
                 leap_burn: float | None, state: dict, cur: str) -> dict:
    """One month's row for the view: derived income (juice − LEAP burn = leftover)
    merged with the finalize/paid bookkeeping."""
    net = net or {"net_juice": 0.0, "closes": 0}
    record = record or {}
    net_juice = round(float(net.get("net_juice") or 0), 2)
    burn_tracked = leap_burn is not None
    burn = round(float(leap_burn or 0), 2)
    net_payout = _net_payout(net_juice, burn)
    finalizable = is_finalizable(state, month, {month: net}, cur)
    return {
        "month": month,
        "label": month_label(month),
        # The income breakdown: juice collected, the LEAP burn reserved against it,
        # and the leftover the operator can actually take.
        "net_juice": net_juice,
        "leap_burn": burn,
        "burn_tracked": burn_tracked,
        "net_payout": net_payout,
        "closes": int(net.get("closes") or 0),
        # A month still in progress shows an ESTIMATE; finalized months are locked.
        "estimated": month == cur and not record.get("finalized"),
        "status": _status(month, net_juice, record, finalizable, cur),
        "finalizable": finalizable,
        "finalized": bool(record.get("finalized")),
        "finalized_at": record.get("finalized_at"),
        "finalized_amount": record.get("finalized_amount"),
        "finalized_juice": record.get("finalized_juice"),
        "finalized_burn": record.get("finalized_burn"),
        "paid": bool(record.get("paid")),
        "paid_at": record.get("paid_at"),
        "paid_amount": record.get("paid_amount"),
        # The single figure the UI headlines for this month: the leftover payout.
        "payout_amount": _payout_amount(net_payout, record),
        "note": record.get("note"),
    }


def view(state: dict | None = None) -> dict:
    """The Payouts page payload: current-month estimate, last-month final payout,
    the full month-by-month history, and roll-up totals."""
    state = state if state is not None else log.load_state()
    net_by_month = monthly_net_juice(state)
    burn_by_month = monthly_leap_burn()
    records = _records(state)
    cur = _cur_month()
    prev = _prev_month(cur)

    # Union of every month that has income, burn, OR a payout record, newest first.
    months = sorted(set(net_by_month) | set(burn_by_month) | set(records) | {cur, prev},
                    reverse=True)
    history = [_month_entry(m, net_by_month.get(m), records.get(m),
                            burn_by_month.get(m), state, cur)
               for m in months]
    by_month = {h["month"]: h for h in history}

    current = by_month[cur]
    previous = by_month[prev]

    year = cur[:4]
    in_year = [h for h in history if h["month"][:4] == year]
    ytd_juice = round(sum(h["net_juice"] for h in in_year), 2)
    ytd_burn = round(sum(h["leap_burn"] for h in in_year), 2)
    ytd = round(ytd_juice - ytd_burn, 2)  # leftover, year to date
    all_time = round(sum(h["net_payout"] for h in history), 2)
    paid_out = round(sum(h["payout_amount"] for h in history if h["paid"]), 2)
    # Leftover that's yours but not withdrawn yet: finalized-unpaid + months that
    # could be finalized now (last short closed / month ended).
    awaiting = round(sum(h["payout_amount"] for h in history
                         if not h["paid"] and (h["finalized"] or h["finalizable"])), 2)

    return {
        "current": current,
        "previous": previous,
        "history": history,
        "totals": {
            "ytd": ytd,
            "ytd_juice": ytd_juice,
            "ytd_burn": ytd_burn,
            "all_time": all_time,
            "paid_out": paid_out,
            "awaiting": awaiting,
            "year": year,
        },
    }


def _validate_month(month: str) -> str:
    month = str(month or "").strip()
    if not (len(month) == 7 and month[4] == "-"):
        raise ValueError(f"invalid month '{month}' — expected YYYY-MM")
    return month


def finalize(month: str, amount: float | None = None,
             note: str | None = None) -> dict:
    """Lock in a month's payout. Only allowed once the month is finalizable (its
    last short has closed, or the calendar month has ended). Snapshots the net
    juice as the finalized amount so the record survives later execution
    corrections. Returns the refreshed view."""
    month = _validate_month(month)
    state = log.load_state()
    net_by_month = monthly_net_juice(state)
    if not is_finalizable(state, month, net_by_month):
        raise ValueError(
            f"{month_label(month)} can't be finalized yet — it's still earning "
            f"juice (an open short still expires this month).")
    net = float((net_by_month.get(month) or {}).get("net_juice") or 0)
    burn = float(monthly_leap_burn().get(month) or 0)
    leftover = _net_payout(net, burn)
    # The finalized payout is the leftover (juice − LEAP burn); an explicit amount
    # overrides it. Snapshot the breakdown so the record survives later marks.
    snapshot = float(amount) if amount is not None else leftover
    payouts = state.setdefault("payouts", {"records": {}})
    rec = payouts.setdefault("records", {}).setdefault(month, {"month": month})
    rec.update({
        "finalized": True,
        "finalized_at": log.utcnow(),
        "finalized_amount": round(snapshot, 2),
        "finalized_juice": round(net, 2),
        "finalized_burn": round(burn, 2),
        "net_juice_at_finalize": round(net, 2),
    })
    if note is not None:
        rec["note"] = str(note)[:500]
    log.save_state(state)
    return view(state)


def unfinalize(month: str) -> dict:
    """Undo a finalize (recovery). Also clears any paid state on that month,
    since an un-finalized month can't be paid. Returns the view."""
    month = _validate_month(month)
    state = log.load_state()
    rec = _records(state).get(month)
    if rec:
        for k in ("finalized", "finalized_at", "finalized_amount", "finalized_juice",
                  "finalized_burn", "net_juice_at_finalize", "paid", "paid_at",
                  "paid_amount"):
            rec.pop(k, None)
        log.save_state(state)
    return view(state)


def mark_paid(month: str, note: str | None = None,
              amount: float | None = None) -> dict:
    """Record that a month's payout has been withdrawn. Finalizes the month first
    if it hasn't been (a mark-paid implies the amount is locked), snapshotting the
    net juice unless an explicit amount is given. Refuses a month that isn't
    finalizable yet (still earning juice). Returns the refreshed view."""
    month = _validate_month(month)
    state = log.load_state()
    net_by_month = monthly_net_juice(state)
    rec = _records(state).get(month) or {}
    if not rec.get("finalized") and not is_finalizable(state, month, net_by_month):
        raise ValueError(
            f"{month_label(month)} can't be paid yet — finalize it once its last "
            f"short of the month has closed.")
    net = float((net_by_month.get(month) or {}).get("net_juice") or 0)
    burn = float(monthly_leap_burn().get(month) or 0)
    leftover = _net_payout(net, burn)
    payouts = state.setdefault("payouts", {"records": {}})
    rec = payouts.setdefault("records", {}).setdefault(month, {"month": month})
    now = log.utcnow()
    if not rec.get("finalized"):
        rec.update({"finalized": True, "finalized_at": now,
                    "finalized_amount": round(leftover, 2),
                    "finalized_juice": round(net, 2),
                    "finalized_burn": round(burn, 2),
                    "net_juice_at_finalize": round(net, 2)})
    snapshot = (float(amount) if amount is not None
                else float(rec.get("finalized_amount") if rec.get("finalized_amount")
                           is not None else leftover))
    rec.update({"paid": True, "paid_at": now, "paid_amount": round(snapshot, 2)})
    if note is not None:
        rec["note"] = str(note)[:500]
    log.save_state(state)
    return view(state)


def unmark_paid(month: str) -> dict:
    """Undo a mark-paid (fat-finger recovery). Leaves the month finalized and any
    note intact. Returns the view."""
    month = _validate_month(month)
    state = log.load_state()
    rec = _records(state).get(month)
    if rec:
        rec["paid"] = False
        rec.pop("paid_at", None)
        rec.pop("paid_amount", None)
        log.save_state(state)
    return view(state)


def pending_finalization(state: dict) -> dict | None:
    """The most recent month that can be finalized now but hasn't been — the
    trigger for the PAYOUT_READY notification. Checks the current month first (its
    last short just closed), then the previous month (calendar rollover fallback).
    None when nothing is waiting. Reports ``reason`` so the alert can say why."""
    cur = _cur_month()
    net_by_month = monthly_net_juice(state)
    burn_by_month = monthly_leap_burn()
    records = _records(state)
    for month, reason in ((cur, "last_short_closed"), (_prev_month(cur), "month_ended")):
        if records.get(month, {}).get("finalized"):
            continue
        if is_finalizable(state, month, net_by_month, cur):
            net = float(net_by_month[month]["net_juice"])
            burn = burn_by_month.get(month)
            return {"month": month, "label": month_label(month),
                    "net_juice": net, "leap_burn": round(float(burn or 0), 2),
                    "net_payout": _net_payout(net, burn), "reason": reason}
    return None
