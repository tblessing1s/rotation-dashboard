"""Backtest engine tests.

Two halves:
  1. the pure engine (backtest.py) driven by synthetic candles with
     hand-computed expected entry/stop/target/outcome, and
  2. the intraday datastore round-trip (db.py) the service layer relies on.
"""
import numpy as np
import pandas as pd

import backtest as engine
import db


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------
def daily_frame(end="2026-06-01", periods=20, high=110.0, low=100.0, close=105.0):
    """Flat daily bars: every session ranges 100–110, so Wilder ATR == 10."""
    idx = pd.bdate_range(end=end, periods=periods)
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": 1_000_000.0},
        index=idx,
    )


def intraday(date, rows):
    """rows: list of (HH:MM, open, high, low, close, volume) in ET."""
    idx = pd.to_datetime([f"{date} {r[0]}" for r in rows])
    data = {k: [] for k in ("Open", "High", "Low", "Close", "Volume")}
    for _, o, h, l, c, v in rows:
        data["Open"].append(o); data["High"].append(h); data["Low"].append(l)
        data["Close"].append(c); data["Volume"].append(v)
    return pd.DataFrame(data, index=idx)


def make_loaders(intraday_map, daily_map):
    def get_intraday(sym, date, interval=5):
        return intraday_map.get((sym, date))

    def get_daily(sym):
        return daily_map.get(sym)

    return get_intraday, get_daily


def base_config(**over):
    cfg = {
        "tickers": ["AMD"],
        "date_range": {"start": "2026-06-01", "end": "2026-06-01"},
        "time_window": {"start_time": "09:30", "end_time": "11:00"},
        "risk_reward": 2,
        "stop_logic": "atr_divided_by_2",
    }
    cfg.update(over)
    c, errors = engine.validate_config(cfg)
    assert not errors, errors
    return c


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def test_validate_config_defaults_and_errors():
    cfg, errors = engine.validate_config({"tickers": ["amd", "amd", "hood"],
                                          "date_range": {"start": "2026-05-15", "end": "2026-06-14"}})
    assert not errors
    assert cfg["tickers"] == ["AMD", "HOOD"]              # upper-cased + de-duped
    assert cfg["entry_rules"]["entry_timing"] == "candle_close"

    _, errs = engine.validate_config({"tickers": [], "risk_reward": -1,
                                     "stop_logic": "bogus",
                                     "date_range": {"start": "2026-06-10", "end": "2026-06-01"}})
    joined = " ".join(errs)
    assert "ticker" in joined and "risk_reward" in joined and "stop_logic" in joined
    assert "on or before" in joined


# ---------------------------------------------------------------------------
# A known winning long off yesterday's low
# ---------------------------------------------------------------------------
def test_long_bounce_win_matches_manual_calc():
    # Y-Low = 100, ATR = 10. Dip to 99.9 with a volume spike, close 101.
    # entry (candle close) = 101, stop = level - ATR/2 = 95, risk = 6,
    # target = 101 + 2*6 = 113. Price later tags 114 -> Win at 113, R = +2.
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000),
        ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 101, 102, 99.9, 101, 5000),   # setup candle (spike, touches Y-Low)
        ("09:50", 103, 104, 100.5, 103, 1200),
        ("09:55", 106, 108, 103, 107, 1200),
        ("10:00", 111, 114, 110, 113, 1200),    # tags the 113 target
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    out = engine.run_backtest(base_config(), get_intraday=loaders[0], get_daily=loaders[1])

    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["direction"] == "Long" and t["level_type"] == "Y-Low"
    assert t["volume_spike"] is True
    assert (t["entry_price"], t["stop_price"], t["target_price"]) == (101.0, 95.0, 113.0)
    assert t["outcome"] == "Win" and t["exit_price"] == 113.0 and t["r_result"] == 2.0
    assert out["summary"]["win_rate_percent"] == 100.0
    assert out["summary"]["expectancy_per_trade"] == 2.0


def test_short_rejection_loss_and_summary_math():
    # Two sessions: the winning long above, plus a short off Y-High that loses.
    # Y-High = 110, entry 109, stop 115, risk 6, target 97; price tags 116 -> stop.
    win_day, loss_day = "2026-06-01", "2026-06-02"
    win_bars = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000), ("09:45", 101, 102, 99.9, 101, 5000),
        ("10:00", 111, 114, 110, 113, 1200),
    ]
    loss_bars = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 109, 110.1, 108, 109, 5000),  # pokes Y-High, closes below -> Short
        ("10:00", 113, 116, 112, 115, 1200),    # tags the 115 stop
    ]
    loaders = make_loaders(
        {("AMD", win_day): intraday(win_day, win_bars),
         ("AMD", loss_day): intraday(loss_day, loss_bars)},
        {"AMD": daily_frame()},
    )
    cfg = base_config(date_range={"start": win_day, "end": loss_day})
    out = engine.run_backtest(cfg, get_intraday=loaders[0], get_daily=loaders[1])

    outcomes = {t["date"]: t for t in out["trades"]}
    assert outcomes[loss_day]["direction"] == "Short"
    assert (outcomes[loss_day]["entry_price"], outcomes[loss_day]["stop_price"]) == (109.0, 115.0)
    assert outcomes[loss_day]["outcome"] == "Loss" and outcomes[loss_day]["r_result"] == -1.0

    s = out["summary"]
    assert (s["total_trades"], s["wins"], s["losses"]) == (2, 1, 1)
    assert s["win_rate_percent"] == 50.0
    assert s["avg_win_r"] == 2.0 and s["avg_loss_r"] == -1.0
    assert s["expectancy_per_trade"] == 0.5


