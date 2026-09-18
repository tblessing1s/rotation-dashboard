"""Scan (regime scorecard, gate telemetry, structure) (11 routes) — split out of the former monolithic app.py."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api_common import _err

import config
import data_handler
import logging_handler as log
import logging
import screening

scan_bp = Blueprint("scan", __name__)


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


@scan_bp.route("/api/scan/refresh", methods=["POST"])
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


@scan_bp.route("/api/scan/status")
def api_scan_status():
    """Poll the background scan: running / done / error, timestamps, and whether
    the memoized results are warm (ready to render)."""
    try:
        return jsonify(screening.scan_status())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@scan_bp.route("/api/scan/scorecard")
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


@scan_bp.route("/api/scan/ready")
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


@scan_bp.route("/api/scan/refresh-quote", methods=["POST"])
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


@scan_bp.route("/api/scan/rejection-stats")
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


@scan_bp.route("/api/scan/gate-telemetry")
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


@scan_bp.route("/api/scan/transitions")
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


@scan_bp.route("/api/scan/structure-label", methods=["GET", "POST"])
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


@scan_bp.route("/api/scan/candidate-universe")
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

