"""CFM dashboard Flask backend.

Serves the built React frontend and the CFM API: scan (regime/sectors/stock
filter) -> entry gate -> execute (Schwab + auto-log) -> track (positions/theta
ledger/kill switch/checklist). state.json is the source of truth; the only route
that contacts a provider live is the Schwab account/quote path used at execution.
"""
from __future__ import annotations

import os
import secrets

from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_cors import CORS

import config
import data_handler
import executor
import kill_switch
import logging_handler as log
import option_chain
import position_manager
import schwab_api
import screening
import sector_data

DIST_DIR = os.path.join(config.REPO_DIR, "frontend", "dist")

app = Flask(__name__, static_folder=None)
CORS(app)


def _err(e: Exception, code: int = 500):
    return jsonify({"error": str(e)}), code


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
@app.route("/api/regime")
def api_regime():
    try:
        return jsonify(screening.regime())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/sectors")
def api_sectors():
    try:
        return jsonify(screening.sectors())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/stock-filter")
def api_stock_filter():
    try:
        return jsonify(screening.stock_filter(request.args.get("sector")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/entry-gate")
def api_entry_gate():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(screening.entry_gate(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/option-chain/<ticker>")
def api_option_chain(ticker: str):
    strategy = request.args.get("strategy", "atr")
    try:
        return jsonify(option_chain.option_chain(ticker, strategy))
    except option_chain.RegimeBlocked as e:
        return jsonify({"error": str(e), "regime": "red"}), 403
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/roll-suggestion")
def api_roll_suggestion():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(executor.roll_suggestion(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------
@app.route("/api/execute", methods=["POST"])
def api_execute():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(executor.execute(payload))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------
@app.route("/api/positions")
def api_positions():
    try:
        state = log.load_state()
        return jsonify({
            "positions": position_manager.positions_view(state),
            "capital": position_manager.capital_summary(state),
            "extrinsic_payback": state.get("extrinsic_payback", {}),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/theta-ledger")
def api_theta_ledger():
    ticker = request.args.get("ticker")
    period = request.args.get("period")  # week | month | ytd
    try:
        state = log.load_state()
        ledger = state.get("theta_ledger", {})
        weeks = ledger.get("weeks", [])
        if ticker:
            weeks = [w for w in weeks if w.get("ticker", "").upper() == ticker.upper()]
        totals = ledger.get("totals", {})
        out = {"weeks": weeks, "totals": totals, "extrinsic_payback": state.get("extrinsic_payback", {})}
        if period in ("week", "month", "ytd"):
            key = {"week": "this_week", "month": "this_month", "ytd": "ytd"}[period]
            out["period"] = {"period": period, "net_juice": totals.get(key)}
        return jsonify(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/kill-switch")
def api_kill_switch():
    try:
        return jsonify({"positions": kill_switch.evaluate_all(log.load_state())})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/daily-checklist")
def api_daily_checklist():
    try:
        return jsonify({"items": screening.daily_checklist(log.load_state())})
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# State / config
# ---------------------------------------------------------------------------
@app.route("/api/state", methods=["GET", "POST"])
def api_state():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        try:
            state = log.load_state()
            # Only metadata + thesis-style fields are user-editable here.
            if "metadata" in payload:
                state.setdefault("metadata", {}).update(payload["metadata"])
            log.recompute_derived(state)
            return jsonify(log.save_state(state))
        except Exception as e:  # noqa: BLE001
            return _err(e)
    return jsonify(log.load_state())


@app.route("/api/config")
def api_config():
    return jsonify({
        "benchmark": config.BENCHMARK,
        "sectors": {etf: s.as_dict() for etf, s in sector_data.sectors().items()},
        "thresholds": {
            "regime_breadth_green": config.REGIME_BREADTH_GREEN,
            "vix_calm": config.VIX_CALM,
            "sector_rs3m_min": config.SECTOR_RS3M_MIN,
            "stock_rs_vs_spy_min": config.STOCK_RS_VS_SPY_MIN,
            "stock_rs_vs_sector_min": config.STOCK_RS_VS_SECTOR_MIN,
        },
        "cfm": {
            "leap_contracts": config.LEAP_CONTRACTS,
            "leap_target_delta": config.LEAP_TARGET_DELTA,
            "leap_target_dte": config.LEAP_TARGET_DTE,
            "short_atr_mult": config.SHORT_ATR_MULT,
            "share_cap": config.SHARE_CAP,
        },
        "live_trading": executor.live_enabled(),
        "schwab": schwab_api.token_status(),
        "alpha_vantage_configured": __import__("alpha_vantage").configured(),
    })


@app.route("/api/data-status")
def api_data_status():
    syms = [config.BENCHMARK, config.VIX_SYMBOL] + sector_data.sector_etfs()
    return jsonify({s: {"cache_age_hours": data_handler.cache_age_hours(s)} for s in syms})


# ---------------------------------------------------------------------------
# Schwab OAuth (hosted re-auth)
# ---------------------------------------------------------------------------
@app.route("/api/account/status")
def api_account_status():
    return jsonify(schwab_api.token_status())


def _callback_uri() -> str:
    """The OAuth callback URL. Fly terminates TLS, so request.url_root can come
    back as http://; force https (except on localhost) so it matches the https
    callback registered with the Schwab app and used in the authorize request."""
    root = request.url_root.rstrip("/")
    if root.startswith("http://") and not any(h in root for h in ("localhost", "127.0.0.1")):
        root = "https://" + root[len("http://"):]
    return root + "/auth/schwab/callback"


@app.route("/auth/schwab")
def auth_schwab():
    try:
        state = secrets.token_urlsafe(16)
        return jsonify({"authorize_url": schwab_api.authorize_url(_callback_uri(), state)})
    except Exception as e:  # noqa: BLE001
        return _err(e, 400)


@app.route("/auth/schwab/callback")
def auth_schwab_callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?schwab=error&msg=missing+authorization+code")
    try:
        tokens = schwab_api.exchange_code(code, _callback_uri())
        schwab_api.store_refresh_token(tokens["refresh_token"])
        return redirect("/?schwab=connected")
    except Exception as e:  # noqa: BLE001
        from urllib.parse import quote
        return redirect(f"/?schwab=error&msg={quote(str(e)[:200])}")


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.route("/")
@app.route("/<path:path>")
def serve_frontend(path: str = ""):
    if path and os.path.exists(os.path.join(DIST_DIR, path)):
        return send_from_directory(DIST_DIR, path)
    index = os.path.join(DIST_DIR, "index.html")
    if os.path.exists(index):
        return send_from_directory(DIST_DIR, "index.html")
    return jsonify({"error": "frontend not built — run `npm run build` in frontend/"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5179)), debug=True)