def test_skip_first_n_candles_blocks_setup():
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000), ("09:45", 101, 102, 99.9, 101, 5000),
        ("10:00", 111, 114, 110, 113, 1200),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    # Dropping the first 4 candles removes the 09:45 setup entirely.
    cfg = base_config(skip_conditions={"skip_first_n_candles": 4})
    out = engine.run_backtest(cfg, get_intraday=loaders[0], get_daily=loaders[1])
    assert out["trades"] == []


def test_skip_if_spy_down_logs_a_skip():
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000), ("09:45", 101, 102, 99.9, 101, 5000),
        ("10:00", 111, 114, 110, 113, 1200),
    ]
    spy_down = [  # opens at 500, drifts to 495 -> "Down" by entry time
        ("09:30", 500, 500, 499, 499, 1), ("09:45", 498, 498, 495, 495, 1),
    ]
    loaders = make_loaders(
        {("AMD", day): intraday(day, bars), ("SPY", day): intraday(day, spy_down)},
        {"AMD": daily_frame()},
    )
    cfg = base_config(skip_conditions={"skip_if_spy_down": True})
    out = engine.run_backtest(cfg, get_intraday=loaders[0], get_daily=loaders[1])
    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["outcome"] == "Skip" and t["spy_direction"] == "Down"
    assert out["summary"]["skips"] == 1 and out["summary"]["total_trades"] == 0


def test_missing_intraday_reported_as_coverage_gap():
    loaders = make_loaders({}, {"AMD": daily_frame()})
    out = engine.run_backtest(base_config(), get_intraday=loaders[0], get_daily=loaders[1])
    assert out["trades"] == []
    assert out["coverage"]["missing"] == [{"ticker": "AMD", "date": "2026-06-01"}]


def test_fixed_distance_and_just_beyond_stops():
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000), ("09:45", 101, 102, 99.9, 101, 5000),
        ("10:00", 111, 114, 110, 113, 1200),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    cfg = base_config(stop_logic="fixed_distance", stop_params={"fixed_distance": 2.0})
    t = engine.run_backtest(cfg, get_intraday=loaders[0], get_daily=loaders[1])["trades"][0]
    assert t["stop_price"] == 99.0 and t["target_price"] == 105.0  # entry 101 -2 stop, +2*2 target

    cfg2 = base_config(stop_logic="just_beyond_level", stop_params={"buffer_pct": 1.0})
    t2 = engine.run_backtest(cfg2, get_intraday=loaders[0], get_daily=loaders[1])["trades"][0]
    assert t2["stop_price"] == 99.0  # level 100 * (1 - 0.01)


def test_trades_to_csv_roundtrip():
    csv = engine.trades_to_csv([
        {"date": "2026-06-01", "ticker": "AMD", "outcome": "Win", "r_result": 2.0},
    ])
    lines = csv.strip().splitlines()
    assert lines[0].startswith("date,ticker,level_type")
    assert "AMD" in lines[1] and "Win" in lines[1]


# ---------------------------------------------------------------------------
# Datastore round-trip (the service layer's loaders depend on this)
# ---------------------------------------------------------------------------
def test_intraday_db_roundtrip(fresh_db):
    idx = pd.to_datetime([f"2026-06-01 09:{m:02d}" for m in (30, 35, 40)]).tz_localize("America/New_York")
    frame = pd.DataFrame(
        {"Open": [1, 2, 3], "High": [2, 3, 4], "Low": [0.5, 1.5, 2.5],
         "Close": [1.5, 2.5, 3.5], "Volume": [100, 200, 300]},
        index=idx,
    )
    assert db.append_intraday_bars("AMD", frame, "schwab", 5) == 3
    assert db.append_intraday_bars("AMD", frame, "schwab", 5) == 0  # idempotent

    got = db.get_intraday_bars("AMD", "2026-06-01", "2026-06-01", 5)
    assert list(got["Close"]) == [1.5, 2.5, 3.5]
    assert got.index[0].strftime("%H:%M") == "09:30"  # returned in ET wall-clock
    assert db.intraday_coverage("AMD", "2026-06-01", "2026-06-01", 5) == {"2026-06-01"}
