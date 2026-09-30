"""Theta ledger, payouts, and trading history (11 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, Response

from api_common import _err

import executor
import logging_handler as log

ledger_bp = Blueprint("ledger", __name__)


@ledger_bp.route("/api/theta-ledger")
def api_theta_ledger():
    ticker = request.args.get("ticker")
    period = request.args.get("period")  # week | month | ytd
    try:
        state = log.load_state()
        # Rebuild derived state from the immutable executions first: the persisted
        # theta_ledger can lag the executions (a write path that didn't recompute),
        # which is what makes the per-week closes disagree with the live Payouts
        # view. Deriving on read keeps the two reconciled by construction.
        log.recompute_derived(state)
        ledger = state.get("theta_ledger", {})
        weeks = ledger.get("weeks", [])
        if ticker:
            weeks = [w for w in weeks if w.get("ticker", "").upper() == ticker.upper()]
        totals = ledger.get("totals", {})
        roll_ledger = state.get("roll_ledger", {"rolls": [], "by_ticker": {}})
        if ticker:
            roll_ledger = {
                "rolls": [r for r in roll_ledger.get("rolls", [])
                          if r.get("ticker", "").upper() == ticker.upper()],
                "by_ticker": {k: v for k, v in roll_ledger.get("by_ticker", {}).items()
                              if k.upper() == ticker.upper()},
            }
        import slippage
        out = {"weeks": weeks, "totals": totals,
               "extrinsic_summary": ledger.get("extrinsic_summary", {}),
               "extrinsic_payback": state.get("extrinsic_payback", {}),
               "roll_ledger": roll_ledger,
               # Paper juice is booked at the quoted mid; this caveat/haircut says
               # how far realized fills will run below it (measured once live).
               "slippage": slippage.report(state)}
        if period in ("week", "month", "ytd"):
            key = {"week": "this_week", "month": "this_month", "ytd": "ytd"}[period]
            out["period"] = {"period": period, "net_juice": totals.get(key)}
        return jsonify(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/payouts")
def api_payouts():
    """Monthly payout tracker: current-month estimate, last-month final payout,
    the month-by-month income history, and roll-up totals. Income per month is
    derived from the close_short executions; only paid-status bookkeeping is
    persisted (see payouts.py)."""
    try:
        import payouts
        return jsonify(payouts.view(log.load_state()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/payouts/finalize", methods=["POST"])
def api_payouts_finalize():
    """Lock in a month's payout once it's finalizable — its last short of the
    month has closed or the calendar month has ended. Snapshots the net juice."""
    payload = request.get_json(silent=True) or {}
    try:
        import payouts
        return jsonify(payouts.finalize(
            payload.get("month"), amount=payload.get("amount"),
            note=payload.get("note")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/payouts/unfinalize", methods=["POST"])
def api_payouts_unfinalize():
    """Undo a finalize (also clears paid state on that month)."""
    payload = request.get_json(silent=True) or {}
    try:
        import payouts
        return jsonify(payouts.unfinalize(payload.get("month")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/payouts/mark-paid", methods=["POST"])
def api_payouts_mark_paid():
    """Record that a month's payout has been withdrawn (finalizes it first if
    needed). Snapshots the amount (or an explicit override)."""
    payload = request.get_json(silent=True) or {}
    try:
        import payouts
        return jsonify(payouts.mark_paid(
            payload.get("month"), note=payload.get("note"),
            amount=payload.get("amount")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/payouts/unmark-paid", methods=["POST"])
def api_payouts_unmark_paid():
    """Undo a mark-paid (fat-finger recovery)."""
    payload = request.get_json(silent=True) or {}
    try:
        import payouts
        return jsonify(payouts.unmark_paid(payload.get("month")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/slippage")
def api_slippage():
    """Realized paper-fill slippage vs the quoted mid (mid-fill caveat + haircut)."""
    try:
        import slippage
        return jsonify(slippage.report(log.load_state()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/history")
def api_history():
    """Closed-cycle records + aggregate stats + the weekly net-juice chart."""
    try:
        import history
        # Rebuild the derived ledgers from the immutable executions before serving,
        # so the per-week / cycle views can never show a stale persisted derivation
        # (which is how History could disagree with the always-live Payouts view).
        state = log.load_state()
        log.recompute_derived(state)
        return jsonify(history.view(state))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/account-value-history")
def api_account_value_history():
    """Daily mark-to-market account-value points (position_manager.account_value)
    for this account's History tab chart. Recorded once/day by the nightly
    maintenance job (maintenance.snapshot_account_value) — there is no way to
    reconstruct past points on read, so a day the job didn't run has no point.

    The LAST point is today's value computed live (``live: true``), replacing any
    point already stored for today: the headline must follow the book as it is
    now, not as it was at the last nightly run — a correction (a voided trade, an
    undone roll) otherwise kept showing the stale total until the next night."""
    try:
        state = log.load_state()
        points = list(state.get("account_value_history", []))
        today = log.utcnow()[:10]
        try:
            import position_manager
            live = {"date": today, **position_manager.account_value(state), "live": True}
            if points and points[-1].get("date") == today:
                points[-1] = live
            else:
                points.append(live)
        except Exception as e:  # noqa: BLE001 — the stored history still renders
            log.logger.warning("live account value unavailable: %s", e)
        return jsonify({"points": points})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/account-value-history/remove", methods=["POST"])
def api_account_value_history_remove():
    """Drop stored daily points recorded while the book was wrong (e.g. a
    phantom position a later correction took off). They can't be recomputed —
    no historical prices are kept — so the honest fix is a gap. A typed reason
    is required; the removed points are kept under
    ``account_value_history_removed`` for the audit trail."""
    payload = request.get_json(silent=True) or {}
    dates = {str(d)[:10] for d in (payload.get("dates") or []) if d}
    reason = (payload.get("reason") or "").strip()
    if not dates:
        return jsonify({"error": "dates is required"}), 400
    if not reason:
        return jsonify({"error": "a typed reason is required"}), 400
    try:
        def _drop(state):
            hist = state.get("account_value_history") or []
            gone = [p for p in hist if p.get("date") in dates]
            state["account_value_history"] = [p for p in hist if p.get("date") not in dates]
            at = log.utcnow()
            state.setdefault("account_value_history_removed", []).extend(
                {**p, "removed_at": at, "removed_reason": reason} for p in gone)
            return [p.get("date") for p in gone]
        removed = log.mutate_state(_drop)
        return jsonify({"removed": removed})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/transactions/save", methods=["POST"])
def api_transactions_save():
    """Editable transaction table save: apply per-transaction economic edits (with
    linked stock price <-> extrinsic), then derive the open position from the
    transactions. The one-table source of truth."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(executor.save_transactions(payload.get("edits") or [],
                                                  payload.get("ticker")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/export/juice-journal")
