"""CFM dashboard Flask backend.

Serves the built React frontend and the CFM API: scan (regime/sectors/stock
filter) -> entry gate -> execute (Schwab + auto-log) -> track (positions/theta
ledger/kill switch/checklist). state.json is the source of truth; the only route
that contacts a provider live is the Schwab account/quote path used at execution.
"""
from __future__ import annotations

import logging
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


@app.route("/api/scan/refresh", methods=["POST"])
def api_scan_refresh():
    """Start a full-universe scan in a detached server-side job (deduped — one at
    a time) and return its status immediately. Because the sweep runs off-request,
    it keeps going even if the client tab is backgrounded, switched, or closed;
    the client polls /api/scan/status and reads results warm when it returns."""
    try:
        return jsonify(screening.start_background_scan())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/status")
def api_scan_status():
    """Poll the background scan: running / done / error, timestamps, and whether
    the memoized results are warm (ready to render)."""
    try:
        return jsonify(screening.scan_status())
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
    """Tickers whose canonical scan VERDICT is READY (the FULL Level 1–4 gate
    clears — regime + sector + stock lights + structure + the Level-4 right spot),
    then layered with Level 5 (Account & Juice) — a ready-to-enter shortlist.

    Verdict-completeness (Phase-0 fix): the READY verdict now consumes the whole
    L1–L4 gate, so a name extended past the Level-4 right spot is WATCH, never
    READY — it can no longer reach this shortlist (the AAPL "READY + fails level 4"
    bug). Level 5 is the account overlay evaluated here, with the SAME account
    context Execute uses (account_gate loads state + resolves live cash once); a
    name that clears L1–L4 but fails L5 lands in ``near_misses`` with its L5
    triggers so the operator sees the path (earnings date, sector slot, cash).

    Juice numbers are always the history-implied estimate (no live chain in a bulk
    sweep); optional ?contracts= sizes the capital/reserve checks
    (default LEAP_CONTRACTS)."""
    raw = request.args.get("tickers")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()] if raw else None
    contracts = int(request.args.get("contracts") or 0) or None
    try:
        from metrics import scorecard as scorecard_metrics
        import account_gate
        import data_cache
        import market_scheduler
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        sc = scorecard_metrics.scorecard(tickers)
        # Ready-to-Enter = the canonical scan VERDICT is READY (the invisible market
        # regime + Symbol Genius + structure entrability all clear). A RED regime
        # forces every verdict to BLOCKED, so the shortlist correctly empties on a
        # red tape — the invisible-regime rule, now enforced here.
        ready_rows = [r for r in sc["results"] if r.get("verdict") == "READY"]
        level5 = account_gate.evaluate_many([r["ticker"] for r in ready_rows], contracts=contracts)

        # HARD_CFM_RULE (STALE_BLOCKS_GO): a GO that the operator would act on must
        # not be emitted on stale inputs. Only enforced once the tiered scheduler is
        # actually populating quotes (data_cache.active), and only in a live, open-
        # market context — a bulk warm scan legitimately has no live quotes and must
        # behave as before. Blocked names are surfaced separately, never silently.
        now_et = _dt.now(_ZI("America/New_York"))
        mkt_open = market_scheduler.is_market_open(now_et)
        live = mkt_open and not config.demo_enabled() and data_cache.active()

        # On-demand quote fetch: the tiered poller only quotes open positions,
        # on-deck queue names, and held sector ETFs, so a fresh GO that isn't
        # queued for a slot has no live quote and would be perpetually
        # stale-blocked below. When live, pull a live quote for exactly the GO
        # names that lack a fresh one, so this shortlist reflects what the
        # operator could actually enter — not just what happens to be on-deck.
        if live and ready_rows:
            import data_transport
            from market_scheduler import QUOTE as _QUOTE
            need = [r["ticker"] for r in ready_rows
                    if data_cache.get_with_staleness(
                        r["ticker"], _QUOTE, tier=market_scheduler.Tier.T1)[2]]
            if need:
                try:
                    data_transport.fetch_quotes_batched(
                        {s: market_scheduler.Tier.T1 for s in need})
                except Exception as fe:  # noqa: BLE001 — scan still returns on a miss
                    logging.getLogger("cfm.app").warning(
                        "scan_ready on-demand quote fetch failed: %s", fe)

        import scan_triggers
        ready, near_misses, stale_blocked = [], [], []
        for r in ready_rows:
            l5 = level5.get(r["ticker"])
            # Level-5 account overlay: classify the L5 blocking failures into the
            # same forward triggers the table uses (earnings CALENDAR, sector-slot
            # CONDITIONAL, cash/capital CONDITIONAL) so a near-miss shows its path.
            l5_blocks = scan_triggers.gate_blocks(None, account_gate=l5)
            l5_triggers = scan_triggers.triggers_for_blocks(l5_blocks)
            blocked, stale_inputs = data_cache.stale_blocks_go(
                r["ticker"], market_scheduler.Tier.T1, market_open=mkt_open, live=live)
            entry = {"ticker": r["ticker"], "sector": r["sector"],
                     "juice_weekly_pct": r.get("juice_weekly_pct"),
                     "net_juice_weekly_pct": r.get("net_juice_weekly_pct"),
                     "earnings_date": r.get("earnings_date"), "level5": l5,
                     # The per-symbol scan cells (SYM | BASE | INST | VERDICT) so the
                     # shortlist can show the same read as the table.
                     "sym": r.get("sym"), "base_stage": r.get("base_stage"),
                     "inst_flow": r.get("inst_flow"), "verdict": r.get("verdict"),
                     # Per-name Genius lights so the shortlist can show the four-light
                     # row + verdict + right-spot at a glance (same data as the gate).
                     "lights": r.get("lights"), "stock_greens": r.get("stock_greens"),
                     "stock_verdict": r.get("stock_verdict"),
                     "stock_vetoes": r.get("stock_vetoes"), "right_spot": r.get("right_spot"),
                     # L5 overlay triggers + rendered path (near-miss "path to READY").
                     "l5_triggers": l5_triggers,
                     "l5_path_to_ready": scan_triggers.path_to_ready(l5_triggers),
                     "l5_eligible_days": scan_triggers.earliest_eligible_days(l5_triggers),
                     "stale": blocked, "stale_inputs": stale_inputs}
            if blocked:
                stale_blocked.append(entry)
            else:
                (ready if l5 and l5["pass"] else near_misses).append(entry)
        # Rank on NET juice/week (gross minus LEAP burn) — never gross. Fall back
        # to gross only when net is unavailable so a pricing gap can't drop a name.
        ready.sort(key=lambda r: (r.get("net_juice_weekly_pct")
                                  if r.get("net_juice_weekly_pct") is not None
                                  else r.get("juice_weekly_pct") or 0), reverse=True)
        return jsonify({"as_of": sc["as_of"], "ready": ready, "near_misses": near_misses,
                        "stale_blocked": stale_blocked})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/refresh-quote", methods=["POST"])
