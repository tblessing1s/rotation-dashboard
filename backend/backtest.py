"""
Stock backtesting engine for the Rotation Dashboard.

Given a configured day-trading *setup* (price reacting to yesterday's high/low
with a volume spike, etc.), this walks 5-minute candles chronologically and, for
each qualifying setup, computes entry / stop / target, then steps forward to see
whether the target or the stop is hit first. Every trade is logged with its
market context (SPY + sector direction) so an individual fill can be audited.

Design
------
* **Pure core.** ``run_backtest`` takes data *loaders* (callables) rather than
  reaching into the datastore, so the engine is deterministic and unit-testable
  with synthetic candles. ``backtest_service`` wires the loaders to db.py.
* **Modular rules.** Setup detectors and stop-placement rules are registered in
  ``SETUP_TYPES`` / ``STOP_LOGIC``. Adding a new setup or stop style is a small,
  isolated function — no change to the walk-forward loop.
* **No look-ahead.** Yesterday's levels and ATR come strictly from sessions
  *before* the trading day; market-context direction is read from intraday data
  up to the entry candle only.
"""
from __future__ import annotations

from datetime import date as _date, datetime, timedelta

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration: defaults, schema, validation
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "tickers": [],
    "date_range": {"start": None, "end": None},
    "setup_conditions": {
        "type": "support_resistance_bounce",
        "use_yesterday_levels": True,
        "proximity_pct": 0.30,        # how close (%) price must come to the level
    },
    "entry_rules": {
        "volume_multiplier": 2.0,     # candle volume must exceed N x the volume average
        # Bars in the volume moving average. Matches thinkorswim's Volume Avg study,
        # Average(volume, length): a simple MA that *includes the current bar* and
        # runs continuously across days. TOS default is 50.
        "vol_avg_length": 50,
        "entry_timing": "candle_close",  # or "immediate_touch"
    },
    "skip_conditions": {
        "skip_first_n_candles": 0,
        "skip_if_spy_down": False,
        "skip_if_sector_down": False,
    },
    "risk_reward": 2.0,
    "stop_logic": "atr_divided_by_2",  # atr_divided_by_2 | fixed_distance | just_beyond_level
    "stop_params": {
        "fixed_distance": 0.50,        # used by fixed_distance
        "buffer_pct": 0.10,            # used by just_beyond_level (% beyond the level)
        "atr_period": 14,
    },
    "time_window": {"start_time": "09:30", "end_time": "11:00"},
    "interval_min": 5,
    "sector_map": {},                  # ticker -> sector proxy symbol (e.g. AMD -> XLK)
}

