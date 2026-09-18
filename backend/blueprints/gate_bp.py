"""Screening / entry & account gate / option pricing (16 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import config
import earnings
import executor
import logging_handler as log
import option_chain
import position_manager
import screening
import strike_policy

gate_bp = Blueprint("gate", __name__)


@gate_bp.route("/api/sectors")
def api_sectors():
    try:
        return jsonify(screening.sectors())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/stock-filter")
def api_stock_filter():
    try:
        return jsonify(screening.stock_filter(request.args.get("sector")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/entry-gate")
def api_entry_gate():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(screening.entry_gate(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/account-gate")
def api_account_gate():
    """Level 5 (Account & Juice) pre-trade gate. Optional query params let the
    Execute flow pass real chain numbers: contracts, leap_cost (per share),
    weekly_extrinsic (per share)."""
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400

    def _f(name):
        v = request.args.get(name)
        return float(v) if v not in (None, "") else None

    try:
        import account_gate
        return jsonify(account_gate.evaluate(
            ticker,
            contracts=int(request.args.get("contracts") or 0) or None,
            leap_cost_per_share=_f("leap_cost"),
            weekly_extrinsic_per_share=_f("weekly_extrinsic"),
        ))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/option-chain/<ticker>")
def api_option_chain(ticker: str):
    strategy = request.args.get("strategy", "atr")
    # ?refresh=1 forces a live re-pull (the modal's bid/ask poll) past the 5-min cache.
    refresh = request.args.get("refresh", "").strip() in ("1", "true", "yes")
    try:
        return jsonify(option_chain.option_chain(ticker, strategy, refresh=refresh))
    except option_chain.RegimeBlocked as e:
        return jsonify({"error": str(e), "regime": "red"}), 403
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/put-chain/<ticker>")
def api_put_chain(ticker: str):
    """Weekly short-put candidates for the CSP ticket (schema v22).

    Separate from /api/option-chain because it answers a different question — the
    call route asks "what do I sell against shares I hold", this one asks "where
    would I be happy to be assigned". Same underlying fetch and cache, though: one
    Schwab payload carries both sides.
    """
    refresh = request.args.get("refresh", "").strip() in ("1", "true", "yes")
    try:
        return jsonify(option_chain.put_chain(ticker, refresh=refresh))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/put-placement-status")
def api_put_placement_status():
    """Which of the three placement switches are on, and which are not. Lets the
    ticket say WHY it can only record rather than greying a button out."""
    return jsonify(option_chain.placement_status())


@gate_bp.route("/api/defend")
def api_defend():
    """Defensive roll-down recommendation for a position whose short strike has
    been breached (underlying < strike): regime-aware new strike, est. net
    credit/debit, new extrinsic, and cost-basis effect."""
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(executor.defend_recommendation(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/leap-roll-estimate")
def api_leap_roll_estimate():
    """Roll-cost estimate for a position's LONG leg: suggested ~target-delta /
    ~180-DTE replacement LEAP, estimated net debit, and whether that debit still
    fits the 2xATR cash reserve (reserve_ok). Prices from the live chain when
    available, else a Black-Scholes estimate at trailing realized vol."""
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        import leap_policy
        return jsonify(leap_policy.roll_cost_estimate(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/strike-posture", methods=["GET", "POST"])
def api_strike_posture():
    """Read or set the operator's risk posture (aggressive/conservative) for
    weekly short strike selection (config.STRIKE_TABLE — the regime x posture
    ATR-mult/ITM%-floor table). Persisted per store (live/demo don't share it)."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(strike_policy.set_posture(payload.get("posture", "")))
        except ValueError as e:
            return _err(e, 400)
        except Exception as e:  # noqa: BLE001
            return _err(e)
    return jsonify({"posture": strike_policy.get_posture(),
                    "postures": list(config.STRIKE_POSTURES),
                    "table": config.STRIKE_TABLE})


@gate_bp.route("/api/roll-suggestion")
def api_roll_suggestion():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(executor.roll_suggestion(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/roll-options")
def api_roll_options():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    prior_target = request.args.get("prior_target", type=float)
    try:
        return jsonify(option_chain.roll_options(ticker, prior_target=prior_target))
    except option_chain.RegimeBlocked as e:
        return jsonify({"error": str(e), "regime": "red"}), 403
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/coverage")
def api_coverage():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(option_chain.coverage(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@gate_bp.route("/api/burn/<ticker>")
def api_burn(ticker):
    """Per-position theta-burn detail for the Burn panel: the three headline
    figures (juice/burn/net per week) + coverage + hold-extension ladder from
    leap_health, the weekly juice-vs-burn series (realized weeks from the mark
    telemetry, projected weeks forward to the planned exit), and the
    realized-vs-projected divergence. Read-only; degrades gracefully."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        import burn_marks
        state = log.load_state()
        pos = log.find_position(state, ticker)
        if not pos or pos.get("status") == "closed":
            return jsonify({"ticker": ticker, "error": "no open position"}), 404
        health = (position_manager.enrich_position(pos).get("leap_health")
                  or pos.get("leap_health") or {})
        marks = burn_marks.series(ticker)
        # Weekly juice-vs-burn: realized weeks (from marks) full-opacity, then the
        # projected forward weeks (to the planned exit) lighter.
        ledger_weeks = {(w.get("week"), w.get("ticker")): w.get("net_juice")
                        for w in (state.get("theta_ledger", {}) or {}).get("weeks", [])}
        trailing = health.get("trailing_avg_weekly_juice")
        weekly = []
        for m in marks:
            if m.get("realized_burn_week") is None:
                continue
            wk = _iso_week_label(m.get("date"))
            weekly.append({"label": (m.get("date") or "")[5:], "projected": False,
                           "juice": ledger_weeks.get((wk, ticker), trailing),
                           "burn": m.get("realized_burn_week")})
        proj = health.get("burn_projection") or {}
        model_burn = health.get("model_burn_per_week")
        weeks_ahead = int(max(1, round(proj.get("weeks_remaining") or 0))) if proj.get("priceable") else 0
        for i in range(weeks_ahead):
            weekly.append({"label": f"+{i + 1}", "projected": True,
                           "juice": trailing, "burn": model_burn})
        return jsonify({
            "ticker": ticker,
            "planned_exit_dte": health.get("planned_exit_dte"),
            "juice_per_week": trailing,
            "burn_per_week": model_burn,
            "net_juice_per_week": health.get("net_juice_per_week"),
            "coverage": health.get("coverage"),
            "burn_projection": proj,
            "extension_preview": health.get("extension_preview"),
            "weekly": weekly,
            "divergence": burn_marks.divergence(ticker),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _iso_week_label(date_str) -> str | None:
    try:
        from datetime import datetime as _d
        d = _d.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    except (TypeError, ValueError):
        return None


@gate_bp.route("/api/earnings")
def api_earnings():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    refresh = request.args.get("refresh") in ("1", "true", "yes")
    try:
        return jsonify(earnings.next_earnings(ticker, refresh=refresh))
    except Exception as e:  # noqa: BLE001
        return _err(e)

