"""
Rotation Dashboard — backend API.

The request path reads exclusively from the SQLite datastore (see db.py).
External providers are only contacted by scheduled ingestion (ingest.py),
triggered by the Fly cron machine via POST /api/ingest, the CLI, or a
background catch-up thread when the app wakes up with stale data.

Run:  python app.py     (then open http://localhost:5179)
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
import traceback

from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_cors import CORS

import config as cfg
import db
import indicators as ind
import ingest
import market_calendar as mcal

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(HERE, "..", "frontend", "dist")

STATE_FILE = ingest.STATE_FILE
_LEGACY_STATE_FILE = os.path.join(HERE, "state.json")

app = Flask(__name__, static_folder=None)
CORS(app)


def _migrate_legacy_state() -> None:
    """One-time move of state.json into DATA_DIR (the Fly volume)."""
    if os.path.exists(STATE_FILE) or not os.path.exists(_LEGACY_STATE_FILE):
        return
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    shutil.copy2(_LEGACY_STATE_FILE, STATE_FILE)
    print(f"[state] migrated {_LEGACY_STATE_FILE} -> {STATE_FILE}")


_migrate_legacy_state()


# ---------------------------------------------------------------------------
# Background catch-up: if the machine wakes up with stale data, kick one
# ingestion run in a daemon thread. Requests are never blocked on providers.
# ---------------------------------------------------------------------------
_last_kick = 0.0


@app.before_request
def _catchup_if_stale():
    global _last_kick
    if not request.path.startswith("/api/"):
        return
    now = time.time()
    if now - _last_kick < 600:  # at most one kick per 10 minutes
        return
    if ingest.is_stale():
        _last_kick = now
        print("[ingest] data stale — starting background catch-up run")
        ingest.run_in_background("catchup")


# ---------------------------------------------------------------------------
# Staleness helpers
# ---------------------------------------------------------------------------
def _bar_staleness(as_of: str | None) -> str:
    return mcal.staleness(as_of)


def _ingest_staleness(fetched_at: str | None) -> str:
    """Freshness of slow-moving (monthly/quarterly) series: what matters is
    that ingestion keeps running, not that the observation is recent."""
    if not fetched_at:
        return "unknown"
    try:
        from datetime import datetime, timezone

        fetched = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
    except ValueError:
        return "unknown"
    if age_hours <= 36:
        return "fresh"
    if age_hours <= 96:
        return "yellow"
    return "red"


# ---------------------------------------------------------------------------
# API — all reads come from the datastore
# ---------------------------------------------------------------------------
@app.route("/api/quotes")
def api_quotes():
    out = {}
    for symbol in cfg.QUOTE_SYMBOLS:
        bar = db.latest_bar(symbol)
        if bar is None:
            out[symbol] = {"symbol": symbol, "error": True}
            continue
        quote = {
            "symbol": symbol,
            "close": round(bar["close"], 2),
            "open": round(bar["open"], 2) if bar["open"] is not None else None,
            "date": bar["date"],
            "source": bar["source"],
            "fetchedAt": bar["fetched_at"],
            "staleness": _bar_staleness(bar["date"]),
        }
        out[symbol] = quote
        if symbol == cfg.VIX_PROXY_SYMBOL:
            out["VIX"] = {**quote, "symbol": "VIX", "underlyingSymbol": symbol}
    return jsonify(out)


@app.route("/api/indicators")
def api_indicators():
    requested = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in requested.split(",") if s.strip()] or cfg.TRACKED
    symbols = list(dict.fromkeys(symbols))

    snapshots = db.latest_snapshots("indicators")
    out = {}
    missing = []
    for sym in symbols:
        snap = snapshots.get(sym)
        if snap is None:
            out[sym] = {"error": "no data"}
            missing.append(sym)
            continue
        snap["staleness"] = _bar_staleness(snap.get("asOf"))
        out[sym] = snap

    # Newly watched symbols get picked up by a targeted background fetch; the
    # response stays datastore-only.
    if missing:
        known = set(db.known_symbols())
        new_symbols = [s for s in missing if s not in known]
        if new_symbols:
            ingest.run_in_background("new-symbols", new_symbols)
    return jsonify(out)


def _entry_candidate_universe() -> list[dict]:
    cfm = set(getattr(cfg, "CFM_ENTRY_CANDIDATES", []))
    app_candidates = set(getattr(cfg, "APP_ENTRY_CANDIDATES", []))
    proxies = getattr(cfg, "ENTRY_CANDIDATE_PROXY", {})
    universe = []
    for symbol in getattr(cfg, "ENTRY_CANDIDATES", []):
        strategies = []
        if symbol in cfm:
            strategies.append("CFM")
        if symbol in app_candidates:
            strategies.append("APP")
        universe.append({
            "symbol": symbol,
            "candidateStrategies": strategies,
            "sectorProxy": proxies.get(symbol, symbol if symbol in getattr(cfg, "SECTOR_SYMBOLS", []) else None),
        })
    return universe


@app.route("/api/indicator-snapshots")
def api_indicator_snapshots():
    """Recent indicator snapshots for Entry Watch candidate rank comparisons.

    Datastore-only: returns previously computed snapshot payloads from SQLite.
    The symbol set is clamped to the Entry Watch candidate universe so the UI
    can rebuild prior CFM/APP rankings without broad arbitrary history reads.
    """
    universe = _entry_candidate_universe()
    allowed = {item["symbol"] for item in universe}
    requested = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in requested.split(",") if s.strip()]
    if symbols:
        symbols = [s for s in dict.fromkeys(symbols) if s in allowed]
    else:
        symbols = [item["symbol"] for item in universe]

    as_of = request.args.get("as_of") or None
    try:
        limit = max(1, min(10, int(request.args.get("limit", "3"))))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    sessions = []
    for session in db.snapshots_by_as_of("indicators", symbols, as_of=as_of, limit_sessions=limit):
        snapshots = session.get("snapshots", {})
        missing = [s for s in symbols if s not in snapshots]
        enriched = {}
        for item in universe:
            symbol = item["symbol"]
            if symbol not in snapshots:
                continue
            payload = snapshots[symbol]
            enriched[symbol] = {
                "symbol": symbol,
                "candidateStrategies": item["candidateStrategies"],
                "sectorProxy": item["sectorProxy"],
                "asOf": session["asOf"],
                "computedAt": payload.get("_computedAt") or session.get("computedAt"),
                "indicators": payload,
                "staleness": _bar_staleness(session["asOf"]),
            }
        sessions.append({
            "asOf": session["asOf"],
            "computedAt": session.get("computedAt"),
            "complete": not missing,
            "missing": missing,
            "symbols": enriched,
        })

    return jsonify({
        "universe": universe,
        "asOf": as_of,
        "sessions": sessions,
        "historyState": "ok" if sessions else "insufficient_history",
    })


@app.route("/api/levels")
def api_levels():
    """On-demand support/resistance for a single Entry Watch symbol.

    Reads stored daily bars only (no provider call). Unknown symbols trigger a
    targeted background fetch so a retry after the next ingest has data.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    bars = db.get_bars(symbol)
    if bars is None or bars.empty:
        if symbol not in set(db.known_symbols()):
            ingest.run_in_background("new-symbols", [symbol])
        return jsonify({"symbol": symbol, "error": "no data"})

    result = ind.support_resistance(bars)
    result["symbol"] = symbol
    result["asOf"] = str(bars.index[-1].date())
    result["staleness"] = _bar_staleness(result["asOf"])
    return jsonify(result)


