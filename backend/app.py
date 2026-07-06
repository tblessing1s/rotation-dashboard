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

import alert_scheduler
import alerts
import auth
import config
import data_handler
import earnings
import executor
import kill_switch
import logging_handler as log
import option_chain
import position_manager
import schwab_api
import screening
import sector_data
import strike_policy
import webpush

DIST_DIR = os.path.join(config.REPO_DIR, "frontend", "dist")

app = Flask(__name__, static_folder=None)
CORS(app)
auth.init_app(app)


@app.before_request
def _auth_gate():
    return auth.gate()


def _err(e: Exception, code: int = 500):
    return jsonify({"error": str(e)}), code


# ---------------------------------------------------------------------------
# Auth (single-user password gate; see auth.py)
# ---------------------------------------------------------------------------
@app.route("/api/auth/status")
def api_auth_status():
    return jsonify({"required": auth.enabled(), "authenticated": auth.is_authenticated()})


@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(silent=True) or {}
    if auth.verify_password(payload.get("password", "")):
        auth.login()
        return jsonify({"ok": True})
    return jsonify({"error": "invalid password"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    auth.logout()
    return jsonify({"ok": True})


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


@app.route("/api/scan/scorecard")
def api_scorecard():
    """Numeric CFM scorecard, one row per ticker (default: all holdings). Optional
    ?tickers=AAPL,MSFT narrows it to a subset."""
    raw = request.args.get("tickers")
    tickers = [t for t in raw.split(",") if t.strip()] if raw else None
    try:
        from metrics import scorecard as scorecard_metrics
        return jsonify(scorecard_metrics.scorecard(tickers))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/ready")
def api_scan_ready():
    """Tickers that clear Level 3 (beats peers), Level 4 (consolidating), AND
    Level 5 (Account & Juice) right now — a ready-to-enter shortlist.

    Level 1 (market regime) and Level 2 (sector strength) are deliberately
    excluded, same as the Scorecard's own verdict: they're market-wide
    context, not a property of the stock, so this stays a useful relative
    ranking even on a yellow/red tape. RED still hard-blocks actual execution
    regardless of what appears here (Level 1 entry-gate rule, unchanged).

    Only evaluates Level 5 for tickers the Scorecard already verdicts GO (a
    proxy for clearing gate levels 3 & 4 plus its own CFM-suitability rules)
    — cheaper than running Level 5 across the whole universe, and consistent
    with "GO" already meaning stock-level-ready. Juice numbers are always the
    history-implied estimate (no live chain in a bulk sweep); optional
    ?contracts= sizes the capital/reserve checks (default LEAP_CONTRACTS)."""
    raw = request.args.get("tickers")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()] if raw else None
    contracts = int(request.args.get("contracts") or 0) or None
    try:
        from metrics import scorecard as scorecard_metrics
        import account_gate
        sc = scorecard_metrics.scorecard(tickers)
        go_rows = [r for r in sc["results"] if r["verdict"] == "GO"]
        level5 = account_gate.evaluate_many([r["ticker"] for r in go_rows], contracts=contracts)

        ready, near_misses = [], []
        for r in go_rows:
            l5 = level5.get(r["ticker"])
            entry = {"ticker": r["ticker"], "sector": r["sector"],
                     "juice_weekly_pct": r.get("juice_weekly_pct"),
                     "earnings_date": r.get("earnings_date"), "level5": l5}
            (ready if l5 and l5["pass"] else near_misses).append(entry)
        ready.sort(key=lambda r: r.get("juice_weekly_pct") or 0, reverse=True)
        return jsonify({"as_of": sc["as_of"], "ready": ready, "near_misses": near_misses})
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


@app.route("/api/account-gate")
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


@app.route("/api/option-chain/<ticker>")
def api_option_chain(ticker: str):
    strategy = request.args.get("strategy", "atr")
    try:
        return jsonify(option_chain.option_chain(ticker, strategy))
    except option_chain.RegimeBlocked as e:
        return jsonify({"error": str(e), "regime": "red"}), 403
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/defend")
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


@app.route("/api/leap-roll-estimate")
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


@app.route("/api/strike-posture", methods=["GET", "POST"])
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


@app.route("/api/roll-suggestion")
def api_roll_suggestion():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(executor.roll_suggestion(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/roll-options")
def api_roll_options():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(option_chain.roll_options(ticker))
    except option_chain.RegimeBlocked as e:
        return jsonify({"error": str(e), "regime": "red"}), 403
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/coverage")
def api_coverage():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        return jsonify(option_chain.coverage(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/earnings")
def api_earnings():
    ticker = request.args.get("ticker", "")
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    refresh = request.args.get("refresh") in ("1", "true", "yes")
    try:
        return jsonify(earnings.next_earnings(ticker, refresh=refresh))
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
    except executor.PositionFrozenError as e:
        # 409 (distinct from the 400 gate-rejection): the position is frozen for
        # reconciliation review. The diff summary rides in the body. Closing
        # actions are never rejected here, so the operator can still exit.
        return jsonify({"error": str(e), "frozen": True, "ticker": e.ticker,
                        "review": e.review}), 409
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/order-status")
def api_order_status():
    order_id = request.args.get("order_id", "")
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    try:
        return jsonify(executor.order_status(order_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/order-cancel", methods=["POST"])
def api_order_cancel():
    payload = request.get_json(silent=True) or {}
    order_id = payload.get("order_id", "")
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    try:
        return jsonify(executor.cancel_order(order_id))
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
        roll_ledger = state.get("roll_ledger", {"rolls": [], "by_ticker": {}})
        if ticker:
            roll_ledger = {
                "rolls": [r for r in roll_ledger.get("rolls", [])
                          if r.get("ticker", "").upper() == ticker.upper()],
                "by_ticker": {k: v for k, v in roll_ledger.get("by_ticker", {}).items()
                              if k.upper() == ticker.upper()},
            }
        out = {"weeks": weeks, "totals": totals,
               "extrinsic_summary": ledger.get("extrinsic_summary", {}),
               "extrinsic_payback": state.get("extrinsic_payback", {}),
               "roll_ledger": roll_ledger}
        if period in ("week", "month", "ytd"):
            key = {"week": "this_week", "month": "this_month", "ytd": "ytd"}[period]
            out["period"] = {"period": period, "net_juice": totals.get(key)}
        return jsonify(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/history")
def api_history():
    """Closed-cycle records + aggregate stats + the weekly net-juice chart."""
    try:
        import history
        return jsonify(history.view(log.load_state()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/export/juice-journal")
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
        return app.response_class(
            body, mimetype=mime,
            headers={"Content-Disposition": f"attachment; filename={name}"})
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
# Alerts
# ---------------------------------------------------------------------------
@app.route("/api/alerts")
def api_alerts():
    try:
        return jsonify(alerts.view())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/alerts/run", methods=["POST"])
def api_alerts_run():
    """Force one evaluator pass now. Also the external-cron entry point: hitting
    this URL wakes a stopped Fly machine, and dedup makes repeat runs no-ops."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(alerts.run(dry_run=payload.get("dry_run")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/alerts/ack", methods=["POST"])
def api_alerts_ack():
    payload = request.get_json(silent=True) or {}
    alert_id = payload.get("id", "")
    if not alert_id:
        return jsonify({"error": "id is required"}), 400
    try:
        return jsonify(alerts.acknowledge(alert_id))
    except ValueError as e:
        return _err(e, 404)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/alerts/settings", methods=["POST"])
def api_alerts_settings():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(alerts.update_settings(payload))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Web Push (PWA native push): VAPID key handshake + subscription registry.
# ---------------------------------------------------------------------------
@app.route("/api/iv-rank")
def api_iv_rank():
    """IV rank (current IV vs the ticker's own trailing-year range) — where this
    week's premium sits in its own history, the signal juice-adequacy can't see."""
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        import iv_history
        return jsonify(iv_history.iv_rank(ticker))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/verify-fills", methods=["POST"])
def api_verify_fills():
    """Re-fetch recent live orders from Schwab and diff their fills against what
    we recorded, plus a reconcile pass. The live-order verification harness."""
    import fill_verify
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(fill_verify.verify_live_fills(limit=int(payload.get("limit", 20))))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/push/vapid-key")
def api_push_vapid_key():
    """The applicationServerKey the browser needs to subscribe, plus whether the
    server is configured and how many devices are currently registered."""
    return jsonify({
        "key": webpush.public_key(),
        "configured": webpush.keys_configured(),
        "subscriptions": webpush.subscription_count(),
    })


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    """Store a browser PushSubscription so alert batches reach this device."""
    payload = request.get_json(silent=True) or {}
    sub = payload.get("subscription") or payload
    try:
        return jsonify(webpush.add_subscription(sub))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint", "")
    if not endpoint:
        return jsonify({"error": "endpoint is required"}), 400
    try:
        return jsonify(webpush.remove_subscription(endpoint))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    """Send a test push to every registered device — confirms the phone wiring
    without waiting for a real alert to trip."""
    if not webpush.keys_configured():
        return jsonify({"error": "VAPID keys not configured on the server"}), 400
    if webpush.subscription_count() == 0:
        return jsonify({"error": "no device subscribed yet"}), 400
    try:
        webpush.send("[CFM] Test alert",
                     "Push is wired up — real alerts will arrive here.", [])
        return jsonify({"ok": True, "devices": webpush.subscription_count()})
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Reconciliation (state.json vs Schwab)
# ---------------------------------------------------------------------------
@app.route("/api/reconcile", methods=["GET", "POST"])
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


@app.route("/api/reconcile/resolve-expiry", methods=["POST"])
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


@app.route("/api/reconcile/acknowledge", methods=["POST"])
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


@app.route("/api/mode", methods=["GET", "POST"])
def api_mode():
    """Read or set the demo/live data switch. Setting it points the app at the
    separate demo store (seeding it on first use) or back at the live store, and
    clears the in-memory scan/data caches so the next reads reflect the switch."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        demo = bool(payload.get("demo"))
        seeded = False
        try:
            config.set_demo_enabled(demo)
            screening.clear_cache()
            data_handler.reset_caches()
            if demo:
                import seed_demo_data
                seeded = seed_demo_data.ensure_seeded()
            return jsonify({"demo": config.demo_enabled(), "seeded": seeded})
        except Exception as e:  # noqa: BLE001
            return _err(e)
    return jsonify({"demo": config.demo_enabled()})


@app.route("/api/config")
def api_config():
    return jsonify({
        "demo": config.demo_enabled(),
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
            "strike_table": config.STRIKE_TABLE,
            "strike_posture": strike_policy.get_posture(),
        },
        "live_trading": executor.live_enabled(),
        "schwab": schwab_api.token_status(),
        "alpha_vantage_configured": __import__("alpha_vantage").configured(),
    })


@app.route("/api/version")
def api_version():
    """Build identity: {version, commit, built_at}. Open (no auth) so the login
    screen and external health checks can read it without a session."""
    import version
    return jsonify(version.info())


@app.route("/api/data-status")
def api_data_status():
    syms = [config.BENCHMARK, config.VIX_SYMBOL] + sector_data.sector_etfs()
    return jsonify({s: {"cache_age_hours": data_handler.cache_age_hours(s)} for s in syms})


@app.route("/api/portfolio-risk")
def api_portfolio_risk():
    """Aggregate book exposure: delta (raw + SPY-beta-adjusted), theta/day,
    vega, capital vs cap, reserve status, sector exposure breakdown."""
    try:
        import portfolio_risk
        return jsonify(portfolio_risk.portfolio_view(log.load_state()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/data-health")
def api_data_health():
    """Last-successful-fetch per source + cache staleness, so silent data
    failures are visible instead of quietly serving stale frames."""
    try:
        import dividends
        import refresh_policy
        state = log.load_state()
        # Report cache age for the hot set (positions + live candidates) — those
        # are the names whose staleness actually matters intraday.
        hot = refresh_policy.hot_tickers(state)
        key_syms = [config.BENCHMARK, config.VIX_SYMBOL] + hot
        return jsonify({
            "providers": data_handler.health(),
            "ohlcv_cache_age_hours": {s: data_handler.cache_age_hours(s)
                                      for s in dict.fromkeys(s for s in key_syms if s)},
            "hot_refresh": refresh_policy.status(),
            "earnings_cache": earnings.cache_health(),
            "dividends_cache": dividends.cache_health(),
            "schwab_token": schwab_api.token_status(),
            "demo": config.demo_enabled(),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/universe-health")
def api_universe_health():
    """Sweep the whole ticker universe and report dead names (no provider data —
    renamed/delisted/typo'd) and, with ?weeklies=1, names that lack weekly
    options (can't run CFM). On-demand only — fetches OHLCV for every ticker."""
    try:
        import universe_health
        weeklies = request.args.get("weeklies", "").strip() in ("1", "true", "yes")
        return jsonify(universe_health.check(check_weeklies=weeklies))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/universe", methods=["GET"])
def api_universe():
    """The ticker universe (editable JSON store on the volume): sectors with
    their constituents. Managed via /api/universe/add and /remove."""
    try:
        secs = sector_data.sectors()
        return jsonify({
            "sectors": [{"etf": s.etf, "name": s.name, "group": s.group,
                         "tickers": list(s.tickers), "count": len(s.tickers)}
                        for s in secs.values()],
            "total": sum(len(s.tickers) for s in secs.values()),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/universe/add", methods=["POST"])
def api_universe_add():
    """Add a constituent to a sector: {ticker, sector}."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(sector_data.add_ticker(payload.get("ticker", ""), payload.get("sector", "")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/universe/remove", methods=["POST"])
def api_universe_remove():
    """Remove from the universe. {ticker} for one, or {tickers:[...]} to bulk
    remove (e.g. 'remove all dead' after a universe health check)."""
    payload = request.get_json(silent=True) or {}
    try:
        if isinstance(payload.get("tickers"), list):
            return jsonify(sector_data.remove_tickers(payload["tickers"]))
        return jsonify(sector_data.remove_ticker(payload.get("ticker", "")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/universe/sync", methods=["POST"])
def api_universe_sync():
    """Additively pull any new names from the baked-in seed file into the store
    (e.g. after ETFs / S&P additions were added to the seed). Respects the
    operator's removals (tombstoned); never removes or moves anything."""
    try:
        return jsonify(sector_data.sync_from_seed())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/universe/vet", methods=["POST"])
def api_universe_vet():
    """Vet candidate symbols against the CFM criteria (data + weeklies + Scorecard
    verdict): {symbols: [...] or "AAPL, MSFT"}. Returns which are add-ready."""
    payload = request.get_json(silent=True) or {}
    syms = payload.get("symbols")
    if isinstance(syms, str):
        import re
        syms = [s for s in re.split(r"[,\s]+", syms) if s]
    if not isinstance(syms, list):
        return jsonify({"error": "symbols must be a list or a comma/space-separated string"}), 400
    try:
        import universe_health
        return jsonify(universe_health.vet_candidates(syms))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/maintenance/refresh", methods=["POST"])
def api_maintenance_refresh():
    """Force the nightly earnings/dividends refresh now (also runs on the
    scheduler's MAINTENANCE_ET slot)."""
    try:
        import maintenance
        return jsonify(maintenance.nightly_refresh())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/refresh/hot", methods=["POST"])
def api_refresh_hot():
    """Force-refresh the hot set (open positions + live entry/earnings candidates)
    daily bars now, bypassing the freshness window. The scheduler does this
    automatically on the HOT_REFRESH_MINUTES cadence during market hours; this is
    the on-demand path for 'refresh these stocks now'."""
    try:
        import refresh_policy
        return jsonify(refresh_policy.maybe_refresh_hot(force=True))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/diagnostics/vix")
def api_diag_vix():
    """Live, cache-bypassing probe of the VIX so a missing value can be
    diagnosed: token health, the raw Schwab quote, and the daily-bars result."""
    out = {"symbol": config.VIX_SYMBOL, "token": schwab_api.token_status(),
           "schwab_configured": schwab_api.configured()}
    try:
        out["quote"] = data_handler.client().get_quote(config.VIX_SYMBOL)
    except Exception as e:  # noqa: BLE001
        out["quote_error"] = str(e)
    try:
        df = data_handler.get_daily(config.VIX_SYMBOL, force=True)
        out["daily_rows"] = 0 if df is None else len(df)
    except Exception as e:  # noqa: BLE001
        out["daily_error"] = str(e)
    out["last_error"] = data_handler.last_error(config.VIX_SYMBOL)
    return jsonify(out)


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


# Durability startup check: clear orphaned write-temp files and eagerly load the
# active store so a corrupt state.json fails fast HERE (refuse to serve) instead
# of silently re-initializing empty state over the live trading record. Skipped
# only if explicitly disabled (some one-off scripts import app without a store).
if os.environ.get("CFM_SKIP_STARTUP_CHECK", "").strip() not in ("1", "true", "yes"):
    log.startup_check()

# Start the in-process alert scheduler (gunicorn imports this module; the CLI
# path below reaches it too). start_once() is idempotent and a no-op when
# CFM_ALERTS_SCHEDULER=0 (tests / one-off scripts).
alert_scheduler.start_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5179)), debug=True)
