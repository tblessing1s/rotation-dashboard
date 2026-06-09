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
from io import StringIO

from urllib.request import urlopen

import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import config as cfg
import indicators as ind
import macro as macro_data

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
_macro_mem: tuple[float, dict] | None = None


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
# Macro calculations (Level 1)
# ---------------------------------------------------------------------------
def _fred_series(url: str, value_col: str) -> pd.Series:
    """Load a public FRED graph CSV as a numeric Series indexed by date."""
    with urlopen(url, timeout=12) as resp:
        csv = resp.read().decode("utf-8")
    df = pd.read_csv(StringIO(csv))
    df["observation_date"] = pd.to_datetime(df["observation_date"])
    vals = pd.to_numeric(df[value_col].replace(".", pd.NA), errors="coerce")
    return pd.Series(vals.to_numpy(), index=df["observation_date"]).dropna()


def macro_breadth() -> dict:
    """Percent of configured broad-market ETFs above their 50-day moving average."""
    total = above = 0
    members = []
    window = cfg.BREADTH_MA_WINDOW
    for sym in cfg.BREADTH_SYMBOLS:
        bars = get_history(sym)
        if bars is None or len(bars) < window:
            continue
        close = bars["Close"].dropna()
        if len(close) < window:
            continue
        price = float(close.iloc[-1])
        ma = float(close.iloc[-window:].mean())
        is_above = price > ma
        total += 1
        above += 1 if is_above else 0
        members.append({"symbol": sym, "above": is_above, "price": round(price, 2), "ma50": round(ma, 2)})
    if total == 0:
        return {"value": None, "error": "no breadth data", "members": []}
    return {
        "value": round(above / total * 100, 0),
        "above": above,
        "total": total,
        "window": window,
        "members": members,
        "source": "Configured ETF universe above 50-day MA",
    }


def macro_fed_policy() -> dict:
    """Classify Fed stance from recent effective fed funds rate direction."""
    series = _fred_series(cfg.FRED_DFF_URL, "DFF")
    latest = float(series.iloc[-1])
    prior = float(series.iloc[-64]) if len(series) >= 64 else float(series.iloc[0])
    change = latest - prior
    if change >= 0.25:
        stance = "hawkish"
    elif change <= -0.25:
        stance = "dovish"
    else:
        stance = "holding"
    return {
        "value": stance,
        "rate": round(latest, 2),
        "change63d": round(change, 2),
        "asOf": str(series.index[-1].date()),
        "source": "FRED DFF, 63-trading-day rate change",
    }


def macro_inflation() -> dict:
    """Latest CPI year-over-year inflation rate."""
    series = _fred_series(cfg.FRED_CPI_URL, "CPIAUCSL")
    latest = float(series.iloc[-1])
    year_ago = float(series.iloc[-13]) if len(series) >= 13 else float(series.iloc[0])
    yoy = (latest / year_ago - 1) * 100
    return {
        "value": round(yoy, 1),
        "index": round(latest, 3),
        "asOf": str(series.index[-1].date()),
        "source": "FRED CPIAUCSL year-over-year",
    }


def macro_growth() -> dict:
    """Classify growth from real GDP annualized quarterly momentum."""
    series = _fred_series(cfg.FRED_GDPC1_URL, "GDPC1")
    latest = float(series.iloc[-1])
    prev = float(series.iloc[-2]) if len(series) >= 2 else latest
    prev2 = float(series.iloc[-3]) if len(series) >= 3 else prev
    qoq_ann = ((latest / prev) ** 4 - 1) * 100 if prev else 0.0
    prev_qoq_ann = ((prev / prev2) ** 4 - 1) * 100 if prev2 else qoq_ann
    if qoq_ann > prev_qoq_ann + 0.5:
        growth = "accelerating"
    elif qoq_ann < prev_qoq_ann - 0.5:
        growth = "slowing"
    else:
        growth = "stable"
    return {
        "value": growth,
        "qoqAnnualized": round(qoq_ann, 1),
        "previousQoqAnnualized": round(prev_qoq_ann, 1),
        "asOf": str(series.index[-1].date()),
        "source": "FRED GDPC1 real GDP quarterly momentum",
    }


def macro_snapshot() -> dict:
    """Return best-effort Level 1 macro values with field-level metadata."""
    global _macro_mem
    now = time.time()
    ttl = cfg.MACRO_CACHE_TTL_MINUTES * 60
    if _macro_mem and now - _macro_mem[0] < ttl:
        return _macro_mem[1]

    fields = {}
    errors = {}

    vix = latest_quote("^VIX")
    if vix.get("error"):
        errors["vix"] = "quote unavailable"
    else:
        fields["vix"] = {"value": vix["close"], "asOf": vix["date"], "source": "Yahoo Finance ^VIX"}

    calculators = {
        "breadth": macro_breadth,
        "fed": macro_fed_policy,
        "growth": macro_growth,
        "inflation": macro_inflation,
    }
    for key, fn in calculators.items():
        try:
            result = fn()
            if result.get("value") is None:
                errors[key] = result.get("error", "unavailable")
            else:
                fields[key] = result
        except Exception as e:
            errors[key] = str(e)

    values = {key: meta["value"] for key, meta in fields.items()}
    snapshot = {
        "values": values,
        "fields": fields,
        "errors": errors,
        "asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    _macro_mem = (now, snapshot)
    return snapshot


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
    requested = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in requested.split(",") if s.strip()] or cfg.TRACKED
    spy = get_history(cfg.BENCHMARK)
    out = {}
    for sym in dict.fromkeys(symbols):
        bars = get_history(sym)
        out[sym] = ind.compute_all(bars, spy, cfg) if bars is not None else {"error": "no data"}
    return jsonify(out)


@app.route("/api/macro")
def api_macro():
    return jsonify(macro_snapshot())


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