@app.route("/api/macro")
def api_macro():
    snap = db.latest_snapshot("macro", "macro") or {"values": {}, "fields": {}, "errors": {"macro": "no ingested data yet"}}
    fields = dict(snap.get("fields") or {})
    errors = dict(snap.get("errors") or {})

    # Per-field staleness: market inputs by trading-day age, FRED-derived
    # inputs by ingestion age (CPI being a month old is normal).
    for key, meta in fields.items():
        if key in ("vix", "breadth"):
            meta["staleness"] = _bar_staleness(meta.get("asOf"))
        else:
            meta["staleness"] = _ingest_staleness(meta.get("fetchedAt"))

    # Manual overrides always win.
    for key, ov in db.get_overrides("macro").items():
        fields[key] = {
            "value": ov["value"],
            "source": "manual",
            "asOf": ov["updatedAt"],
            "override": True,
            "staleness": "fresh",
        }
        errors.pop(key, None)

    values = {key: meta["value"] for key, meta in fields.items()}

    # The regime gate is only as fresh as its oldest input.
    order = {"fresh": 0, "yellow": 1, "red": 2, "unknown": 2}
    worst = max((meta.get("staleness", "unknown") for meta in fields.values()),
                key=lambda s: order.get(s, 2), default="unknown")
    expected = ["vix", "breadth", "fed", "growth", "inflation"]
    if any(key not in fields for key in expected):
        worst = "red"

    return jsonify({
        "values": values,
        "fields": fields,
        "errors": errors,
        "asOf": snap.get("_computedAt"),
        "staleness": worst,
        "degraded": worst == "red",
    })


