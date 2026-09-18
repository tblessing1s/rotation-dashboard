"""Positions (track) (6 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import executor
import logging_handler as log
import position_manager

positions_bp = Blueprint("positions", __name__)


@positions_bp.route("/api/positions/close-empty", methods=["POST"])
def api_close_empty_positions():
    """Retire positions that hold nothing — no shares, no LEAP legs, no short
    calls, no short puts. A shell like that is a row left behind by a path that
    created the position record before booking a leg that never arrived, and it
    reads on the Positions tab exactly like something you own.

    It cannot touch a position that holds anything, so there is no way to lose a
    real holding through this. Each close appends an immutable marker."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(executor.close_empty_positions(payload.get("reason") or ""))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@positions_bp.route("/api/positions")
def api_positions():
    try:
        state = log.load_state()
        views = position_manager.positions_view(state)
        # Income profile + accrual progress per position (schema v21). Attached
        # here rather than inside positions_view so the accrual read stays a
        # display concern: nothing below feeds coverage, sizing or capital math —
        # accrued cash is CASH, never exposure, until a real lot is bought.
        #
        # Uses the CHEAP pure `progress`, not `lot_add_status`. This is a polled
        # read-only endpoint; running the Level 5 gate here would fire a live Schwab
        # cash_balance() call (and potentially a state.json WRITE, via
        # resolve_operating_cash) once per accrual-ready position on every poll. The
        # gate verdict belongs to the paths that act on it — the alert sweep and the
        # executor — which is where it is evaluated.
        try:
            import accrual
            import income_profile
            for view in views:
                profile = income_profile.of(view)   # the view IS the position dict
                view["income_profile"] = profile
                view["income_profile_badge"] = income_profile.badge(profile)
                view["accrual"] = accrual.progress(
                    state, view.get("ticker", ""),
                    view.get("stock_price") or view.get("price"))
        except Exception:  # noqa: BLE001 — a display readout never sinks the panel
            pass
        return jsonify({
            "positions": views,
            "capital": position_manager.capital_summary(state),
            "extrinsic_payback": state.get("extrinsic_payback", {}),
            "accrual_ledger": state.get("accrual_ledger", {}),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@positions_bp.route("/api/positions/<ticker>/exit", methods=["POST"])
def api_positions_exit(ticker):
    """Fully exit a position on ONE call: close every open short, then sell
    every owned share — executor.exit_position(), the first thing in this app
    that sequences both legs of a real exit instead of stopping at a
    recommendation. Manual-trigger only: this endpoint is the human-in-the-
    loop path (a click), never called by a scheduled pass. Blocks for the
    duration of the exit (each leg waits for its own fill, live mode included)
    exactly like the other synchronous "do it now" actions (reconcile, run
    evaluation pass) — a deliberate, rare action a human is actively waiting on.

    Body: {exit_reason (required, a coded ExitReason), exit_note (required
    only for OPERATOR_DISCRETION), source_rec_id (optional)}."""
    payload = request.get_json(silent=True) or {}
    try:
        result = executor.exit_position(
            ticker, exit_reason=payload.get("exit_reason"),
            exit_note=payload.get("exit_note"),
            source_rec_id=payload.get("source_rec_id"))
        return jsonify(result), (200 if result.get("ok") else 409)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except executor.PositionFrozenError as e:
        return jsonify({"error": str(e), "frozen": True, "ticker": e.ticker,
                        "review": e.review}), 409
    except Exception as e:  # noqa: BLE001
        return _err(e)


@positions_bp.route("/api/positions/refresh-quote", methods=["POST"])
def api_positions_refresh_quote():
    """Force a live quote for one position's stock + its open short-call legs
    right now, bypassing the caches that otherwise back the Positions view
    (up to config.QUOTE_CACHE_SECONDS for the stock, config.
    OPTION_MARK_MAX_AGE_SECONDS for each option leg). The on-demand 'this
    number might be stale' path outside the Roll ticket, which already
    fetches fresh because it's about to build an order."""
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        state = log.load_state()
        position = log.find_position(state, ticker)
        if position is None:
            return jsonify({"error": f"no {ticker} position"}), 404
        return jsonify(position_manager.refresh_quote(ticker, position))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@positions_bp.route("/api/positions/set-legs", methods=["POST"])
def api_set_position_legs():
    """Single-spot position editor: directly set a position's short_calls +
    leap_legs from operator-entered legs (extrinsic computed from premium + entry
    price). The simple way to make state match the real broker position."""
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(executor.set_position_legs(ticker, payload.get("legs") or [],
                                                  payload.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@positions_bp.route("/api/positions/repair-leap-cost", methods=["POST"])
def api_repair_leap_cost():
    """One-click fix for a LEAP whose cost basis was stored per-share (~100× too
    small), which makes the intrinsic-vs-cost orange read absurdly high. Corrects
    only the mis-scaled LEAP legs (×100 + recomputed extrinsic); shorts untouched."""
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(executor.repair_leap_cost_scale(ticker, payload.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)

