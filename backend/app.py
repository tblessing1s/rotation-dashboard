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

from flask import Flask, g, jsonify, redirect, request, send_from_directory
from flask_cors import CORS

import accounts
import alert_scheduler
import alerts
import auth
import config
import data_handler
import earnings
import executor
import fetch_budget
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


@app.before_request
def _bound_fetch_budget():
    """Every HTTP request has a human waiting, so bound what its provider fetches
    may spend. See fetch_budget.py — the background budget is ~87s for a single
    symbol, which cannot fit inside the 60s the frontend waits before aborting.

    Set here and reset in teardown because gunicorn REUSES threads: a context
    variable left set would hand the next request this one's already-expired
    deadline, and every fetch would short-circuit to cache for the life of the
    worker. `executor.execute` opts back into the patient budget for order flow.
    """
    g._fetch_budget_token = fetch_budget.set_current(fetch_budget.interactive_budget())


ACCOUNT_HEADER = "X-CFM-Account"


@app.before_request
def _bind_account():
    """Bind this request to ONE book.

    The dashboard can hold several accounts (accounts.py). A request names the one
    it means with the ``X-CFM-Account`` header (or ``?account=``) — so two browser
    tabs can watch two accounts at once — and anything that doesn't name one gets
    the persisted active account, which is also what the background scheduler and
    CLI tools read. The binding is a contextvar reset in teardown: gunicorn reuses
    threads, and a leaked selection would hand the next request another book.

    An unknown id is refused rather than silently served from the primary book —
    except on the /api/accounts endpoints themselves, which are how a UI holding a
    stale id recovers.
    """
    requested = request.headers.get(ACCOUNT_HEADER) or request.args.get("account")
    if not requested:
        return None
    try:
        g._account_token = accounts.set_override(requested)
    except accounts.UnknownAccount:
        if request.path.startswith("/api/accounts"):
            return None
        return jsonify({"error": f"unknown account '{requested}'",
                        "unknown_account": True}), 404
    except accounts.RegistryCorrupt as e:
        return jsonify({"error": str(e)}), 500
    return None


@app.teardown_request
def _release_account(exc=None):
    token = g.pop("_account_token", None)
    if token is not None:
        accounts.reset_override(token)


@app.teardown_request
def _release_fetch_budget(exc=None):
    token = g.pop("_fetch_budget_token", None)
    if token is not None:
        fetch_budget.reset(token)


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


def _scan_pending(**extra):
    """The response for a full-universe read whose sweep is not warm yet.

    Explicitly `scan_pending`, never an empty result set: "the scan has not
    finished" and "the scan found nothing" are different facts, and a client that
    rendered the first as the second would show an empty Ready-to-Enter as though
    the gate had rejected everything. `results`/`ready` are empty ONLY as a shape
    contract for older clients; `scan_pending` is what a caller must branch on.
    HTTP 200 — a pending sweep is a state, not an error."""
    import screening
    st = screening.scan_status()
    return {"scan_pending": True, "running": bool(st.get("running")),
            "scanned_at": st.get("scanned_at"), "scan_day": st.get("scan_day"),
            "as_of": None, "results": [], **extra}


