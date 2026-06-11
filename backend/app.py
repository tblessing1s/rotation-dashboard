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
import shutil
import tempfile
import time

from flask import Flask, jsonify, request, send_from_directory
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
        out[symbol] = {
            "symbol": symbol,
            "close": round(bar["close"], 2),
            "open": round(bar["open"], 2) if bar["open"] is not None else None,
            "date": bar["date"],
            "source": bar["source"],
            "fetchedAt": bar["fetched_at"],
            "staleness": _bar_staleness(bar["date"]),
        }
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
    return jsonify({
        "quarantine": db.recent_quarantine(),
        "lastRun": db.last_ingest_run(),
        "lastSuccessfulRun": db.last_successful_ingest(),
        "schwabAuthError": db.kv_get("schwab_auth_error"),
    })


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
            "smooth": cfg.MOM_SMOOTH, "scale": cfg.MOM_SCALE,
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