def api_scan_refresh_quote():
    """Force a live data pull for SPECIFIC Ready-to-Enter names — the per-row
    'live scan this stale name' action.

    The tiered poller only quotes on-deck/held/sector-ETF names, so a stale-tagged
    GO in the shortlist may have an absent or aged quote. This force-refreshes the
    named tickers' daily bars AND pulls a live quote through the transport layer
    (which, unlike data_handler.live_prices, records genuine Schwab/Alpha Vantage
    quotes into the staleness store) so both STALE_BLOCKS_GO inputs go fresh and
    the name can clear on the next scan. Returns each ticker's post-pull quote
    source and remaining staleness so the UI can show what actually went live —
    a provider miss that only yields a cached close stays visibly stale."""
    body = request.get_json(silent=True) or {}
    raw = body.get("tickers") if body.get("tickers") is not None else body.get("ticker")
    if isinstance(raw, str):
        raw = [raw]
    tickers = [t.strip().upper() for t in (raw or []) if t and str(t).strip()]
    if not tickers:
        return jsonify({"error": "tickers is required"}), 400
    try:
        import data_cache
        import data_transport
        import market_scheduler
        # Bars first (parquet mtime -> bars leg fresh), then a live quote batch that
        # records into the staleness store. Both are best-effort per the transport.
        data_handler.prefetch(tickers, force=True)
        fetched = data_transport.fetch_quotes_batched(
            {t: market_scheduler.Tier.T1 for t in tickers})
        results = {}
        for t in tickers:
            blocked, stale_inputs = data_cache.stale_blocks_go(
                t, market_scheduler.Tier.T1, market_open=True, live=True)
            results[t] = {"stale": blocked, "stale_inputs": stale_inputs,
                          "quote_source": (fetched["quotes"].get(t) or {}).get("source")}
        return jsonify({"tickers": tickers, "results": results,
                        "degraded": fetched.get("degraded", [])})
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