@app.route("/api/scan/refresh", methods=["POST"])
def api_scan_refresh():
    """Start a full-universe scan in a detached server-side job (deduped — one at
    a time) and return its status immediately. Because the sweep runs off-request,
    it keeps going even if the client tab is backgrounded, switched, or closed;
    the client polls /api/scan/status and reads results warm when it returns.

    FORCED: this is the operator's Rescan button, so it bypasses the day cache. The
    scheduled warm-ups do not force — they only fill an epoch that has no sweep yet
    (see scan_cache), which is what keeps the universe sweep to ~twice a day."""
    try:
        return jsonify(screening.start_background_scan(force=True))
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
    ?tickers=AAPL,MSFT narrows it to a subset.

    AFFORDABILITY (schema v21): a shares-primary entry buys a whole 100-share lot,
    so names whose lot costs more than the account's current dry powder are not
    real candidates and are filtered out by default. Pass ?include_unaffordable=1
    to see them anyway. The filter is applied HERE, not inside the sweep, so the
    memoized market scan stays account-free and shared across requests — only the
    per-request account overlay differs.
    """
    raw = request.args.get("tickers")
    tickers = [t for t in raw.split(",") if t.strip()] if raw else None
    include_unaffordable = request.args.get("include_unaffordable", "").strip() in ("1", "true", "yes")
    try:
        from metrics import scorecard as scorecard_metrics
        # An explicit subset is cheap and computes fresh. The FULL universe is a
        # multi-minute sweep that holds the `scorecard:full` lock, so this read
        # path only ever PEEKS at it (scorecard_warm) — calling scorecard() here
        # made this request wait out any in-flight background sweep and the
        # client aborted at its 60s timeout.
        if tickers:
            out = dict(scorecard_metrics.scorecard(tickers))
        else:
            warm = scorecard_metrics.scorecard_warm()
            if warm is None:
                return jsonify(_scan_pending())
            out = dict(warm)
        # Annotate every row, then filter — so the priced-out rows carry their
        # reason whether or not they are being shown.
        # Row COPIES: the annotation below is this book's (see /api/scan/ready),
        # and the sweep underneath is shared across accounts.
        keep, priced_out, bar = scorecard_metrics.split_by_affordability(
            [dict(r) for r in out.get("results") or []], log.load_state())
        out["affordability"] = bar
        shown = (keep + priced_out) if include_unaffordable else keep
        # `gate_results` (the per-gate calibration telemetry) rides on the sweep
        # row for the nightly recorder, which reads the sweep directly. Nothing on
        # this response consumes it and it is ~2 KB per row — ~1 MB across a full
        # universe — so it is dropped at the API boundary rather than shipped to
        # every Scan tab mount. The calibration view reads the aggregated rollup
        # from /api/scan/gate-telemetry instead.
        out["results"] = [{k: v for k, v in r.items() if k != "gate_results"}
                          for r in shown]
        out["priced_out_tickers"] = [r["ticker"] for r in priced_out]
        return jsonify(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/ready")
def api_scan_ready():
    """The RANKED shortlist: every ELIGIBLE name, best first.

    The scan is a thin hard floor plus a ranker. A name reaches this list by
    clearing the whole VETO SET (``scan_verdict.VETOES`` — the exit mirrors plus
    hard account constraints); its POSITION on the list is its rank, and the rank
    blocks nothing. A name that scores badly is still here, near the bottom, which
    is the point: the old serial filter's answer to a weak field was an empty list,
    and an empty list is not the same claim as "here is the best available, and it
    is not very good".

    THE PRESSURE GUARD (§1.5). A ranker always produces a #1, and an operator who
    wants to be deployed plus a #1 is a mechanism for entering the least-bad name on
    a bad day. So:

      * ``eligible_of_evaluated`` is returned on every response and is meant to be
        read as prominently as the list itself. **Zero eligible is a normal,
        expected outcome, not an error state.**
      * nothing here auto-selects, pre-fills, or flags the top-ranked name as an
        action. The response carries no "recommended" field and the rank is not a
        recommendation.
      * every entry carries its absolute ``score`` alongside its ``rank``. "Best
        available" and "good" are different claims and the UI must never be able to
        show the first while implying the second.
      * the structural vetoes carry NO override path. ``blocked`` names are
        reported with the veto that stopped them and there is no parameter that
        admits them anyway; the L5 account overrides that exist today are unchanged
        and remain the executor's business.

    Level 5 and input staleness are layered HERE, where the account context and the
    freshness read live — the memoized market sweep has neither. The executor
    re-enforces L5 at the ticket regardless: this list is advisory.
    """
    raw = request.args.get("tickers")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()] if raw else None
    contracts = int(request.args.get("contracts") or 0) or None
    try:
        from metrics import scorecard as scorecard_metrics
        import account_gate
        import data_cache
        import market_scheduler
        import scan_score
        import scan_verdict
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        # PEEK, never compute — a full-universe read that triggered the sweep
        # blocked for its whole duration.
        if tickers:
            sc = scorecard_metrics.scorecard(tickers)
        else:
            sc = scorecard_metrics.scorecard_warm()
            if sc is None:
                return jsonify(_scan_pending(eligible=[], blocked=[],
                                             priced_out=[],
                                             eligible_of_evaluated={"eligible": 0,
                                                                    "evaluated": 0}))
        # Copy the memoized rows before annotating: split_by_affordability writes
        # this book's `affordable` / `max_lot_cost` onto each row, and the sweep
        # underneath is SHARED across accounts. Annotating it in place would let
        # one book's dry powder decide what another book sees as priced out.
        rows = [dict(r) for r in sc["results"]]
        evaluated_n = len(rows)

        # Affordability is not a veto — a lot the account cannot buy today is
        # simply not actionable today, which is a different fact from failing the
        # gate. Reported separately so nothing vanishes silently.
        candidate_rows, priced_out, afford = scorecard_metrics.split_by_affordability(
            [r for r in rows if scan_verdict.is_eligible(r.get("verdict"))],
            log.load_state())

        level5 = account_gate.evaluate_many([r["ticker"] for r in candidate_rows],
                                            contracts=contracts)

        # STALE_BLOCKS_GO [HARD_CFM_RULE] — enforced only once the tiered poller is
        # actually populating quotes, and only in a live, open-market context: a
        # bulk warm scan legitimately has no live quotes.
        now_et = _dt.now(_ZI("America/New_York"))
        mkt_open = market_scheduler.is_market_open(now_et)
        live = mkt_open and not config.demo_enabled() and data_cache.active()
        if live and candidate_rows:
            import data_transport
            from market_scheduler import QUOTE as _QUOTE
            need = [r["ticker"] for r in candidate_rows
                    if data_cache.get_with_staleness(
                        r["ticker"], _QUOTE, tier=market_scheduler.Tier.T1)[2]]
            if need:
                try:
                    data_transport.fetch_quotes_batched(
                        {s: market_scheduler.Tier.T1 for s in need})
                except Exception as fe:  # noqa: BLE001 — the scan still returns
                    logging.getLogger("cfm.app").warning(
                        "scan_ready on-demand quote fetch failed: %s", fe)

        eligible, blocked = [], []
        for r in candidate_rows:
            l5 = level5.get(r["ticker"])
            stale, stale_inputs = data_cache.stale_blocks_go(
                r["ticker"], market_scheduler.Tier.T1, market_open=mkt_open, live=live)
            # The account + staleness vetoes, evaluated through the SAME registry
            # the sweep used. A second, differently-shaped evaluation here is
            # exactly how a scan and a ticket start disagreeing.
            late = scan_verdict.evaluate(stale=stale, account_gate=l5)
            entry = {
                "ticker": r["ticker"], "sector": r.get("sector"),
                # Rank ALWAYS travels with its absolute score (§1.5).
                "score": r.get("score"),
                "score_quality": r.get("score_quality"),
                "score_contributions": r.get("score_contributions"),
                "route": r.get("route"),
                "juice_weekly_pct": r.get("juice_weekly_pct"),
                "net_juice_weekly_pct": r.get("net_juice_weekly_pct"),
                "earnings_date": r.get("earnings_date"),
                "earnings_trigger": r.get("earnings_trigger"),
                "level5": l5,
                "sym": r.get("sym"), "base_stage": r.get("base_stage"),
                "inst_flow": r.get("inst_flow"),
                "lights": r.get("lights"), "stock_greens": r.get("stock_greens"),
                "right_spot": r.get("right_spot"),
                "lot_cost": r.get("lot_cost"), "affordable": r.get("affordable"),
                "max_lot_cost": r.get("max_lot_cost"),
                "stale_inputs": stale_inputs,
            }
            if late:
                composed = scan_verdict.compose(late)
                entry["verdict"] = composed["verdict"]
                entry["blocked_by"] = composed["blocked_by"]
                blocked.append(entry)
            else:
                entry["verdict"] = scan_verdict.ELIGIBLE
                entry["blocked_by"] = []
                eligible.append(entry)

        # The rank. Deterministic: ties break by symbol, so two runs over identical
        # inputs produce identical ordering.
        eligible = scan_score.rank(eligible)

        return jsonify({
            "as_of": sc["as_of"],
            "eligible": eligible,
            # Names the veto set stopped, with WHICH veto. No override path.
            "blocked": blocked,
            # THE PRESSURE GUARD HEADLINE. Read this before the list.
            "eligible_of_evaluated": {"eligible": len(eligible),
                                      "evaluated": evaluated_n},
            "affordability": afford,
            "priced_out": [{"ticker": r["ticker"], "lot_cost": r.get("lot_cost"),
                            "over_by": r.get("lot_cost_over_by")}
                           for r in priced_out],
        })
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
    # ?refresh=1 forces a live re-pull (the modal's bid/ask poll) past the 5-min cache.
    refresh = request.args.get("refresh", "").strip() in ("1", "true", "yes")
    try:
        return jsonify(option_chain.option_chain(ticker, strategy, refresh=refresh))
    except option_chain.RegimeBlocked as e:
        return jsonify({"error": str(e), "regime": "red"}), 403
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/put-chain/<ticker>")
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


@app.route("/api/put-placement-status")
def api_put_placement_status():
    """Which of the three placement switches are on, and which are not. Lets the
    ticket say WHY it can only record rather than greying a button out."""
    return jsonify(option_chain.placement_status())


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
    prior_target = request.args.get("prior_target", type=float)
    try:
        return jsonify(option_chain.roll_options(ticker, prior_target=prior_target))
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


@app.route("/api/positions/close-empty", methods=["POST"])
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


@app.route("/api/order-status")
def api_order_status():
    order_id = request.args.get("order_id", "")
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    try:
        return jsonify(executor.order_status(order_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/schwab/rate-limit")
def api_schwab_rate_limit():
    """The process-wide Schwab pacing in effect (requests/minute, tokens left,
    any 429 pause) — what to look at when reads feel slow."""
    return jsonify(schwab_api.rate_limit_status())


@app.route("/api/ticker-strip")
def api_ticker_strip():
    """The chrome's per-position readout (spot + distance to each short strike)
    for the active book. Thin and polled from every tab — see
    position_manager.ticker_strip."""
    try:
        state = log.load_state()
        return jsonify({"as_of": log.utcnow(),
                        "positions": position_manager.ticker_strip(state)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/orders/pending")
def api_orders_pending():
    """The active book's pending (placed, not yet settled) orders, with the
    stock price captured at order time — what a re-poll would book a fill at."""
    try:
        return jsonify({"orders": executor.list_pending_orders()})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/orders/repoll", methods=["POST"])
def api_orders_repoll():
    """Re-poll every pending order against Schwab now (the startup sweep, on
    demand): a fill that happened at the broker but never got booked is committed
    with its original captured economics; terminal orders are cleared."""
    try:
        return jsonify(executor.repoll_pending_orders())
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


@app.route("/api/scan/gate-telemetry")
def api_scan_gate_telemetry():
    """Gate rejection telemetry — the calibration rollup over the per-candidate,
    per-gate evaluation record (gate_telemetry).

    The headline metric is SOLE-BLOCKER RATE: for each gate, the fraction of
    evaluated candidates where that gate failed and every OTHER veto-authority
    gate passed. A high block rate with a low sole-blocker rate means the gate
    co-fires with genuinely bad setups; a high sole-blocker rate means the gate is
    the binding constraint on the whole system.

    READ-ONLY. This endpoint grants no authority, changes no threshold and
    touches no gate. Optional ?start=/?end= (ISO dates, default the last
    GATE_TELEMETRY_LOOKBACK_DAYS days), ?ruleset= (never pools across rulesets —
    an unfiltered range containing more than one returns the counts and no table)
    and ?symbols= (comma-separated universe filter). Empty until the nightly
    sweep has recorded a scan; absence of history is reported as absence, never
    backfilled."""
    try:
        import gate_telemetry
        start = (request.args.get("start") or "").strip() or None
        end = (request.args.get("end") or "").strip() or None
        ruleset = (request.args.get("ruleset") or "").strip() or None
        raw = request.args.get("symbols")
        symbols = [t.strip() for t in raw.split(",") if t.strip()] if raw else None
        return jsonify(gate_telemetry.aggregate(start=start, end=end,
                                                gate_ruleset=ruleset,
                                                symbols=symbols))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/transitions")
def api_scan_transitions():
    """The nightly scan transition feed — BENCH→READY / fresh-READY / degrade /
    pipeline-entrant / sector-slot-open events, newest first. The pipeline's audit
    trail and the retrospective (Q9) capture. Optional ?limit=N (default 100).
    Read-only derived telemetry; empty until the nightly diff has run."""
    try:
        import scan_diff_log
        limit = int(request.args.get("limit") or 100)
        return jsonify({"events": scan_diff_log.recent(limit=limit)})
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/structure-label", methods=["GET", "POST"])
def api_scan_structure_label():
    """Operator STRUCTURE_LABEL annotations — the manual half of the Level-4
    chart-structure calibration set (see structure_labels).

    POST {ticker, label, scan_id?, verdict?, structure_score?,
          structure_score_of?, note?} appends one label. ``label`` is
    COMPELLING / NOT_COMPELLING / UNSURE (yes/no/unsure shorthands accepted).
    Append-only: relabelling appends a second row rather than rewriting the
    first, and NOTHING here edits or re-derives the historical verdict it
    annotates.

    GET returns the calibration rollup (labels crossed against structure_score),
    or one ticker's labels with ?ticker=X, or the newest across all tickers with
    ?recent=N. Curl-able by design — no UI is required for the shadow period.

    Sits behind the normal session auth like every other /api/scan/* route."""
    try:
        import structure_labels
        if request.method == "GET":
            ticker = (request.args.get("ticker") or "").strip()
            if ticker:
                return jsonify({"ticker": ticker.upper(),
                                "labels": structure_labels.series(ticker)})
            limit = request.args.get("recent")
            if limit is not None:
                return jsonify({"labels": structure_labels.recent(limit=int(limit or 100))})
            return jsonify(structure_labels.summary())
        body = request.get_json(silent=True) or {}
        result = structure_labels.record_label(
            body.get("ticker") or "",
            body.get("label") or "",
            scan_id=body.get("scan_id"),
            verdict=body.get("verdict"),
            structure_score=body.get("structure_score"),
            structure_score_of=body.get("structure_score_of"),
            note=body.get("note"))
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/scan/candidate-universe")
def api_scan_candidate_universe():
    """The weekly universe-intake screen result — the momentum/quality-filtered
    candidate list, the sector-diversity fold (the empirical one-position-per-sector
    check), and the append-only add/drop change log. SHADOW: the current sector
    universe stays operative unless CFM_UNIVERSE_SCREEN is enabled. Read-only;
    empty until the first weekly screen has run."""
    try:
        import candidate_universe
        return jsonify(candidate_universe.report())
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


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    """Send one SAMPLE position alert through the real delivery path (channels,
    settings and dry-run as persisted) and report what happened — the operator's
    "would I actually get paged?" check. Persists nothing."""
    try:
        return jsonify(alerts.test_delivery())
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


@app.route("/api/recommendations/acknowledge-miss", methods=["POST"])
def api_recommendations_acknowledge_miss():
    """Operator acknowledgement of a COVERAGE_MISS with a CODED reason (+ optional
    note; OTHER requires one). Appends an immutable record keyed on the miss's
    execution ids — the miss itself is derived and is never removed; it is
    classified. Body: {execution_ids: [...], reason, note?}."""
    import rec_types
    import trust_derive
    payload = request.get_json(silent=True) or {}
    ids = payload.get("execution_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    ids = [str(i) for i in ids if i]
    reason = str(payload.get("reason") or "").strip().upper()
    note = (payload.get("note") or "").strip() or None
    if not ids:
        return jsonify({"error": "execution_ids is required"}), 400
    if not rec_types.is_miss_ack_reason(reason):
        return jsonify({"error": f"reason must be one of {sorted(rec_types.MISS_ACK_REASONS)}"}), 400
    if rec_types.miss_ack_requires_note(reason) and not note:
        return jsonify({"error": f"a typed note is required for {reason}"}), 400
    try:
        state = log.load_state()
        key = trust_derive.miss_key(ids)
        miss = next((r for r in state.get("recommendation_resolutions", []) or []
                     if r.get("status") == "COVERAGE_MISS" and r.get("miss_key") == key), None)
        if miss is None:
            return jsonify({"error": f"no coverage miss on executions {', '.join(sorted(ids))}"}), 404
        if miss.get("acknowledged"):
            return jsonify({"error": "that coverage miss is already acknowledged",
                            "acknowledged": miss["acknowledged"]}), 409
        stored = log.append_coverage_miss_ack({
            "execution_ids": sorted(ids), "ticker": miss.get("ticker"),
            "action_type": miss.get("action_type"), "reason": reason, "note": note})
        return jsonify({"acknowledgement": stored})
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
    stock_price = payload.get("stock_price")
    if stock_price in (None, ""):
        stock_price = None
    else:
        try:
            stock_price = float(stock_price)
        except (TypeError, ValueError):
            return jsonify({"error": "stock_price must be a number"}), 400
        if stock_price <= 0:
            return jsonify({"error": "stock_price must be positive"}), 400
    try:
        return jsonify(executor.adopt_broker_trade(proposal_id, stock_price))
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


# ---------------------------------------------------------------------------
# Accounts (one book per brokerage account; see accounts.py)
# ---------------------------------------------------------------------------
@app.route("/api/accounts")
def api_accounts():
    """The registry: every account, its Schwab connection, which is active, and
    this request's binding.

    The connection block is what makes "why can't this book see its account"
    answerable in the UI: it says whether the book authenticates with the shared
    grant or its own, and whether that grant is actually good right now."""
    try:
        rows = []
        for acct in accounts.list_accounts(include_archived=True):
            connection = accounts.connection_id(acct["id"])
            status = schwab_api.token_status(connection)
            rows.append({**acct, "connection": {
                "id": connection,
                "mode": "own" if acct.get("own_connection") else "shared",
                "connected": bool(status.get("present")),
                "status": status.get("status"),
                "days_left": status.get("daysLeft"),
            }})
        return jsonify({
            "active": accounts.active_id(),
            "persisted_active": accounts.load_registry()["active"],
            "demo": config.demo_enabled(),
            "accounts": rows,
            "max_accounts": accounts.MAX_ACCOUNTS,
        })
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/accounts", methods=["POST"])
def api_accounts_create():
    """Register another book. Its state file is created lazily on first use, so a
    new account starts as an empty book at the current schema version."""
    payload = request.get_json(silent=True) or {}
    try:
        acct = accounts.create(
            payload.get("label") or "",
            broker_account_number=payload.get("broker_account_number"),
            account_id=payload.get("id"),
            note=payload.get("note") or "")
        return jsonify(acct)
    except (ValueError, accounts.RegistryCorrupt) as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/accounts/<account_id>", methods=["PATCH"])
def api_accounts_update(account_id: str):
    """Rename, (re)bind to a brokerage account number, archive/unarchive."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(accounts.update(
            account_id,
            label=payload.get("label"),
            broker_account_number=payload.get("broker_account_number"),
            archived=payload.get("archived"),
            note=payload.get("note"),
            own_connection=payload.get("own_connection")))
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_accounts_delete(account_id: str):
    """Remove an account. Refused while its book still holds executions unless
    ``?purge=1``, and even then the book is only set aside on the volume — an
    execution log is a trading record, not a UI-deletable object."""
    purge = request.args.get("purge", "").strip().lower() in ("1", "true", "yes")
    try:
        return jsonify(accounts.delete(account_id, purge=purge))
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except accounts.AccountInUse as e:
        return _err(e, 409)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/accounts/<account_id>/connection", methods=["DELETE"])
def api_accounts_disconnect(account_id: str):
    """Discard this book's own Schwab grant and put it back on the shared one.

    The refresh token is deleted rather than set aside — it is a credential, not
    a record, and reconnecting re-mints it in one click."""
    try:
        return jsonify(accounts.disconnect(account_id))
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/accounts/active", methods=["POST"])
def api_accounts_set_active():
    """Persist the operator's account choice.

    Deliberately clears NOTHING. The demo switch drops the scan and price caches
    because it changes the DATA SOURCE — a sweep computed against synthetic prices
    must never be replayed for real ones. An account switch changes the BOOK, and
    the market caches are account-free by construction: the memoized sweep holds
    market facts only, affordability and the Level-5 overlay are applied per
    request from the active book's state.

    Clearing them here cost the day's full-universe sweep (`scan_cache.clear()`
    deletes it) and every cached daily frame on every switch, so the next Scan had
    to re-sweep ~500 tickers and re-read every parquet — which is what made the
    Scan tab time out after switching accounts.
    """
    payload = request.get_json(silent=True) or {}
    try:
        acct = accounts.set_active(payload.get("id") or payload.get("account") or "")
        return jsonify({"active": acct["id"], "account": acct})
    except accounts.UnknownAccount as e:
        return _err(e, 404)
    except ValueError as e:
        return _err(e, 400)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/accounts/summary")
def api_accounts_summary():
    """Every book on one screen — open positions, deployed capital, week/month
    theta, live alerts, working orders and un-adopted broker fills per account.

    Read straight off the state files (no provider calls), so the multi-account
    monitor stays a single cheap request however many books there are."""
    include_archived = request.args.get("include_archived", "").strip().lower() in ("1", "true", "yes")
    try:
        return jsonify(accounts.summary(include_archived=include_archived))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.route("/api/accounts/broker-accounts")
def api_broker_accounts():
    """The brokerage accounts this Schwab login can reach, for the binding picker.

    One login commonly reaches several accounts; binding a book to one is what
    keeps its orders, transactions, cash and reconciliation on that account. Only
    account numbers are returned — the trading hashes stay server-side.

    ALWAYS 200, carrying the reason when the list is short or empty. Schwab
    returns only the accounts the app authorization covers, so "my other account
    isn't in the picker" has several different causes (not connected, token
    expired, the account wasn't ticked at consent) and an opaque 400 hides which
    one it is. The UI shows the count and the reason verbatim, and lets the
    operator type a number the enumeration can't see."""
    connection = accounts.connection_id()
    own = connection != accounts.SHARED_CONNECTION
    out = {
        "accounts": [],
        "count": 0,
        "connection": connection,
        "connection_mode": "own" if own else "shared",
        "schwab_configured": schwab_api.configured(),
        "token": schwab_api.token_status(),
        "demo": config.demo_enabled(),
        "error": None,
    }
    if not out["schwab_configured"]:
        out["error"] = (
            "This book authenticates with its own Schwab login, which isn't "
            "connected yet — use Connect below." if own else
            "Schwab isn't connected on this deployment, so the account list can't "
            "be read. Connect it from the Schwab card above.")
        return jsonify(out)
    try:
        numbers = data_handler.broker_client().account_numbers() or []
    except Exception as e:  # noqa: BLE001 — report the reason, don't hide it
        out["error"] = str(e)
        return jsonify(out)
    bound = {a["broker_account_number"]: a["id"]
             for a in accounts.list_accounts(include_archived=True)
             if a["broker_account_number"]}
    for entry in numbers:
        number = str(entry.get("accountNumber") or "").strip()
        if not number:
            continue
        out["accounts"].append({
            "account_number": number,
            "masked": f"…{number[-4:]}" if len(number) > 4 else number,
            "bound_to": bound.get(number),
        })
    out["count"] = len(out["accounts"])
    if not out["count"]:
        out["error"] = ("Schwab returned no accounts for this login. That usually "
                        "means the app authorization covers no account yet — "
                        "reconnect Schwab and tick every account on the consent screen.")
    return jsonify(out)


@app.route("/api/accounts/connections")
def api_account_connections():
    """Every Schwab grant this deployment holds, and how each one is doing.

    One expired grant is a silent outage for the books behind it — the shared
    token's expiry is already surfaced, and a second login's must be too."""
    try:
        rows = []
        for connection in accounts.connections():
            owner = accounts.connection_owner(connection)
            acct = accounts.get(owner) if owner else None
            rows.append({
                "connection": connection,
                "mode": "own" if owner else "shared",
                "account": owner,
                "label": (acct or {}).get("label") if acct else "Shared login",
                "token": schwab_api.token_status(connection),
                "configured": schwab_api.configured(connection),
            })
        return jsonify({"connections": rows})
    except Exception as e:  # noqa: BLE001
        return _err(e)


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
        # Actions that never reach the broker whatever the switches say, so the
        # confirmation dialog cannot promise a transmit the executor will not
        # perform. Served rather than duplicated in the frontend: one source of
        # truth, and it can never drift from the dispatch that enforces it.
        "non_transmitting_actions": sorted(executor.non_transmitting_actions()),
        # How long the UI should let a working order try to fill before cancelling
        # it. Served so the window is ONE number the operator can tune, not a
        # constant compiled into the bundle.
        "order_fill_wait_seconds": config.ORDER_FILL_WAIT_SECONDS,
        "equity_placement": config.EQUITY_ORDER_PLACEMENT_ENABLED,
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
    """Add a constituent to a sector: {ticker, sector}.

    The new name's daily bars and weeklies status are warmed in a detached thread
    so the next scan doesn't pay for them on the request path — a brand-new
    ticker is cold in both caches, and the weeklies probe is a live option-chain
    call. The response doesn't wait for it."""
    payload = request.get_json(silent=True) or {}
    try:
        out = sector_data.add_ticker(payload.get("ticker", ""), payload.get("sector", ""))
        out.update(screening.start_background_warm([out["added"]]))
        return jsonify(out)
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
        out = sector_data.sync_from_seed()
        # Same reasoning as /add: names pulled in from the seed are cold in both
        # caches, so warm them off-request rather than on the next scan.
        out.update(screening.start_background_warm(out.get("added") or []))
        return jsonify(out)
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


@app.route("/api/admin/reset-book", methods=["POST"])
def api_admin_reset_book():
    """One-time hard reset of the book — start over with an empty state.

    Clears positions + the append-only execution log + every derived ledger,
    returning a fresh book at the current schema version. Deliberately hard to
    fire by accident: it needs (1) a valid login (the before_request auth gate),
    (2) config.RESET_BOOK_ENABLED — a server env flag OFF by default, and (3) a
    typed ``confirm: "RESET"`` in the body. Optional ``wipe_all`` also clears push
    subscriptions + account cash (kept by default). SAFE for the single-writer
    store: reset_book acquires the same in-process lock every save uses, so no
    scheduler tick can race it — no need to stop the app. Recoverable: a rotating
    backup is taken first (shipped off-machine when configured) and the prior file
    is written aside as state.json.pre-reset.<ts>."""
    if not config.RESET_BOOK_ENABLED:
        return jsonify({"error": "book reset is disabled; set RESET_BOOK_ENABLED=1 "
                        "to enable it (then unset it again afterwards)",
                        "reset_disabled": True}), 403
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm") or "") != "RESET":
        return jsonify({"error": 'confirmation required: POST {"confirm": "RESET"}',
                        "confirm_required": True}), 400
    wipe_all = bool(body.get("wipe_all"))
    try:
        import backups
        target = config.active_state_path()
        # Recoverable backup FIRST — rotating copy in the backups dir, then a copy
        # shipped off-machine if configured. A failed off-machine copy is reported,
        # not fatal (the local backup + the pre-reset aside copy still recover it).
        backup = backups.make_nightly_backup(target)
        off = backups.send_offmachine_copy(backup)
        report = log.reset_book(build_fresh=lambda prior: log.book_fresh_state(prior, wipe_all))
        return jsonify({"ok": True, "cleared": report["cleared"],
                        "schema_version": report["schema_version"],
                        "wipe_all": wipe_all, "backup": backup,
                        "off_machine": off, "pre_reset": report["pre_reset"]})
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


def _state_for(connection: str) -> str:
    """OAuth state carrying the CONNECTION the grant is for.

    Schwab echoes ``state`` back verbatim, and the callback URL itself is fixed
    (it must match the one registered with the app), so state is the only channel
    that can tell the callback which book's login just consented. The random half
    is kept in front of it.
    """
    return f"{secrets.token_urlsafe(16)}.{connection}"


def _connection_from_state(state: str | None) -> str:
    """The connection a callback's state names, validated against the registry.

    An unknown or missing connection falls back to the SHARED grant — the only
    safe default, since it is the one every deployment already has; storing a
    stranger's consent into a book's slot is what must not happen.
    """
    tail = (state or "").rsplit(".", 1)[-1].strip()
    if not tail or tail == accounts.SHARED_CONNECTION:
        return accounts.SHARED_CONNECTION
    owner = accounts.connection_owner(tail)
    if owner and accounts.exists(owner):
        return tail
    return accounts.SHARED_CONNECTION


@app.route("/auth/schwab")
def auth_schwab():
    """Start the Schwab consent flow for ONE connection.

    Which one comes from the account this request is bound to (the
    ``?account=``/header binding): a book on its own connection re-consents its
    own login, every other book re-consents the shared one."""
    try:
        connection = accounts.connection_id()
        return jsonify({
            "authorize_url": schwab_api.authorize_url(_callback_uri(),
                                                      _state_for(connection)),
            "connection": connection,
            "account": accounts.active_id(),
        })
    except Exception as e:  # noqa: BLE001
        return _err(e, 400)


@app.route("/auth/schwab/callback")
def auth_schwab_callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?schwab=error&msg=missing+authorization+code")
    connection = _connection_from_state(request.args.get("state"))
    try:
        tokens = schwab_api.exchange_code(code, _callback_uri())
        # Store against the connection that STARTED the flow, never the currently
        # active account: the operator may well have switched books in another tab
        # while Schwab's consent screen was open.
        schwab_api.store_refresh_token(tokens["refresh_token"], connection)
        owner = accounts.connection_owner(connection)
        return redirect(f"/?schwab=connected&account={owner}" if owner else "/?schwab=connected")
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
    # Every account: a working order left behind by a crash is just as dangerous
    # in the second book as in the first, and each book's pending orders live in
    # its own store.
    for _account_id in accounts.scheduled_ids():
        try:
            with accounts.use(_account_id):
                executor.reconcile_pending_orders_on_startup()
        except Exception as e:  # noqa: BLE001 — reconciliation must never block startup
            log.logger.error("startup order reconciliation failed for account %s: %s",
                             _account_id, e)

# Start the in-process alert scheduler (gunicorn imports this module; the CLI
# path below reaches it too). start_once() is idempotent and a no-op when
# CFM_ALERTS_SCHEDULER=0 (tests / one-off scripts).
alert_scheduler.start_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5179)), debug=True)