@app.route("/api/overrides", methods=["GET", "POST"])
def api_overrides():
    if request.method == "GET":
        scope = request.args.get("scope", "macro")
        return jsonify(db.get_overrides(scope))
    body = request.get_json(force=True) or {}
    scope = str(body.get("scope") or "macro")
    key = str(body.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    if body.get("value") is None:
        db.clear_override(scope, key)
        return jsonify({"ok": True, "cleared": key})
    db.set_override(scope, key, body["value"], source="manual")
    return jsonify({"ok": True, "key": key})


@app.route("/api/data-issues")
def api_data_issues():
    from providers import schwab

    return jsonify({
        "quarantine": db.recent_quarantine(),
        "lastRun": db.last_ingest_run(),
        "lastSuccessfulRun": db.last_successful_ingest(),
        "schwabAuthError": db.kv_get("schwab_auth_error"),
        "schwabToken": schwab.token_status(),
    })


# ---------------------------------------------------------------------------
# Schwab re-authorization (hosted OAuth) — browser flow so the weekly refresh
# is a one-click consent instead of a CLI + secret-edit chore. Schwab requires
# a human login every 7 days; this just removes everything around that click.
# The minted refresh token is stored in the datastore (current_refresh_token()
# reads it before the env secret), so no redeploy/secret edit is needed.
# ---------------------------------------------------------------------------
def _schwab_redirect_uri() -> str:
    """The callback that must be registered on the Schwab app. Defaults to this
    app's public URL; override with SCHWAB_REDIRECT_URI if the host differs."""
    override = os.environ.get("SCHWAB_REDIRECT_URI")
    if override:
        return override
    return f"https://{request.host}/auth/schwab/callback"


def _auth_result_page(title: str, message: str, ok: bool = True):
    color = "#1f9d55" if ok else "#e3342f"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>"
        "body{font-family:system-ui,-apple-system,sans-serif;background:#0b0e14;color:#e6e6e6;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
        ".card{max-width:520px;padding:32px;border:1px solid #222;border-radius:12px;background:#11151c}"
        f"h1{{color:{color};font-size:20px;margin:0 0 12px}}"
        "p{color:#9aa4b2;line-height:1.55}a{color:#3b82f6}</style></head>"
        f"<body><div class='card'><h1>{title}</h1><p>{message}</p>"
        "<p><a href='/'>&larr; Back to dashboard</a></p></div></body></html>"
    )
    return html, (200 if ok else 400)


@app.route("/auth/schwab")
def auth_schwab_start():
    if not (os.environ.get("SCHWAB_APP_KEY") and os.environ.get("SCHWAB_APP_SECRET")):
        return _auth_result_page(
            "Schwab not configured",
            "Set the SCHWAB_APP_KEY and SCHWAB_APP_SECRET secrets first.",
            ok=False,
        )
    from providers import schwab

    state = secrets.token_urlsafe(24)
    db.kv_set("schwab_oauth_state", {"state": state, "at": db.utcnow()})
    return redirect(schwab.authorize_url(_schwab_redirect_uri(), state))