def api_export_juice_journal():
    """The operator's off-system record (CFM 'juice journal' rule): weekly
    ledger + roll ledger + closed cycles as CSV (default) or markdown."""
    fmt = (request.args.get("format") or "csv").lower()
    try:
        import history
        state = log.load_state()
        if fmt in ("md", "markdown"):
            body, mime, name = history.juice_journal_markdown(state), "text/markdown", "juice_journal.md"
        else:
            body, mime, name = history.juice_journal_csv(state), "text/csv", "juice_journal.csv"
        return Response(
            body, mimetype=mime,
            headers={"Content-Disposition": f"attachment; filename={name}"})
    except Exception as e:  # noqa: BLE001
        return _err(e)



def _download(body: str, mime: str, name: str) -> Response:
    return Response(body, mimetype=mime,
                    headers={"Content-Disposition": f"attachment; filename={name}",
                             "Cache-Control": "no-store"})


@ledger_bp.route("/api/export/transactions")
def api_export_transactions():
    """The History tab's Transactions table as CSV — one row per fill, from the
    corrected view the ledgers use (see record_export)."""
    try:
        import record_export
        return _download(record_export.transactions_csv(log.load_state()),
                         "text/csv", "transactions.csv")
    except Exception as e:  # noqa: BLE001
        return _err(e)


@ledger_bp.route("/api/export/executions")
def api_export_executions():
    """The raw, append-only execution log, complete and uncapped, exactly as stored:
    ``?format=json`` (default, lossless) or ``csv`` (nested values as JSON text)."""
    fmt = (request.args.get("format") or "json").lower()
    try:
        import record_export
        state = log.load_state()
        if fmt == "csv":
            return _download(record_export.executions_csv(state), "text/csv", "executions.csv")
        return _download(record_export.executions_json(state), "application/json", "executions.json")
    except Exception as e:  # noqa: BLE001
        return _err(e)
