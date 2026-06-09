"""
Rotation Dashboard — local backend.

Fetches quotes + daily history server-side (no CORS), caches to disk, computes
indicators, and persists your manual inputs / positions to a JSON file so your
work survives restarts.

Run:  python app.py     (then open http://localhost:5179)
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import config as cfg
import indicators as ind

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, cfg.CACHE_DIR)
STATE_FILE = os.path.join(HERE, "state.json")
FRONTEND = os.path.join(HERE, "..", "frontend", "dist")
os.makedirs(CACHE, exist_ok=True)

app = Flask(__name__, static_folder=None)
CORS(app)

# ---------------------------------------------------------------------------
# History cache (per symbol, on disk as parquet, with TTL)
# ---------------------------------------------------------------------------
_mem: dict[str, tuple[float, pd.DataFrame]] = {}


def _cache_path(symbol: str) -> str:
    safe = symbol.replace("^", "_")
    return os.path.join(CACHE, f"{safe}.parquet")


def get_history(symbol: str) -> pd.DataFrame | None:
    now = time.time()
    ttl = cfg.CACHE_TTL_MINUTES * 60

    # in-memory
    if symbol in _mem and now - _mem[symbol][0] < ttl:
        return _mem[symbol][1]

    # on-disk
    path = _cache_path(symbol)
    if os.path.exists(path) and now - os.path.getmtime(path) < ttl:
        try:
            df = pd.read_parquet(path)
            _mem[symbol] = (now, df)
            return df
        except Exception:
            pass

    # fetch fresh
    try:
        start = (datetime.now() - timedelta(days=cfg.HISTORY_DAYS)).strftime("%Y-%m-%d")
        df = yf.download(symbol, start=start, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return _stale(path)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.to_parquet(path)
        _mem[symbol] = (now, df)
        return df
    except Exception as e:
        print(f"[fetch] {symbol} failed: {e}")
        return _stale(path)


def _stale(path: str) -> pd.DataFrame | None:
    """Fall back to any cached copy even if expired."""
    if os.path.exists(path):
        try:
            return pd.read_parquet(path)
        except Exception:
            return None
    return None


def latest_quote(symbol: str) -> dict:
    df = get_history(symbol)
    if df is None or df.empty:
        return {"symbol": symbol, "error": True}
    last = df.iloc[-1]
    return {
        "symbol": symbol,
        "close": round(float(last["Close"]), 2),
        "open": round(float(last["Open"]), 2),
        "date": str(df.index[-1].date()),
    }


# ---------------------------------------------------------------------------
# State persistence (manual inputs, positions)
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
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.route("/api/quotes")
def api_quotes():
    return jsonify({s: latest_quote(s) for s in cfg.QUOTE_SYMBOLS})


@app.route("/api/indicators")
def api_indicators():
    spy = get_history(cfg.BENCHMARK)
    out = {}
    for sym in cfg.TRACKED:
        bars = get_history(sym)
        out[sym] = ind.compute_all(bars, spy, cfg) if bars is not None else {"error": "no data"}
    return jsonify(out)


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
        "capital": cfg.CAPITAL, "reserve": cfg.RESERVE,
        "rs3m": {"lookback": cfg.RS3M_LOOKBACK, "smooth": cfg.MOM_SMOOTH, "scale": cfg.MOM_SCALE},
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