@app.route("/api/burn/<ticker>")
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
    except executor.ResubmitLockedError as e:
        # 409: the resubmission gate blocked a new live order for this position
        # intent — a prior order isn't confirmed terminal at the broker yet (or the
        # per-session attempt cap is hit). In addition to the freeze/gate/kill-switch.
        return jsonify({"error": str(e), "resubmit_locked": True,
                        "intent": e.intent_key, "reason": e.reason}), 409
    except executor.ExecutionWindowError as e:
        # 409: the market-settle execution gate deferred the order (settle window /
        # close blackout / off-hours). The UI stages it as PENDING_SETTLE and shows
        # the countdown to executable_at; the alert already fired.
        return jsonify({"error": str(e), "execution_deferred": True,
                        "reason": e.reason, "ticker": e.ticker,
                        "action": e.gate_action,
                        "executable_at": (e.executable_at.isoformat()
                                          if e.executable_at else None)}), 409
    except executor.SpreadAckRequiredError as e:
        # 409: spread abnormally wide vs the trailing baseline — the operator must
        # acknowledge the estimated excess slippage (resend with spread_ack: true).
        return jsonify({"error": str(e), "spread_ack_required": True,
                        "ticker": e.ticker, "current_spread": e.current_spread,
                        "baseline_spread": e.baseline_spread,
                        "est_excess_slippage_usd": e.est_excess_slippage_usd}), 409
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


@app.route("/api/order-submission-status")
def api_order_submission_status():
    """MANUAL status check for a client_order_ref (incident hotfix, D2/D4). Resolves
    an order whose broker outcome isn't yet confirmed — recovers a missing orderId by
    recent-orders match and syncs the durable record to the broker truth. Never
    auto-retries the submission; the operator drives it. Reading it never lies:
    UNKNOWN stays 'confirming', a rejection carries Schwab's verbatim reason."""
    ref = request.args.get("ref", "") or request.args.get("client_order_ref", "")
    if not ref:
        return jsonify({"error": "ref is required"}), 400
    try:
        return jsonify(executor.submission_status(ref))
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


@app.route("/api/payouts")
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


@app.route("/api/payouts/finalize", methods=["POST"])
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