@app.route("/auth/schwab/callback")
def auth_schwab_callback():
    from providers import schwab

    if request.args.get("error"):
        return _auth_result_page(
            "Schwab authorization declined",
            f"Schwab returned: {request.args.get('error')}",
            ok=False,
        )
    code = request.args.get("code")
    state = request.args.get("state")
    saved = db.kv_get("schwab_oauth_state") or {}
    if not code:
        return _auth_result_page("Missing code", "No authorization code in the callback URL.", ok=False)
    if not state or state != saved.get("state"):
        return _auth_result_page(
            "State mismatch",
            "The authorization state did not match. Start again from /auth/schwab.",
            ok=False,
        )
    db.kv_set("schwab_oauth_state", None)  # single-use
    try:
        tokens = schwab.exchange_code(code, _schwab_redirect_uri())
    except Exception as e:  # noqa: BLE001 — surface the failure to the browser
        return _auth_result_page("Token exchange failed", str(e), ok=False)
    refresh = tokens.get("refresh_token")
    if not refresh:
        return _auth_result_page("No refresh token", "Schwab did not return a refresh token.", ok=False)
    schwab.store_refresh_token(refresh)
    ingest.run_in_background("schwab-reauth")  # pull Schwab data right away
    return _auth_result_page(
        "Schwab re-authorized ✓",
        "Your refresh token is stored and valid for 7 days. A fresh ingest is "
        "running now and the dashboard will use Schwab as the primary feed.",
        ok=True,
    )


@app.route("/api/data-status")
def api_data_status():
    import status as status_mod

    return jsonify(status_mod.data_status())


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    token = os.environ.get("INGEST_TOKEN")
    if token:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() \
            or request.args.get("token", "")
        if supplied != token:
            return jsonify({"error": "unauthorized"}), 401
    if request.args.get("wait") == "1":
        # Synchronous: the cron machine uses this so the Fly machine stays
        # awake for the whole run.
        return jsonify(ingest.run(trigger="cron"))
    ingest.run_in_background("api")
    return jsonify({"ok": True, "started": True}), 202


# ---------------------------------------------------------------------------
# Schwab account sync — pull live positions + trade history on demand.
#
# Unlike every other API route, this one deliberately contacts a provider: it
# is user-triggered (the Positions tab "Sync from Schwab" button), returns
# account data that has no place in the market datastore, and degrades to a
# clear error if the Schwab app lacks the Accounts & Trading product.
# ---------------------------------------------------------------------------
@app.route("/api/account/status")
def api_account_status():
    import schwab_account

    return jsonify({
        "configured": schwab_account.available(),
        "lastError": db.kv_get("schwab_account_error"),
    })


@app.route("/api/account/sync", methods=["POST"])
def api_account_sync():
    import schwab_account

    body = request.get_json(silent=True) or {}
    days = body.get("days") or request.args.get("days") or schwab_account.MAX_SYNC_DAYS
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = schwab_account.MAX_SYNC_DAYS
    result = schwab_account.sync(days=days)
    return jsonify(result), 200 if result.get("configured") else 409


# ---------------------------------------------------------------------------
# Backtesting engine — configure a day-trading setup, run it against stored
# 5-minute bars, and get a trade log + summary stats. The run path is
# datastore-only; pulling missing intraday history from Schwab/Yahoo is an
# explicit, user-triggered action (the backfill endpoint, or autoBackfill).
# ---------------------------------------------------------------------------
@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    import backtest_service

    body = request.get_json(silent=True) or {}
    auto = bool(body.get("autoBackfill"))
    config = body.get("config") if "config" in body else body
    out = backtest_service.run(config, auto_backfill=auto)
    return jsonify(out), 200 if out.get("ok") else 400


@app.route("/api/backtest/coverage", methods=["POST"])
def api_backtest_coverage():
    import backtest as engine
    import backtest_service

    body = request.get_json(silent=True) or {}
    config, errors = engine.validate_config(body.get("config") if "config" in body else body)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    config = backtest_service._apply_default_sector_map(config)
    return jsonify({"ok": True, "coverage": backtest_service.coverage_report(config)})


@app.route("/api/backtest/backfill", methods=["POST"])
def api_backtest_backfill():
    import backtest as engine
    import backtest_service

    body = request.get_json(silent=True) or {}
    config, errors = engine.validate_config(body.get("config") if "config" in body else body)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    config = backtest_service._apply_default_sector_map(config)
    symbols = backtest_service._context_symbols(config)
    result = backtest_service.backfill(
        symbols, config["date_range"]["start"], config["date_range"]["end"],
        int(config.get("interval_min", 5)), engine._vol_avg_length(config),
        fine_symbols=config["tickers"],
        refine_interval_min=int(config.get("refine_interval_min", 1) or 0),
    )
    return jsonify({"ok": result["ok"], "backfill": result,
                    "coverage": backtest_service.coverage_report(config)})


