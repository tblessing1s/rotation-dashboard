"""Reconciliation (state.json vs Schwab) (6 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import alerts
import executor
import logging_handler as log

reconcile_bp = Blueprint("reconcile", __name__)


@reconcile_bp.route("/api/reconcile", methods=["GET", "POST"])
def api_reconcile():
    """GET: the last reconciliation report + history. POST: run reconciliation
    now (fetches live Schwab positions; report-only in demo/paper). Then also
    fires the alert pass so a fresh dirty/short-stock report surfaces at once."""
    if request.method == "POST":
        try:
            import reconcile
            report = reconcile.run_reconciliation()
            try:
                alerts.run()  # surface reconcile_dirty / short_stock immediately
            except Exception:  # noqa: BLE001 — a notify failure must not fail the run
                pass
            return jsonify(report)
        except Exception as e:  # noqa: BLE001
            return _err(e)
    state = log.load_state()
    return jsonify(state.get("reconciliation") or {"last": None, "history": []})


@reconcile_bp.route("/api/reconcile/resolve-expiry", methods=["POST"])
def api_reconcile_resolve_expiry():
    """One-click resolution for an EXPIRED_WORTHLESS_PENDING diff: books the $0
    close_short and clears the diff."""
    payload = request.get_json(silent=True) or {}
    diff_id = payload.get("diff_id", "")
    if not diff_id:
        return jsonify({"error": "diff_id is required"}), 400
    try:
        return jsonify(executor.resolve_expiry(diff_id))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/record-called-away", methods=["POST"])
def api_reconcile_record_called_away():
    """One-click resolution for a covered call that was assigned: an EQUITY
    share-reduction diff becomes a booked called-away (short call dropped, shares
    sold at the strike, realized P&L). Operator-initiated.
    Body: {diff_id, strike?, expiration?}."""
    payload = request.get_json(silent=True) or {}
    diff_id = payload.get("diff_id", "")
    if not diff_id:
        return jsonify({"error": "diff_id is required"}), 400
    try:
        return jsonify(executor.record_called_away(
            diff_id, strike=payload.get("strike"), expiration=payload.get("expiration")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/acknowledge", methods=["POST"])
def api_reconcile_acknowledge():
    """Acknowledge a diff the operator deems a non-issue (typed ack_reason
    required), logged onto the reconciliation record."""
    payload = request.get_json(silent=True) or {}
    diff_id = payload.get("diff_id", "")
    if not diff_id:
        return jsonify({"error": "diff_id is required"}), 400
    try:
        return jsonify(executor.acknowledge_diff(diff_id, payload.get("ack_reason", "")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/record-manual-roll", methods=["POST"])
def api_record_manual_roll():
    """Record an already-executed out-of-band roll (buy-to-close + sell-to-open)
    from the operator's captured fills + the roll-time underlying price. The app
    computes both legs' extrinsic from stock_price — nothing hand-entered beyond
    the fills. If stock_price is omitted but the new leg's premium + extrinsic are
    given, it is derived (stock = strike + max(premium − extrinsic, 0))."""
    p = request.get_json(silent=True) or {}
    try:
        stock_price = p.get("stock_price")
        if stock_price is None and p.get("to_premium") is not None and p.get("to_extrinsic") is not None:
            stock_price = executor.derive_stock_price_from_call(
                p["to_strike"], p["to_premium"], p["to_extrinsic"])
        return jsonify(executor.record_manual_roll(
            p.get("ticker"), from_strike=p.get("from_strike"),
            buyback_per_share=p.get("buyback_per_share"), to_strike=p.get("to_strike"),
            premium_per_share=p.get("to_premium", p.get("premium_per_share")),
            stock_price=stock_price, to_expiration=p.get("to_expiration"),
            from_expiration=p.get("from_expiration"),
            from_contracts=int(p.get("from_contracts") or 1),
            to_contracts=int(p.get("to_contracts") or 1),
            from_diff_id=p.get("from_diff_id"), to_diff_id=p.get("to_diff_id"),
            when=p.get("when")))
    except (ValueError, TypeError, KeyError) as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/history-audit", methods=["GET", "POST"])
def api_history_audit():
    """The trade-history audit — this book's fills vs Schwab's transactions
    (history_audit.py). GET returns the last report; POST runs it now
    (optional ``lookback_days``)."""
    import history_audit
    try:
        if request.method == "POST":
            p = request.get_json(silent=True) or {}
            return jsonify(history_audit.run_history_audit(
                lookback_days=p.get("lookback_days")))
        state = log.load_state()
        ha = state.get("history_audit") or {}
        return jsonify({"last": ha.get("last"), "last_failed": ha.get("last_failed")})
    except (ValueError, TypeError) as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/cross-book", methods=["GET", "POST"])
def api_cross_book():
    """The cross-book duplicate check (cross_book.py): GET this book's last
    record; POST re-checks every book now and returns this book's record."""
    import cross_book
    try:
        if request.method == "POST":
            cross_book.run_and_persist()
        return jsonify(log.load_state().get("cross_book") or {})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/cross-book/acknowledge", methods=["POST"])