ENTRY_TIMINGS = {"candle_close", "immediate_touch"}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _valid_date(s) -> str | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _valid_time(s) -> str | None:
    try:
        return datetime.strptime(str(s), "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def validate_config(raw: dict) -> tuple[dict, list[str]]:
    """Merge a partial config over defaults and validate it.

    Returns ``(config, errors)``. When ``errors`` is non-empty the config should
    not be run; the messages are user-facing.
    """
    cfg = _deep_merge(DEFAULT_CONFIG, raw or {})
    errors: list[str] = []

    tickers = [str(t).strip().upper() for t in (cfg.get("tickers") or []) if str(t).strip()]
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        errors.append("Select at least one ticker.")
    cfg["tickers"] = tickers

    dr = cfg.get("date_range") or {}
    start, end = _valid_date(dr.get("start")), _valid_date(dr.get("end"))
    if not start or not end:
        errors.append("date_range.start and date_range.end must be YYYY-MM-DD dates.")
    elif start > end:
        errors.append("date_range.start must be on or before date_range.end.")
    cfg["date_range"] = {"start": start, "end": end}

    if cfg["setup_conditions"].get("type") not in SETUP_TYPES:
        errors.append(
            f"Unknown setup type '{cfg['setup_conditions'].get('type')}'. "
            f"Available: {', '.join(sorted(SETUP_TYPES))}."
        )

    if cfg["entry_rules"].get("entry_timing") not in ENTRY_TIMINGS:
        errors.append(f"entry_timing must be one of {sorted(ENTRY_TIMINGS)}.")
    try:
        if float(cfg["entry_rules"].get("volume_multiplier", 0)) < 0:
            errors.append("volume_multiplier cannot be negative.")
    except (TypeError, ValueError):
        errors.append("volume_multiplier must be a number.")

    try:
        rr = float(cfg.get("risk_reward"))
        if rr <= 0:
            errors.append("risk_reward must be greater than 0.")
        cfg["risk_reward"] = rr
    except (TypeError, ValueError):
        errors.append("risk_reward must be a number (e.g. 2 for 2:1).")

    if cfg.get("stop_logic") not in STOP_LOGIC:
        errors.append(
            f"Unknown stop_logic '{cfg.get('stop_logic')}'. "
            f"Available: {', '.join(sorted(STOP_LOGIC))}."
        )

    tw = cfg.get("time_window") or {}
    st, et = _valid_time(tw.get("start_time")), _valid_time(tw.get("end_time"))
    if not st or not et:
        errors.append("time_window start_time/end_time must be HH:MM (24-hour).")
    elif st >= et:
        errors.append("time_window start_time must be before end_time.")
    cfg["time_window"] = {"start_time": st, "end_time": et}

    try:
        cfg["interval_min"] = int(cfg.get("interval_min") or 5)
    except (TypeError, ValueError):
        errors.append("interval_min must be an integer number of minutes.")

    return cfg, errors


# ---------------------------------------------------------------------------
# Pluggable rules: setup detectors and stop-placement logic
# ---------------------------------------------------------------------------
SETUP_TYPES: dict = {}
STOP_LOGIC: dict = {}


def register_setup(name):
    def deco(fn):
        SETUP_TYPES[name] = fn
        return fn
    return deco


def register_stop(name):
    def deco(fn):
        STOP_LOGIC[name] = fn
        return fn
    return deco


@register_setup("support_resistance_bounce")
def _detect_sr_bounce(candle, *, levels, avg_volume, cfg) -> dict | None:
    """Price reacts off yesterday's high (short) or low (long) with a volume spike.

    Returns a setup dict ``{direction, level, level_type, volume_spike}`` or
    None. A bounce requires the candle to *trade into* the level and *close back
    on the right side of it* — a long holds above Y-Low, a short rejects Y-High.
    """
    prox = float(cfg["setup_conditions"].get("proximity_pct", 0.30)) / 100.0
    mult = float(cfg["entry_rules"].get("volume_multiplier", 0) or 0)
    volume_spike = _volume_spike(candle, avg_volume, mult)
    # A volume spike is part of this setup; when a multiplier is configured it
    # must be met for the setup to qualify.
    if mult > 0 and not volume_spike:
        return None

    hit = _sr_level_touch(candle, levels, prox)
    if hit is None:
        return None
    direction, level, level_type = hit
    return {"direction": direction, "level": level, "level_type": level_type,
            "volume_spike": volume_spike}


def _volume_spike(candle, avg_volume, mult) -> bool:
    vol = float(candle["Volume"] or 0)
    return avg_volume > 0 and vol >= mult * avg_volume


def _sr_level_touch(candle, levels, prox):
    """(direction, level, level_type) when the candle reaches and *holds* a
    yesterday level, else None. Volume is not considered here.

    Long: the wick reached down into yesterday's low zone — within `prox` above
    the level, or through it — and closed back *above* the level (support held).
    With prox=0 this is a clean touch, not an exact-equality match. Short is the
    mirror at yesterday's high.
    """
    y_high, y_low = levels.get("y_high"), levels.get("y_low")
    high, low, close = float(candle["High"]), float(candle["Low"]), float(candle["Close"])
    if y_low is not None and low <= y_low * (1 + prox) and close >= y_low:
        return ("Long", y_low, "Y-Low")
    if y_high is not None and high >= y_high * (1 - prox) and close <= y_high:
        return ("Short", y_high, "Y-High")
    return None


@register_stop("atr_divided_by_2")
def _stop_atr_half(direction, *, level, entry, atr, cfg):
    if atr is None or atr <= 0:
        return None
    pad = atr / 2.0
    return level - pad if direction == "Long" else level + pad


@register_stop("fixed_distance")
def _stop_fixed(direction, *, level, entry, atr, cfg):
    dist = abs(float(cfg["stop_params"].get("fixed_distance", 0.50)))
    return entry - dist if direction == "Long" else entry + dist


@register_stop("just_beyond_level")
def _stop_just_beyond(direction, *, level, entry, atr, cfg):
    buf = abs(float(cfg["stop_params"].get("buffer_pct", 0.10))) / 100.0
    return level * (1 - buf) if direction == "Long" else level * (1 + buf)


# ---------------------------------------------------------------------------
# Indicators used by the engine
# ---------------------------------------------------------------------------
def wilder_atr(daily: pd.DataFrame, period: int = 14) -> pd.Series | None:
    """Wilder ATR series indexed like `daily` (NaN until enough history)."""
    if daily is None or len(daily) < 2:
        return None
    high = daily["High"].astype(float)
    low = daily["Low"].astype(float)
    close = daily["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _prior_daily_bar(daily: pd.DataFrame | None, day: str):
    """The most recent daily bar strictly before `day` (yesterday's session)."""
    if daily is None or daily.empty:
        return None
    target = pd.Timestamp(day).normalize()
    prior = daily[daily.index.normalize() < target]
    return prior.iloc[-1] if len(prior) else None


def _atr_as_of(atr: pd.Series | None, day: str) -> float | None:
    if atr is None:
        return None
    target = pd.Timestamp(day).normalize()
    prior = atr[atr.index.normalize() < target].dropna()
    return float(prior.iloc[-1]) if len(prior) else None


# ---------------------------------------------------------------------------
# Market-context direction (SPY / sector)
# ---------------------------------------------------------------------------
def _direction_from_intraday(intraday: pd.DataFrame | None, at_ts) -> str | None:
    """Up/Down measured as price-so-far vs the session open, up to `at_ts`."""
    if intraday is None or intraday.empty:
        return None
    day_open = float(intraday.iloc[0]["Open"])
    upto = intraday[intraday.index <= at_ts]
    if upto.empty:
        return None
    cur = float(upto.iloc[-1]["Close"])
    return "Up" if cur >= day_open else "Down"


def _context_direction(symbol, day, at_ts, *, day_session, get_daily) -> str:
    if not symbol:
        return "Unknown"
    intraday = day_session(symbol, day)
    direction = _direction_from_intraday(intraday, at_ts)
    if direction:
        return direction
    # Fall back to the prior daily session's own direction (no look-ahead).
    prior = _prior_daily_bar(get_daily(symbol), day)
    if prior is not None and prior.get("Open") is not None:
        return "Up" if float(prior["Close"]) >= float(prior["Open"]) else "Down"
    return "Unknown"


def _vol_avg_length(cfg) -> int:
    """Bars in the volume MA. Honors vol_avg_length, with vol_lookback kept as a
    back-compat alias for configs saved before the rename."""
    er = cfg.get("entry_rules", {})
    return int(er.get("vol_avg_length") or er.get("vol_lookback") or 50)


def intraday_history_start(start: str, length: int) -> str:
    """Earliest ET date whose intraday bars must be loaded so the first requested
    session's volume MA(length) is already fully formed — i.e. the MA matches
    thinkorswim, which carries the window across the prior day(s). A regular
    session is ~78 five-minute bars; buffer enough trading days for `length`."""
    sessions = max(1, -(-int(length) // 78) + 1)  # ceil(length/78) + 1
    cal_days = sessions * 2 + 5                    # generous weekday->calendar pad
    return (pd.Timestamp(start) - pd.Timedelta(days=cal_days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------
def _simulate(direction, entry, stop, target, forward: pd.DataFrame):
    """Step through `forward` candles; return (outcome, exit_price, exit_ts, note).

    If the stop and target are both touched within one candle we assume the stop
    filled first (the conservative read for a 5-minute bar).
    """
    risk = abs(entry - stop)
    for ts, c in forward.iterrows():
        hi, lo = float(c["High"]), float(c["Low"])
        if direction == "Long":
            hit_stop, hit_target = lo <= stop, hi >= target
        else:
            hit_stop, hit_target = hi >= stop, lo <= target
        if hit_stop:
            return "Loss", stop, ts, ""
        if hit_target:
            return "Win", target, ts, ""
    # Neither level reached during the session: mark to the last close.
    if forward.empty:
        return "Loss", entry, None, "no candles after entry"
    last_ts = forward.index[-1]
    exit_price = float(forward.iloc[-1]["Close"])
    r = (exit_price - entry) / risk if direction == "Long" else (entry - exit_price) / risk
    return ("Win" if r > 0 else "Loss"), exit_price, last_ts, "closed at session end"


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------
def _session_dates(start: str, end: str) -> list[str]:
    """NYSE trading days in [start, end] — weekends and full holidays excluded so
    a closed day (e.g. Memorial Day) never shows up as a missing session."""
    try:
        import market_calendar as mcal
        is_trading = mcal.is_trading_day
    except Exception:  # noqa: BLE001 — fall back to plain weekday logic
        is_trading = lambda d: d.weekday() < 5
    out = []
    d = pd.Timestamp(start).date()
    last = pd.Timestamp(end).date()
    while d <= last:
        if is_trading(d):
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _window_candles(intraday: pd.DataFrame, start_time: str, end_time: str,
                    skip_first_n: int) -> pd.DataFrame:
    """Session candles inside the time window, after dropping the first N of the day."""
    if intraday is None or intraday.empty:
        return intraday
    session = intraday.sort_index()
    if skip_first_n > 0:
        session = session.iloc[skip_first_n:]
    t = session.index.time
    st = datetime.strptime(start_time, "%H:%M").time()
    et = datetime.strptime(end_time, "%H:%M").time()
    mask = [(x >= st) and (x <= et) for x in t]
    return session[mask]


def run_backtest(config: dict, *, get_intraday_range, get_daily) -> dict:
    """Run a backtest.

    Parameters
    ----------
    config : dict
        A validated config (see ``validate_config``).
    get_intraday_range : callable(symbol, start, end, interval_min) -> DataFrame | None
        Continuous intraday OHLCV for [start, end] (tz-naive ET index, may span
        multiple sessions). Loaded with a buffer before the requested range so
        the volume MA matches thinkorswim from the first session.
    get_daily : callable(symbol) -> DataFrame | None
        Daily OHLCV (date index) for yesterday's levels and ATR.

    Returns
    -------
    dict with keys ``summary``, ``trades``, ``coverage``, ``warnings``,
    ``diagnostics``, ``config``.
    """
    cfg = config
    rr = float(cfg["risk_reward"])
    interval = int(cfg.get("interval_min", 5))
    length = _vol_avg_length(cfg)
    detect = SETUP_TYPES[cfg["setup_conditions"]["type"]]
    place_stop = STOP_LOGIC[cfg["stop_logic"]]
    tw = cfg["time_window"]
    skip = cfg["skip_conditions"]
    skip_n = int(skip.get("skip_first_n_candles", 0) or 0)
    sector_map = {k.upper(): v for k, v in (cfg.get("sector_map") or {}).items()}

    start, end = cfg["date_range"]["start"], cfg["date_range"]["end"]
    hist_start = intraday_history_start(start, length)

    # Cache daily bars + ATR + the continuous intraday series & its volume MA.
    daily_cache: dict[str, pd.DataFrame | None] = {}
    atr_cache: dict[str, pd.Series | None] = {}
    series_cache: dict[str, pd.DataFrame | None] = {}
    volavg_cache: dict[str, pd.Series | None] = {}

    def daily(sym):
        if sym not in daily_cache:
            daily_cache[sym] = get_daily(sym)
        return daily_cache[sym]

    def atr_series(sym):
        if sym not in atr_cache:
            atr_cache[sym] = wilder_atr(daily(sym), int(cfg["stop_params"].get("atr_period", 14)))
        return atr_cache[sym]

    def series(sym):
        if sym not in series_cache:
            s = get_intraday_range(sym, hist_start, end, interval)
            series_cache[sym] = s.sort_index() if s is not None and not s.empty else None
        return series_cache[sym]

    def vol_avg(sym):
        # thinkorswim Average(volume, length): simple MA including the current
        # bar, continuous across days. A full window is required (NaN until then).
        if sym not in volavg_cache:
            s = series(sym)
            volavg_cache[sym] = (
                s["Volume"].astype(float).rolling(length, min_periods=length).mean()
                if s is not None else None
            )
        return volavg_cache[sym]

    def day_session(sym, day):
        s = series(sym)
        if s is None:
            return None
        d = s[s.index.normalize() == pd.Timestamp(day).normalize()]
        return d if not d.empty else None

    dates = _session_dates(start, end)
    trades: list[dict] = []
    warnings: list[str] = []
    coverage = {"requested_sessions": 0, "missing": [], "covered": 0}
    # Why a run produced the trades it did — so "0 trades" is never a black box.
    diag = {"candles_evaluated": 0, "level_touches": 0, "volume_spikes": 0,
            "setups_detected": 0, "setups_skipped": 0}

    for ticker in cfg["tickers"]:
        proxy = sector_map.get(ticker)
        ticker_vol_avg = vol_avg(ticker)
        for day in dates:
            coverage["requested_sessions"] += 1
            session = day_session(ticker, day)
            if session is None:
                coverage["missing"].append({"ticker": ticker, "date": day})
                continue
            coverage["covered"] += 1

            prior = _prior_daily_bar(daily(ticker), day)
            if prior is None:
                warnings.append(f"{ticker} {day}: no prior daily bar for yesterday's levels — skipped.")
                continue
            levels = {"y_high": float(prior["High"]), "y_low": float(prior["Low"])}
            atr_val = _atr_as_of(atr_series(ticker), day)

            window = _window_candles(session, tw["start_time"], tw["end_time"], skip_n)
            if window is None or window.empty:
                continue

            trade = _scan_day(
                ticker, day, session, window, levels, atr_val, proxy, ticker_vol_avg,
                cfg=cfg, rr=rr, detect=detect, place_stop=place_stop, skip=skip,
                day_session=day_session, get_daily=daily, warnings=warnings, diag=diag,
            )
            if trade:
                trades.append(trade)

    trades.sort(key=lambda t: (t["date"], t.get("entry_time") or "", t["ticker"]))
    return {
        "summary": summarize(trades),
        "trades": trades,
        "coverage": coverage,
        "warnings": warnings,
        "diagnostics": diag,
        "config": cfg,
    }


def _scan_day(ticker, day, session, window, levels, atr_val, proxy, vol_avg_series, *, cfg, rr,
              detect, place_stop, skip, day_session, get_daily, warnings, diag):
    """Find the first qualifying setup of the day and resolve it to a trade/skip.

    `vol_avg_series` is the ticker's thinkorswim-style volume MA (continuous,
    includes the current bar) indexed by the same timestamps as the candles.
    """
    is_sr = cfg["setup_conditions"]["type"] == "support_resistance_bounce"
    prox = float(cfg["setup_conditions"].get("proximity_pct", 0.30)) / 100.0
    mult = float(cfg["entry_rules"].get("volume_multiplier", 0) or 0)
    for ts, candle in window.iterrows():
        avg_volume = float(vol_avg_series.get(ts, float("nan"))) if vol_avg_series is not None else float("nan")
        if avg_volume != avg_volume:  # NaN — MA window not yet full at this bar
            continue

        diag["candles_evaluated"] += 1
        if is_sr:
            if _sr_level_touch(candle, levels, prox) is not None:
                diag["level_touches"] += 1
            if _volume_spike(candle, avg_volume, mult):
                diag["volume_spikes"] += 1

        setup = detect(candle, levels=levels, avg_volume=avg_volume, cfg=cfg)
        if not setup:
            continue
        diag["setups_detected"] += 1

        direction = setup["direction"]
        spy_dir = _context_direction("SPY", day, ts, day_session=day_session, get_daily=get_daily)
        sector_dir = _context_direction(proxy, day, ts, day_session=day_session, get_daily=get_daily)

        entry_volume = float(candle["Volume"] or 0)
        volume_ratio = round(entry_volume / avg_volume, 2) if avg_volume > 0 else None
        base = {
            "date": day, "ticker": ticker, "level_type": setup["level_type"],
            "volume_spike": bool(setup.get("volume_spike")), "direction": direction,
            "entry_time": ts.strftime("%H:%M"), "spy_direction": spy_dir,
            "sector_direction": sector_dir,
            "entry_volume": int(round(entry_volume)),
            "avg_volume": int(round(avg_volume)),
            "volume_ratio": volume_ratio,
        }

        # Skip conditions: a real setup blocked by market context is a logged skip.
        if skip.get("skip_if_spy_down") and spy_dir == "Down":
            diag["setups_skipped"] += 1
            return {**base, "entry_price": None, "stop_price": None, "target_price": None,
                    "exit_price": None, "outcome": "Skip", "r_result": 0.0,
                    "exit_time": None, "notes": "skipped: SPY direction down"}
        if skip.get("skip_if_sector_down") and sector_dir == "Down":
            diag["setups_skipped"] += 1
            return {**base, "entry_price": None, "stop_price": None, "target_price": None,
                    "exit_price": None, "outcome": "Skip", "r_result": 0.0,
                    "exit_time": None, "notes": "skipped: sector direction down"}

        level = setup["level"]
        if cfg["entry_rules"]["entry_timing"] == "immediate_touch":
            entry = float(level)
        else:
            entry = float(candle["Close"])

        stop = place_stop(direction, level=level, entry=entry, atr=atr_val, cfg=cfg)
        if stop is None:
            warnings.append(f"{ticker} {day}: ATR unavailable for stop placement — setup skipped.")
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            warnings.append(f"{ticker} {day}: zero risk distance — setup skipped.")
            return None
        target = entry + risk * rr if direction == "Long" else entry - risk * rr

        forward = session[session.index > ts]
        outcome, exit_price, exit_ts, note = _simulate(direction, entry, stop, target, forward)
        r_result = (exit_price - entry) / risk if direction == "Long" else (entry - exit_price) / risk

        return {
            **base,
            "entry_price": round(entry, 2),
            "stop_price": round(stop, 2),
            "target_price": round(target, 2),
            "exit_price": round(exit_price, 2),
            "outcome": outcome,
            "r_result": round(r_result, 2),
            "exit_time": exit_ts.strftime("%H:%M") if exit_ts is not None else None,
            "notes": note,
        }
    return None


def summarize(trades: list[dict]) -> dict:
    """Win rate, average R, and expectancy over the resolved (non-skip) trades."""
    resolved = [t for t in trades if t["outcome"] in ("Win", "Loss")]
    skips = sum(1 for t in trades if t["outcome"] == "Skip")
    wins = [t for t in resolved if t["outcome"] == "Win"]
    losses = [t for t in resolved if t["outcome"] == "Loss"]
    n = len(resolved)

    def avg(rows):
        return round(sum(t["r_result"] for t in rows) / len(rows), 3) if rows else 0.0

    avg_win = avg(wins)
    avg_loss = avg(losses)
    win_rate = round(len(wins) / n * 100, 1) if n else 0.0
    expectancy = round(sum(t["r_result"] for t in resolved) / n, 3) if n else 0.0
    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "skips": skips,
        "win_rate_percent": win_rate,
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "expectancy_per_trade": expectancy,
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "date", "ticker", "level_type", "volume_spike", "entry_volume", "avg_volume",
    "volume_ratio", "direction", "entry_time", "entry_price", "stop_price",
    "target_price", "exit_price", "exit_time", "outcome", "r_result",
    "spy_direction", "sector_direction", "notes",
]


def trades_to_csv(trades: list[dict]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        writer.writerow({k: t.get(k, "") for k in CSV_COLUMNS})
    return buf.getvalue()