@app.route("/api/backtest/export", methods=["POST"])
def api_backtest_export():
    import backtest as engine

    body = request.get_json(silent=True) or {}
    csv_text = engine.trades_to_csv(body.get("trades") or [])
    return app.response_class(
        csv_text, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=backtest_trades.csv"},
    )


@app.route("/api/backtest/resolve", methods=["GET", "POST"])
def api_backtest_resolve():
    import backtest_service

    if request.method == "GET":
        return jsonify(backtest_service.list_resolutions())
    body = request.get_json(silent=True) or {}
    ticker = str(body.get("ticker") or "").strip()
    date = str(body.get("date") or "").strip()
    entry_time = str(body.get("entry_time") or "").strip()
    if not (ticker and date and entry_time):
        return jsonify({"error": "ticker, date and entry_time are required"}), 400
    store = backtest_service.set_resolution(ticker, date, entry_time, body.get("outcome"))
    return jsonify({"ok": True, "resolutions": store})


@app.route("/api/backtest/configs", methods=["GET", "POST", "DELETE"])
def api_backtest_configs():
    import backtest_service

    if request.method == "GET":
        return jsonify(backtest_service.list_configs())
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    if request.method == "DELETE":
        return jsonify(backtest_service.delete_config(name))
    if body.get("config") is None:
        return jsonify({"error": "config required"}), 400
    try:
        store = backtest_service.save_config(name, body["config"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "configs": store})


# ---------------------------------------------------------------------------
# Intraday Setup Executor — Phase 1 (detection only). Reuses the backtest
# engine's setup rules to evaluate today's closed 5-minute candles for setups
# (price breaks yesterday's level on a volume spike) and emit signals. Refresh
# pulls today's bars from Schwab/Yahoo; playback replays stored candles to
# validate detection against historical data. No alerts/order execution yet.
# ---------------------------------------------------------------------------
@app.route("/api/executor/config", methods=["GET"])
def api_executor_config():
    import intraday_executor as ix

    return jsonify({"ok": True, "config": ix.DEFAULT_MONITOR_CONFIG})


def _executor_error(label: str, exc: Exception):
    """Turn an unexpected executor failure into a structured JSON error (logged
    with a traceback) instead of an opaque HTML 500, so the UI can show why."""
    print(f"[executor] {label} failed: {exc}")
    traceback.print_exc()
    return jsonify({"ok": False, "error": f"{label} failed: {exc}"}), 500


@app.route("/api/executor/monitor", methods=["POST"])
def api_executor_monitor():
    import intraday_executor as ix

    body = request.get_json(silent=True) or {}
    config = body.get("config") if "config" in body else body
    try:
        out = ix.run_monitor(config, refresh=bool(body.get("refresh")),
                             on_date=body.get("date"), as_of=body.get("asOf"))
    except Exception as e:  # noqa: BLE001 — surface the cause to the UI + logs
        return _executor_error("Scan", e)
    return jsonify(out), 200 if out.get("ok") else 400


@app.route("/api/executor/playback", methods=["POST"])
def api_executor_playback():
    import intraday_executor as ix

    body = request.get_json(silent=True) or {}
    config = body.get("config") if "config" in body else body
    try:
        out = ix.run_playback(config, date=body.get("date"),
                             date_range=body.get("date_range"),
                             auto_backfill=bool(body.get("autoBackfill")))
    except Exception as e:  # noqa: BLE001 — surface the cause to the UI + logs
        return _executor_error("Playback", e)
    return jsonify(out), 200 if out.get("ok") else 400


@app.route("/api/executor/replay", methods=["POST"])
def api_executor_replay():
    """Replay a date (or range) through the full executor engine and return
    completed trades with outcomes (entry/stop/target/exit/R).

    This is the offline validation path: REPLAY binds the historical data source +
    ReplayExecutionAdapter and resolves each setup's exit over stored candles using
    the same rules as the backtester — so the trades here reproduce backtest
    results exactly. Detection/sizing live in the shared StrategyCore; switching to
    PAPER/LIVE later changes only the bound adapter + data source.
    """
    import executor_engine as ee

    body = request.get_json(silent=True) or {}
    config = body.get("config") if "config" in body else body
    try:
        out = ee.run_replay(config, date=body.get("date"),
                            date_range=body.get("date_range"))
    except Exception as e:  # noqa: BLE001 — surface the cause to the UI + logs
        return _executor_error("Replay", e)
    return jsonify(out), 200 if out.get("ok") else 400