@app.route("/api/payouts/unfinalize", methods=["POST"])
def api_payouts_unfinalize():
    """Undo a finalize (also clears paid state on that month)."""
    payload = request.get_json(silent=True) or {}
    try:
        import payouts
        return jsonify(payouts.unfinalize(payload.get("month")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/payouts/mark-paid", methods=["POST"])
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


@app.route("/api/payouts/unmark-paid", methods=["POST"])
def api_payouts_unmark_paid():
    """Undo a mark-paid (fat-finger recovery)."""
    payload = request.get_json(silent=True) or {}
    try:
        import payouts
        return jsonify(payouts.unmark_paid(payload.get("month")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/slippage")
def api_slippage():
    """Realized paper-fill slippage vs the quoted mid (mid-fill caveat + haircut)."""
    try:
        import slippage
        return jsonify(slippage.report(log.load_state()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/history")
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


@app.route("/api/positions/set-legs", methods=["POST"])
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


@app.route("/api/transactions/save", methods=["POST"])
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


@app.route("/api/positions/repair-leap-cost", methods=["POST"])
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


@app.route("/api/executions/raw")
def api_executions_raw():
    """Raw, unprocessed data for validation: the append-only execution log
    (newest first, capped) plus each position's LIVE derived legs (short_calls /
    leap_legs / shares). Read-only. Lets the operator eyeball exactly what state
    holds — e.g. spot a duplicate short leg or a leg with no entry extrinsic."""
    try:
        state = log.load_state()
        execs = list(reversed(state.get("executions", [])))[:300]
        positions = [{
            "ticker": p.get("ticker"),
            "status": p.get("status"),
            "needs_review": bool(p.get("needs_review")),
            "short_calls": p.get("short_calls") or [],
            "leap_legs": log.leap_legs(p),
            "shares": p.get("shares") or {},
        } for p in state.get("positions", [])]
        return jsonify({"executions": execs, "positions": positions,
                        "execution_count": len(state.get("executions", []))})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/executions/void", methods=["POST"])
def api_executions_void():
    """Void (exclude) or restore executions — an append-only soft delete for
    pruning pre-trading test/setup entries. Voided executions drop out of history
    + derived ledgers but stay on the immutable log."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
    if not ids:
        return jsonify({"error": "ids is required"}), 400
    try:
        if payload.get("restore"):
            return jsonify(executor.restore_executions(ids))
        return jsonify(executor.void_executions(ids, payload.get("reason")))
    except ValueError as e:
        return _err(e, 400)
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


@app.route("/api/symbol-genius/flips")
def api_symbol_genius_flips():
    """Symbol Genius flip-frequency shadow-log — how often each tracked name's SYM
    color changed over the retained window. The measurement that must precede any
    decision to add a per-symbol yellow dwell (does SYM churn enough to warrant
    one?). Read-only telemetry; empty until the nightly sweep has logged a few days."""
    try:
        import symbol_genius_history
        return jsonify(symbol_genius_history.flip_stats())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/rejection-stats")
def api_scan_rejection_stats():
    """Scan rejection-reason calibration rollup — the distribution of binding
    constraints and the READY rate over the retained window (the empirical read on
    whether the entry gate is too strict, plus the RS/SCORE graduation dataset).
    Optional ?window=N bounds each symbol to its newest N records. Read-only
    telemetry; empty until the nightly sweep has logged a few days."""
    try:
        import scan_rejection_log
        window = int(request.args.get("window") or 0) or None
        return jsonify(scan_rejection_log.summary(window=window))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/overview")
def api_overview():
    """One-call landing payload for the Overview tab: regime + positions/capital
    + theta totals/payback + kill-switch, pre-joined server-side so the landing
    screen renders from a single fetch instead of stitching four.

    Sections are best-effort independent — a data-provider hiccup in one (e.g.
    regime needs fresh SPY/VIX bars) must not blank the position-derived rest,
    so a failed section carries {"error": ...} instead of failing the request."""
    def section(fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    try:
        state = log.load_state()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    ledger = state.get("theta_ledger", {})
    positions = section(lambda: position_manager.positions_view(state))
    return jsonify({
        "regime": section(screening.regime),
        "positions": positions,
        "capital": section(lambda: position_manager.capital_summary(state)),
        "theta": {
            "totals": ledger.get("totals", {}),
            "extrinsic_payback": state.get("extrinsic_payback", {}),
            # Forward NET juice/week rollup (juice - LEAP burn), the headline
            # income figure; extrinsic_payback stays as the capital-recovery view.
            "net_juice_rollup": (position_manager.net_juice_rollup(positions)
                                 if isinstance(positions, list) else {}),
            # The 1-2%/week-of-deployed target band (HARD_CFM_RULE), so the
            # Overview can show this week's juice against pace without a second
            # call — same formula the History weekly chart uses.
            "weekly_target": section(lambda: {
                "target_low": round(position_manager.deployed_capital(state)
                                    * config.WEEKLY_JUICE_TARGET_PCT_MIN / 100, 2),
                "target_high": round(position_manager.deployed_capital(state)
                                     * config.WEEKLY_JUICE_TARGET_PCT_MAX / 100, 2),
            }),
        },
        # Live BS-engine verification harness: realized-vs-projected burn drift.
        "burn_divergence": section(lambda: __import__("burn_marks").aggregate_divergence()),
        "kill_switch": section(lambda: kill_switch.evaluate_all(state)),
        # Monthly payout glance: this month's estimated payout + last month's, so
        # the landing shows "what the payout is going to be" without a second call.
        "payouts": section(lambda: _payouts_glance(state)),
    })


def _payouts_glance(state: dict) -> dict:
    """The compact current+previous payout figures for the Overview landing,
    pulled from the payouts view (full detail lives on the Payouts tab)."""
    import payouts
    v = payouts.view(state)
    keep = ("month", "label", "net_juice", "leap_burn", "burn_tracked",
            "intrinsic_lost", "intrinsic_repaid", "intrinsic_debt",
            "intrinsic_repayment_on", "net_payout", "payout_amount", "status",
            "finalizable", "finalized", "paid", "estimated")
    return {
        "current": {k: v["current"].get(k) for k in keep},
        "previous": {k: v["previous"].get(k) for k in keep},
    }


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
# Recommendation trust layer: open recommendations, dismissals, the scoreboard.
# Everything served here is either an immutable record or a recompute_derived
# product — no endpoint computes a score.
# ---------------------------------------------------------------------------
@app.route("/api/recommendations")
def api_recommendations():
    """Open (unresolved, unexpired) recommendations + the last pass summary."""
    import recommendation_runner
    import recommendation_settle as settle
    import trust_derive
    from datetime import datetime, timezone
    try:
        state = log.load_state()
        now = datetime.now(timezone.utc)
        open_recs = trust_derive.open_recommendations(state, now)
        # Bars are snapshot working data, not payload — strip anything
        # non-JSON-serializable defensively (records themselves never carry
        # DataFrames, but keep the endpoint robust to engine additions).
        return jsonify({
            "open": open_recs,
            "open_actionable": [r for r in open_recs if r.get("action_type") != "NO_ACTION"],
            # PENDING_SETTLE recs carry executable_at so the card can render a
            # live countdown and a pre-approve toggle (the gate deferred the order;
            # the alert already fired).
            "pending_settle": settle.pending(state),
            "gate_enforced": config.market_settle_gate_enabled(),
            "last_run": recommendation_runner.last_run(),
            "total": len(state.get("recommendations", [])),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/recommendations/run", methods=["POST"])
def api_recommendations_run():
    """Force one evaluation pass now (the scheduled slots call the same code)."""
    import recommendation_runner
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(recommendation_runner.run(
            notify=bool(payload.get("notify", True)),
            include_entry=bool(payload.get("include_entry", True)),
            dry_run=payload.get("dry_run")))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/recommendations/dismiss", methods=["POST"])
def api_recommendations_dismiss():
    """Operator dismissal with a CODED override reason (+ optional note; OTHER
    requires one). Appends an immutable override record — the recommendation
    itself is never mutated; precision math derives from the record."""
    import rec_types
    import trust_derive
    from datetime import datetime, timezone
    payload = request.get_json(silent=True) or {}
    rec_id = str(payload.get("rec_id") or "")
    reason = str(payload.get("reason") or "").strip().upper()
    note = (payload.get("note") or "").strip() or None
    if not rec_id:
        return jsonify({"error": "rec_id is required"}), 400
    if not rec_types.is_override_reason(reason):
        return jsonify({"error": f"reason must be one of {sorted(rec_types.OVERRIDE_REASONS)}"}), 400
    if rec_types.override_requires_note(reason) and not note:
        return jsonify({"error": f"a typed note is required for {reason}"}), 400
    try:
        state = log.load_state()
        now = datetime.now(timezone.utc)
        open_ids = {r.get("rec_id") for r in trust_derive.open_recommendations(state, now)}
        if rec_id not in open_ids:
            return jsonify({"error": f"{rec_id} is not an open recommendation "
                                     "(already resolved, expired, or unknown)"}), 404
        stored = log.append_recommendation_override(
            {"rec_id": rec_id, "reason": reason, "note": note})
        return jsonify({"override": stored})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/recommendations/preapprove", methods=["POST"])
def api_recommendations_preapprove():
    """Toggle pre-approval on a PENDING_SETTLE recommendation. A pre-approved rec
    auto-submits when its settle window opens — but ONLY if its trigger re-validates
    at that moment (a filled gap self-cancels it). Body: {rec_id, approve?: bool}."""
    import recommendation_settle as settle
    from datetime import datetime, timezone
    payload = request.get_json(silent=True) or {}
    rec_id = str(payload.get("rec_id") or "")
    approve = bool(payload.get("approve", True))
    if not rec_id:
        return jsonify({"error": "rec_id is required"}), 400
    try:
        with log._lock:
            state = log.load_state()
            rec = settle.set_pre_approved(state, rec_id, approve, datetime.now(timezone.utc))
            if rec is None:
                return jsonify({"error": f"{rec_id} is not a PENDING_SETTLE recommendation "
                                         "(unknown, already released, or not deferred)"}), 404
            log.save_state(state)
        return jsonify({"rec_id": rec_id, "settle": rec.get("settle")})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/trust-scoreboard")
def api_trust_scoreboard():
    """The derived trust scoreboard: coverage / precision / timeliness /
    fidelity / graduation per action type, plus the loud lists (coverage
    misses, fidelity failures). Read-only; recompute_derived owns the math."""
    try:
        state = log.load_state()
        board = state.get("trust_scoreboard") or {}
        fidelity = state.get("order_fidelity") or {}
        return jsonify({
            "scoreboard": board,
            "fidelity_failures": [f for f in fidelity.values() if f.get("pass") is False],
            "fidelity_records": sorted(fidelity.values(),
                                       key=lambda f: f.get("graded_at") or "")[-50:],
            "resolutions": (state.get("recommendation_resolutions") or [])[-100:],
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Web Push (PWA native push): VAPID key handshake + subscription registry.
# ---------------------------------------------------------------------------
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
# Transaction ingestion (Schwab executions -> state, spec §4)
# ---------------------------------------------------------------------------
@app.route("/api/ingestion", methods=["GET", "POST"])
def api_ingestion():
    """GET: the last ingestion summary + open out-of-band adoption proposals.
    POST: run ingestion now (pulls Schwab transactions; dedupe by transaction id).
    Matched fills confirm app orders; out-of-band trades surface as proposals for
    one-click adoption — never auto-booked (NO_AUTO_REMEDIATION)."""
    if request.method == "POST":
        try:
            import transaction_ingest
            return jsonify(transaction_ingest.run_ingestion())
        except Exception as e:  # noqa: BLE001
            return _err(e)
    state = log.load_state()
    return jsonify(state.get("ingestion") or {"last": None, "proposals": []})


@app.route("/api/ingestion/adopt", methods=["POST"])
def api_ingestion_adopt():
    """Adopt one out-of-band broker trade (a proposal) into state.json, booking it
    through the same builders app fills use — economics verbatim from the broker
    record. Human-gated; the operator confirms the proposal."""
    payload = request.get_json(silent=True) or {}
    proposal_id = payload.get("proposal_id", "")
    if not proposal_id:
        return jsonify({"error": "proposal_id is required"}), 400
    try:
        return jsonify(executor.adopt_broker_trade(proposal_id, payload.get("stock_price")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/ingestion/adoptions")
def api_ingestion_adoptions():
    """List broker_manual adoptions booked into state (for the Undo control)."""
    return jsonify({"adoptions": executor.list_broker_manual_adoptions()})


@app.route("/api/ingestion/reverse", methods=["POST"])
def api_ingestion_reverse():
    """Reverse (undo) one broker_manual adoption exactly — inverts each execution
    it appended, restoring a removed LEAP leg with its original entry extrinsic."""
    payload = request.get_json(silent=True) or {}
    proposal_id = payload.get("proposal_id", "")
    if not proposal_id:
        return jsonify({"error": "proposal_id is required"}), 400
    try:
        return jsonify(executor.reverse_adoption(proposal_id, payload.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/reconcile/record-manual-roll", methods=["POST"])
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
            from_diff_id=p.get("from_diff_id"), to_diff_id=p.get("to_diff_id")))
    except (ValueError, TypeError, KeyError) as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/reconcile/rebuild-position", methods=["POST"])
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
            dry_run=bool(payload.get("dry_run")), reason=payload.get("reason")))
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/reconcile/freeze-status")
def api_reconcile_freeze_status():
    """The global reconciliation-freeze verdict (frozen tickers + reasons) plus the
    market-hours minutes staleness degrade. Drives the divergence/freeze panel and
    the 'last reconciled N minutes ago' heartbeat."""
    import reconcile
    return jsonify(reconcile.freeze_status(log.load_state()))


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


def _live_trading_status() -> dict:
    """Current live-trading state for the UI switch. `enabled` is the toggle
    (env or persisted); `transmit` is the EFFECTIVE gate — orders only reach the
    broker when live is on AND not in demo. Preconditions are surfaced so the UI
    can explain why a switched-on session might still be paper."""
    return {
        "enabled": config.live_trading_enabled(),
        "env_locked": config.live_trading_env(),
        "transmit": executor.live_transmit(),
        "demo": config.demo_enabled(),
        "schwab_configured": schwab_api.configured(),
        "schwab": schwab_api.token_status(),
    }


@app.route("/api/live-trading", methods=["GET", "POST"])
def api_live_trading():
    """Read or set the live-trading toggle. Enabling it means executed orders are
    transmitted to the real Schwab account (unless in demo mode). Locked when
    CFM_LIVE_TRADING is set in the environment."""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        try:
            config.set_live_trading_enabled(bool(payload.get("enabled")))
        except RuntimeError as e:
            return _err(e, 400)  # env-locked
        except Exception as e:  # noqa: BLE001
            return _err(e)
    return jsonify(_live_trading_status())


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
        # Effective transmit capability, NOT the raw flag: in demo mode a trade
        # never reaches the broker (see executor.live_transmit), so the Paper/Live
        # badge must read paper even when CFM_LIVE_TRADING is on. live_trading_flag
        # exposes the raw env flag for diagnostics.
        "live_trading": executor.live_transmit(),
        "live_trading_flag": executor.live_enabled(),
        "demo": config.demo_enabled(),
        "schwab": schwab_api.token_status(),
        "alpha_vantage_configured": __import__("alpha_vantage").configured(),
    })


@app.route("/api/version")
def api_version():
    """Build identity: {version, commit, built_at}. Open (no auth) so the login
    screen and external health checks can read it without a session."""
    import version
    return jsonify(version.info())


@app.route("/api/portfolio-risk")
def api_portfolio_risk():
    """Aggregate book exposure: delta (raw + SPY-beta-adjusted), theta/day,
    vega, capital vs cap, reserve status, sector exposure breakdown."""
    try:
        import portfolio_risk
        return jsonify(portfolio_risk.portfolio_view(log.load_state()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _tier_poll_status():
    """Tier-poll status for the health panel; degrades to a disabled marker if the
    runtime isn't importable (e.g. scheduler off)."""
    try:
        import tier_poll
        return {**tier_poll.status(), "recent_alerts": tier_poll.recent_alerts()}
    except Exception:  # noqa: BLE001
        return {"available": False}


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
        import data_budget
        import data_cache
        return jsonify({
            "providers": data_handler.health(),
            "ohlcv_cache_age_hours": {s: data_handler.cache_age_hours(s)
                                      for s in dict.fromkeys(s for s in key_syms if s)},
            "hot_refresh": refresh_policy.status(),
            "earnings_cache": earnings.cache_health(),
            "dividends_cache": dividends.cache_health(),
            "schwab_token": schwab_api.token_status(),
            "data_budget": data_budget.snapshot(),
            "staleness": data_cache.summary(),
            "tier_poll": _tier_poll_status(),
            "demo": config.demo_enabled(),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/data-budget")
def api_data_budget():
    """Today's provider-call budget per tier, per-provider usage vs configured
    daily limits, and the current shed level (Tier 3 → Tier 2 → Tier 1-cadence,
    never Tier 0). Telemetry only — persisted outside state.json."""
    try:
        import data_budget
        return jsonify(data_budget.snapshot())
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


@app.route("/api/refresh/ticker", methods=["POST"])
def api_refresh_ticker():
    """Force-refresh ONE ticker's daily bars now and return its fresh scorecard
    row — the on-demand 'this quote is stale, pull it live' path for a single
    name in the Scan. Names outside the hot set otherwise ride the daily cadence
    and read stale intraday; this pulls the current session's price on demand."""
    payload = request.get_json(silent=True) or {}
    ticker = (payload.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    try:
        import refresh_policy
        return jsonify(refresh_policy.refresh_tickers([ticker]))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/refresh/sector", methods=["POST"])
def api_refresh_sector():
    """Force-refresh a whole sector — the ETF plus its constituents — now and
    return their fresh scorecard rows. 'Refresh this sector' from the Scan, for
    when you want the whole group live at once rather than name by name."""
    payload = request.get_json(silent=True) or {}
    sector = (payload.get("sector") or "").strip().upper()
    if not sector:
        return jsonify({"error": "sector is required"}), 400
    if sector not in sector_data.sector_etfs():
        return jsonify({"error": f"unknown sector '{sector}'"}), 400
    try:
        import refresh_policy
        names = [sector] + sector_data.constituents(sector)
        return jsonify(refresh_policy.refresh_tickers(names))
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
    # Order-lifecycle startup reconciliation: any locally non-terminal order is
    # re-polled against the broker before new order activity is allowed for its
    # position (a crash mid-cancel must not orphan a working broker order). No-op
    # when no live broker is configured (paper/tests); never blocks serving.
    try:
        executor.reconcile_pending_orders_on_startup()
    except Exception as e:  # noqa: BLE001 — reconciliation must never block startup
        log.logger.error("startup order reconciliation failed: %s", e)

# Start the in-process alert scheduler (gunicorn imports this module; the CLI
# path below reaches it too). start_once() is idempotent and a no-op when
# CFM_ALERTS_SCHEDULER=0 (tests / one-off scripts).
alert_scheduler.start_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5179)), debug=True)