def api_cross_book_ack():
    """Mark one cross-book duplicate explained for this book (typed reason)."""
    import cross_book
    p = request.get_json(silent=True) or {}
    if not p.get("duplicate_id"):
        return jsonify({"error": "duplicate_id is required"}), 400
    try:
        return jsonify(cross_book.acknowledge(p["duplicate_id"], p.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/history-audit/acknowledge", methods=["POST"])
def api_history_audit_ack():
    """Mark one audit finding explained (typed reason, logged)."""
    import history_audit
    p = request.get_json(silent=True) or {}
    if not p.get("finding_id"):
        return jsonify({"error": "finding_id is required"}), 400
    try:
        return jsonify(history_audit.acknowledge(p["finding_id"], p.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/unfilled-rolls")
def api_unfilled_rolls():
    """Rolls on ?ticker= that were booked without a broker fill behind them (paper /
    untransmitted) and are still standing — what the review panel offers to undo
    when the broker shows the new call missing."""
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify({"ticker": ticker, "rolls": executor.find_unfilled_rolls(ticker)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/undo-unfilled-roll", methods=["POST"])
def api_undo_unfilled_roll():
    """The roll was booked but never filled at Schwab: void both legs, restore the
    old short, and (``assigned``) book that old call's assignment — see
    executor.undo_unfilled_roll."""
    p = request.get_json(silent=True) or {}
    try:
        return jsonify(executor.undo_unfilled_roll(
            p.get("ticker"), p.get("roll_id"), p.get("reason"),
            assigned=bool(p.get("assigned")), assigned_on=p.get("assigned_on"),
            from_expiration=p.get("from_expiration"),
            stock_price=p.get("stock_price"), diff_ids=p.get("diff_ids")))
    except (ValueError, TypeError) as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/rebuild-position", methods=["POST"])
def api_rebuild_position():
    """Rebuild one position's legs from the broker's actual holdings (ground
    truth), restoring economics from the immutable execution log. The clean repair
    for an accumulated reconciliation tangle — replaces stacking adjustments."""
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(executor.rebuild_position_from_broker(
            ticker, broker_legs=payload.get("broker_legs"), legs=payload.get("legs"),
            dry_run=bool(payload.get("dry_run")), reason=payload.get("reason"),
            diff_ids=payload.get("diff_ids")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@reconcile_bp.route("/api/reconcile/freeze-status")
def api_reconcile_freeze_status():
    """The global reconciliation-freeze verdict (frozen tickers + reasons) plus the
    market-hours minutes staleness degrade. Drives the divergence/freeze panel and
    the 'last reconciled N minutes ago' heartbeat."""
    import reconcile
    return jsonify(reconcile.freeze_status(log.load_state()))