@app.route("/api/executor/signals", methods=["GET"])
def api_executor_signals():
    body_date = request.args.get("date")
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return jsonify({"ok": True, "signals": db.recent_setup_signals(body_date, limit)})


@app.route("/api/executor/paper/execute", methods=["POST"])
def api_executor_paper_execute():
    """Paper-only execution: logs the signal as an OPEN simulated bracket trade.

    No live/Schwab order placement is performed from this endpoint.
    """
    import intraday_executor as ix

    body = request.get_json(silent=True) or {}
    out = ix.execute_paper_order(body.get("signal") or body, notes=body.get("notes"))
    return jsonify(out), 200 if out.get("ok") else 400


@app.route("/api/executor/schwab/preview", methods=["POST"])
def api_executor_schwab_preview():
    """Dry-run a signal's bracket order against Schwab's previewOrder endpoint.

    Schwab has no paper-trading API, so this validates the order against the real
    account (buying power, pricing, tradeability) and returns the projected cost /
    fees and any rejects — but NOTHING is filled. It never places a live order.
    """
    import schwab_orders

    body = request.get_json(silent=True) or {}
    try:
        out = schwab_orders.preview_bracket(body.get("signal") or body,
                                            account_hash=body.get("accountHash"))
    except Exception as e:  # noqa: BLE001 — surface the cause to the UI + logs
        return _executor_error("Schwab preview", e)
    return jsonify(out), 200 if out.get("ok") else 400


@app.route("/api/executor/paper/trades", methods=["GET"])
def api_executor_paper_trades():
    import intraday_executor as ix

    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    out = ix.list_paper_trades(
        date=request.args.get("date"),
        status=request.args.get("status"),
        limit=limit,
    )
    return jsonify(out)


# ---------------------------------------------------------------------------
# State persistence (manual inputs, positions) — lives on the data volume
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(data: dict) -> None:
    # Gunicorn serves this with multiple threads and the frontend POSTs state
    # back debounced + on beforeunload, so writes overlap. A shared temp path
    # would let one writer's os.replace yank the file out from under another
    # (FileNotFoundError on the rename), so give each writer a unique temp file
    # in the same directory and atomically rename it into place.
    state_dir = os.path.dirname(STATE_FILE)
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix="state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        # Don't leak the temp file if the write or rename fails.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@app.route("/api/state", methods=["GET", "POST"])
def api_state():
    if request.method == "POST":
        save_state(request.get_json(force=True))
        return jsonify({"ok": True})
    return jsonify(load_state())


@app.route("/api/config")
def api_config():
    return jsonify({
        "tracked": cfg.TRACKED, "benchmark": cfg.BENCHMARK,
        "sectors": cfg.SECTOR_UNIVERSE,
        "capital": cfg.CAPITAL, "reserve": cfg.RESERVE,
        "rs3m": {
            "method": cfg.RS3M_METHOD, "emaSpan": cfg.RS3M_EMA_SPAN,
            "lookback": cfg.RS3M_LOOKBACK, "momWindow": cfg.RS3M_MOM_WINDOW,
            "momPastEndLag": cfg.RS3M_MOM_PAST_END_LAG,
            "momPastLookback": cfg.RS3M_MOM_PAST_LOOKBACK,
            "smooth": cfg.MOM_SMOOTH, "scale": cfg.MOM_SCALE,
            "rsiMethod": cfg.RSI_METHOD, "ma21Method": cfg.MA21_METHOD,
        },
    })


# ---------------------------------------------------------------------------
# Serve the built frontend (single origin -> no CORS issues at all)
# ---------------------------------------------------------------------------
@app.route("/")
@app.route("/<path:path>")
def serve(path="index.html"):
    full = os.path.join(FRONTEND, path)
    if os.path.exists(full) and not os.path.isdir(full):
        return send_from_directory(FRONTEND, path)
    if os.path.exists(os.path.join(FRONTEND, "index.html")):
        return send_from_directory(FRONTEND, "index.html")
    return (
        "<h2>Backend is running.</h2>"
        "<p>Frontend not built yet. The API works: try "
        "<a href='/api/indicators'>/api/indicators</a>.</p>", 200,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5179"))
    print(f"Rotation Dashboard backend  ->  http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
