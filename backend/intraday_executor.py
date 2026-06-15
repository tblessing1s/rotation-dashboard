"""
Intraday Setup Executor — Phase 1: real-time setup *detection*.

This is the detection foundation of the executor spec. It reuses the backtest
engine's setup detectors, volume moving average, and Wilder ATR (backtest.py)
so live detection and historical backtests apply byte-for-byte identical rules:
a closed 5-minute candle that reaches/breaks yesterday's high or low on a volume
spike is a setup, and the resulting *signal* carries the entry / stop / target /
position size the backtester would have traded.

What this module does (Phase 1):
  * `detect_signals`   — pure core: evaluate closed candles and emit signals.
  * `monitor_status`   — per-ticker dashboard state (levels, last candle, vol).
  * service helpers     — wire the pure core to the datastore + the existing
                          Schwab/Yahoo backfill so "today's" 5-minute bars can be
                          pulled and replayed.

What it intentionally does NOT do yet (later phases): desktop/Slack alerts,
bracket-order placement, the React dashboard. The signal payload is shaped so
those phases can consume it directly.

Real-time data note: the existing stack is pull-based (Schwab pricehistory),
so "real-time" here means polling — refresh today's 5-minute bars, then detect
on the latest closed candle. A WebSocket tick feed is a future enhancement.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import backtest as engine

EXCHANGE_TZ = engine.EXCHANGE_TZ            # America/New_York (stored candle wall-clock)
WINDOW_TZ = engine.BACKTEST_WINDOW_TZ       # America/Chicago (config time window, like backtests)

# The executor's defaults mirror the spec: break of yesterday's level (price
# closes at/beyond it) on a >=2x volume spike, ATRx2 stop beyond the level,
# 2:1 target, 08:30–10:00 Central window, $20 fixed risk per trade.
DEFAULT_MONITOR_CONFIG = {
    "tickers": ["CRWV", "HIMS", "CVNA", "HOOD", "TOST"],
    "setup_conditions": {
        "type": "support_resistance_break",
        "use_yesterday_levels": True,
        "proximity_pct": 0.0,            # must close AT/through the level
    },
    "entry_rules": {
        "volume_multiplier": 2.0,
        "vol_avg_length": 50,
        "entry_timing": "candle_close",
    },
    "risk_reward": 2.0,
    "stop_logic": "atr_beyond_level",
    "stop_params": {
        "atr_multiplier": 2.0,
        "atr_period": 14,
        "atr_timeframe": "intraday",
    },
    "time_window": {"start_time": "08:30", "end_time": "10:00"},
    "interval_min": 5,
    "fixed_risk_per_trade": 20.0,        # dollars of risk sized into position_size
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def validate_monitor_config(raw: dict) -> tuple[dict, list[str]]:
    """Merge a partial monitor config over the defaults and validate it.

    The shared fields (tickers, setup, volume, stop, window, interval) are
    validated by the backtest engine so the two stay in lock-step; this adds the
    executor-only ``fixed_risk_per_trade`` knob. Returns ``(config, errors)``.
    """
    merged = engine._deep_merge(DEFAULT_MONITOR_CONFIG, raw or {})

    # Reuse the engine validator for the shared portion. It requires a
    # date_range; the executor doesn't, so probe with a throwaway range and drop
    # it afterwards (detection ranges come from the caller, not the config).
    probe = dict(merged)
    probe["date_range"] = {"start": "2000-01-01", "end": "2000-01-01"}
    cfg, errors = engine.validate_config(probe)
    cfg.pop("date_range", None)

    try:
        risk = float(merged.get("fixed_risk_per_trade", 0) or 0)
        if risk <= 0:
            errors.append("fixed_risk_per_trade must be greater than 0.")
        cfg["fixed_risk_per_trade"] = risk
    except (TypeError, ValueError):
        errors.append("fixed_risk_per_trade must be a number.")

    return cfg, errors


def _today_et() -> str:
    """Current trading-session date in exchange-local (ET) wall-clock."""
    return datetime.now(EXCHANGE_TZ).strftime("%Y-%m-%d")


def _to_et_naive(ts) -> pd.Timestamp:
    """Coerce a timestamp to tz-naive ET, matching the stored candle index."""
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert(EXCHANGE_TZ).tz_localize(None)
    return t


# ---------------------------------------------------------------------------
# Per-ticker data assembly (reuses engine indicators so rules never diverge)
# ---------------------------------------------------------------------------
def _ticker_context(ticker, on_date, config, *, get_intraday_range, get_daily):
    """Assemble everything detection needs for one ticker on ``on_date``.

    Returns a dict with the session window candles, yesterday's levels, the
    thinkorswim-style volume MA, and an ATR resolver — or ``None`` when there is
    no intraday session or no prior daily bar (the same skip rules the
    backtester applies, so a ticker that wouldn't backtest won't signal either).
    """
    interval = int(config.get("interval_min", 5))
    length = engine._vol_avg_length(config)
    tw = config["time_window"]
    atr_period = int(config["stop_params"].get("atr_period", 14))
    atr_mode = str(config["stop_params"].get("atr_timeframe", "intraday")).lower()

    # Load a buffer of prior sessions so the volume MA is fully formed from the
    # first candle of the day (matches the backtester / thinkorswim).
    hist_start = engine.intraday_history_start(on_date, length)
    series = get_intraday_range(ticker, hist_start, on_date, interval)
    if series is None or series.empty:
        return None
    series = series.sort_index()

    daily = get_daily(ticker)
    prior = engine._prior_daily_bar(daily, on_date)
    if prior is None:
        return {"missing": "no prior daily bar for yesterday's levels"}
    levels = {"y_high": float(prior["High"]), "y_low": float(prior["Low"])}

    vol_avg = series["Volume"].astype(float).rolling(length, min_periods=length).mean()

    if atr_mode == "daily":
        daily_atr = engine._atr_as_of(engine.wilder_atr(daily, atr_period), on_date)
        resolve_atr = lambda ts: daily_atr  # noqa: E731 — constant through the day
    else:
        iatr = engine.wilder_atr(series, atr_period)

        def resolve_atr(ts):
            if iatr is None:
                return None
            v = iatr.get(ts)
            return float(v) if v is not None and v == v else None

    session = series[series.index.normalize() == pd.Timestamp(on_date).normalize()]
    window = engine._window_candles(session, tw["start_time"], tw["end_time"], 0)
    return {
        "series": series, "session": session, "window": window,
        "levels": levels, "vol_avg": vol_avg, "resolve_atr": resolve_atr,
    }


def _build_signal(ticker, ts, candle, setup, *, levels, avg_volume, atr, config):
    """Turn a detected setup into a full signal (entry/stop/target/size).

    Mirrors ``backtest._scan_day``'s order math exactly, minus forward
    simulation. Returns ``None`` when the stop can't be placed (ATR missing) or
    the risk distance is zero — the same guards the backtester uses.
    """
    direction = setup["direction"]
    level = float(setup["level"])
    if config["entry_rules"]["entry_timing"] == "immediate_touch":
        entry = level
    else:
        entry = float(candle["Close"])

    place_stop = engine.STOP_LOGIC[config["stop_logic"]]
    stop = place_stop(direction, level=level, entry=entry, atr=atr, cfg=config)
    if stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    rr = float(config["risk_reward"])
    reward = risk * rr
    target = entry + reward if direction == "Long" else entry - reward

    entry_volume = float(candle["Volume"] or 0)
    volume_ratio = round(entry_volume / avg_volume, 2) if avg_volume > 0 else None
    risk_per_trade = float(config.get("fixed_risk_per_trade", 0) or 0)
    position_size = int(risk_per_trade // risk) if risk > 0 and risk_per_trade > 0 else 0

    return {
        "ticker": ticker,
        "date": pd.Timestamp(ts).strftime("%Y-%m-%d"),
        "candle_time": engine._central_time_label(ts),
        "direction": direction,
        "level_type": setup["level_type"],
        "level": round(level, 2),
        "entry_price": round(entry, 2),
        "stop_price": round(stop, 2),
        "target_price": round(target, 2),
        "risk": round(risk, 2),
        "reward": round(reward, 2),
        "risk_reward_ratio": round(reward / risk, 2) if risk else None,
        "position_size": position_size,
        "entry_volume": int(round(entry_volume)),
        "avg_volume": int(round(avg_volume)),
        "volume_ratio": volume_ratio,
        "atr": round(float(atr), 4) if atr else None,
    }


# ---------------------------------------------------------------------------
# Detection (pure core)
# ---------------------------------------------------------------------------
def detect_signals(config, *, get_intraday_range, get_daily,
                   on_date=None, as_of=None, mode="live") -> list[dict]:
    """Evaluate closed candles for setups and return signal dicts.

    Parameters
    ----------
    config : dict
        A validated monitor config (see ``validate_monitor_config``).
    get_intraday_range, get_daily : callables
        Datastore loaders, identical signatures to the backtest engine's.
    on_date : str | None
        ET session date (YYYY-MM-DD). Defaults to today (ET).
    as_of : timestamp | None
        Only candles that have *closed* by this instant are considered. Defaults
        to "now". A candle starting at ``ts`` closes at ``ts + interval``.
    mode : "live" | "playback"
        ``live`` returns at most one signal per ticker — the latest closed candle
        that qualifies. ``playback`` returns every qualifying candle in the
        window (used to validate detection against historical data).
    """
    on_date = on_date or _today_et()
    interval = int(config.get("interval_min", 5))
    mult = float(config["entry_rules"].get("volume_multiplier", 0) or 0)
    detect = engine.SETUP_TYPES[config["setup_conditions"]["type"]]
    as_of_ts = _to_et_naive(as_of) if as_of is not None else None

    signals: list[dict] = []
    for ticker in config["tickers"]:
        ctx = _ticker_context(ticker, on_date, config,
                              get_intraday_range=get_intraday_range, get_daily=get_daily)
        if not ctx or "window" not in ctx:
            continue
        window = ctx["window"]
        if window is None or window.empty:
            continue

        # Only consider candles whose close time has passed `as_of`.
        if as_of_ts is not None:
            closed = window[window.index + pd.Timedelta(minutes=interval) <= as_of_ts]
        else:
            closed = window
        if closed.empty:
            continue

        candidates = [closed.index[-1]] if mode == "live" else list(closed.index)
        for ts in candidates:
            candle = closed.loc[ts]
            avg_volume = float(ctx["vol_avg"].get(ts, float("nan")))
            if avg_volume != avg_volume:        # NaN — MA window not full yet
                continue
            setup = detect(candle, levels=ctx["levels"], avg_volume=avg_volume, cfg=config)
            if not setup:
                continue
            signal = _build_signal(
                ticker, ts, candle, setup,
                levels=ctx["levels"], avg_volume=avg_volume,
                atr=ctx["resolve_atr"](ts), config=config,
            )
            if signal is not None:
                signals.append(signal)

    signals.sort(key=lambda s: (s["date"], s.get("candle_time") or "", s["ticker"]))
    return signals


def monitor_status(config, *, get_intraday_range, get_daily,
                   on_date=None, as_of=None) -> list[dict]:
    """Per-ticker monitor state for the dashboard: yesterday's levels, the latest
    closed candle, distance to each level, and the live volume ratio. This is the
    "system ready to monitor" view — it never decides a setup, it just reports."""
    on_date = on_date or _today_et()
    interval = int(config.get("interval_min", 5))
    as_of_ts = _to_et_naive(as_of) if as_of is not None else None

    out: list[dict] = []
    for ticker in config["tickers"]:
        ctx = _ticker_context(ticker, on_date, config,
                              get_intraday_range=get_intraday_range, get_daily=get_daily)
        status: dict = {"ticker": ticker, "date": on_date}
        if not ctx:
            status["state"] = "no-data"
            out.append(status)
            continue
        if "window" not in ctx:
            status["state"] = "no-levels"
            status["note"] = ctx.get("missing")
            out.append(status)
            continue

        status["y_high"] = round(ctx["levels"]["y_high"], 2)
        status["y_low"] = round(ctx["levels"]["y_low"], 2)
        window = ctx["window"]
        if as_of_ts is not None and window is not None and not window.empty:
            window = window[window.index + pd.Timedelta(minutes=interval) <= as_of_ts]
        if window is None or window.empty:
            status["state"] = "waiting"
            out.append(status)
            continue

        ts = window.index[-1]
        candle = window.loc[ts]
        close = float(candle["Close"])
        avg_volume = float(ctx["vol_avg"].get(ts, float("nan")))
        status.update({
            "state": "monitoring",
            "last_candle_time": engine._central_time_label(ts),
            "last_close": round(close, 2),
            "last_volume": int(round(float(candle["Volume"] or 0))),
            "avg_volume": int(round(avg_volume)) if avg_volume == avg_volume else None,
            "volume_ratio": (round(float(candle["Volume"] or 0) / avg_volume, 2)
                             if avg_volume == avg_volume and avg_volume > 0 else None),
            "pct_to_high": round((status["y_high"] - close) / close * 100, 2) if close else None,
            "pct_to_low": round((close - status["y_low"]) / close * 100, 2) if close else None,
        })
        out.append(status)
    return out


# ---------------------------------------------------------------------------
# Service layer — wire the pure core to the datastore + provider backfill
# ---------------------------------------------------------------------------
def _loaders():
    import db

    daily_cache: dict[str, object] = {}

    def get_intraday_range(symbol, start, end, interval):
        return db.get_intraday_bars(symbol, start, end, interval)

    def get_daily(symbol):
        if symbol not in daily_cache:
            daily_cache[symbol] = db.get_bars(symbol)
        return daily_cache[symbol]

    return get_intraday_range, get_daily


def refresh_today(config, on_date=None) -> dict:
    """Pull today's 5-minute bars (plus the volume-MA buffer + daily history) from
    the provider chain into the datastore, so detection reads fresh candles. This
    is the polling stand-in for a real-time tick feed."""
    import backtest_service

    on_date = on_date or _today_et()
    return backtest_service.backfill(
        list(config["tickers"]), on_date, on_date,
        int(config.get("interval_min", 5)), engine._vol_avg_length(config),
        fine_symbols=[], refine_interval_min=0,
    )


def run_monitor(raw_config, *, refresh=False, on_date=None, as_of=None,
                persist=True) -> dict:
    """Validate, optionally refresh today's data, then detect (live) + report
    per-ticker status. Newly detected signals are logged idempotently."""
    config, errors = validate_monitor_config(raw_config)
    if errors:
        return {"ok": False, "errors": errors}

    refresh_result = refresh_today(config, on_date) if refresh else None
    get_intraday_range, get_daily = _loaders()
    signals = detect_signals(config, get_intraday_range=get_intraday_range,
                             get_daily=get_daily, on_date=on_date, as_of=as_of, mode="live")
    monitors = monitor_status(config, get_intraday_range=get_intraday_range,
                              get_daily=get_daily, on_date=on_date, as_of=as_of)

    new_signals = 0
    if persist:
        import db
        for sig in signals:
            if db.record_setup_signal(sig):
                new_signals += 1

    out = {"ok": True, "date": on_date or _today_et(), "signals": signals,
           "monitors": monitors, "newSignals": new_signals}
    if refresh_result is not None:
        out["refresh"] = refresh_result
    return out


def run_playback(raw_config, *, date=None, date_range=None, auto_backfill=False) -> dict:
    """Replay stored candles over a date (or range) and return every signal the
    detector would have fired — the Phase 1 way to validate detection logic
    against historical data. Read-only unless ``auto_backfill`` is set."""
    config, errors = validate_monitor_config(raw_config)
    if errors:
        return {"ok": False, "errors": errors}

    if date_range:
        start, end = date_range.get("start"), date_range.get("end")
    else:
        start = end = date
    if not start or not end:
        return {"ok": False, "errors": ["Provide date or date_range {start, end}."]}

    backfill_result = None
    if auto_backfill:
        import backtest_service
        backfill_result = backtest_service.backfill(
            list(config["tickers"]), start, end,
            int(config.get("interval_min", 5)), engine._vol_avg_length(config),
            fine_symbols=[], refine_interval_min=0,
        )

    get_intraday_range, get_daily = _loaders()
    all_signals: list[dict] = []
    for day in engine._session_dates(start, end):
        all_signals += detect_signals(config, get_intraday_range=get_intraday_range,
                                      get_daily=get_daily, on_date=day, mode="playback")
    out = {"ok": True, "date_range": {"start": start, "end": end},
           "signals": all_signals, "count": len(all_signals)}
    if backfill_result is not None:
        out["backfill"] = backfill_result
    return out
