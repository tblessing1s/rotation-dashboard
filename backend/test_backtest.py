"""Backtest engine tests.

Two halves:
  1. the pure engine (backtest.py) driven by synthetic candles with
     hand-computed expected entry/stop/target/outcome, and
  2. the intraday datastore round-trip (db.py) the service layer relies on.
"""
import numpy as np
import pandas as pd

import backtest as engine
import backtest_service
import db
from providers.base import Provider


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
    def get_intraday_range(sym, start, end, interval=5):
        frames = [df for (s, d), df in intraday_map.items() if s == sym and start <= d <= end]
        return pd.concat(frames).sort_index() if frames else None

    def get_daily(sym):
        return daily_map.get(sym)

    return get_intraday_range, get_daily


def run(cfg, loaders):
    return engine.run_backtest(cfg, get_intraday_range=loaders[0], get_daily=loaders[1])


def base_config(**over):
    cfg = {
        "tickers": ["AMD"],
        "date_range": {"start": "2026-06-01", "end": "2026-06-01"},
        "time_window": {"start_time": "09:30", "end_time": "11:00"},
        "risk_reward": 2,
        "stop_logic": "atr_divided_by_2",
        # Small MA so the short worked-example sessions form a full window.
        "entry_rules": {"volume_multiplier": 2, "vol_avg_length": 3, "entry_timing": "candle_close"},
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
    out = run(base_config(), loaders)

    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["direction"] == "Long" and t["level_type"] == "Y-Low"
    assert t["volume_spike"] is True
    # Entry-candle volume vs the TOS-style volume MA (length 3, includes the
    # current bar): (1000 + 1000 + 5000) / 3 = 2333, ratio 5000/2333 = 2.14.
    assert t["entry_volume"] == 5000 and t["avg_volume"] == 2333
    assert t["volume_ratio"] == 2.14
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
    out = run(cfg, loaders)

    outcomes = {t["date"]: t for t in out["trades"]}
    assert outcomes[loss_day]["direction"] == "Short"
    assert (outcomes[loss_day]["entry_price"], outcomes[loss_day]["stop_price"]) == (109.0, 115.0)
    assert outcomes[loss_day]["outcome"] == "Loss" and outcomes[loss_day]["r_result"] == -1.0

    s = out["summary"]
    assert (s["total_trades"], s["wins"], s["losses"]) == (2, 1, 1)
    assert s["win_rate_percent"] == 50.0
    assert s["avg_win_r"] == 2.0 and s["avg_loss_r"] == -1.0
    assert s["expectancy_per_trade"] == 0.5


def test_breakout_direction_high_is_long_low_is_short():
    # Breakout setup: close ABOVE Y-High -> Long; close BELOW Y-Low -> Short
    # (the opposite mapping from the bounce/fade setup). Y-High=110, Y-Low=100.
    up_day, down_day = "2026-06-01", "2026-06-02"
    up_bars = [  # opens inside the range, then closes above Y-High -> Long
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 108, 112, 108, 111, 5000),   # closes 111 > 110 -> breakout Long
        ("10:00", 113, 118, 112, 117, 1200),   # runs up
    ]
    down_bars = [  # opens inside the range, then closes below Y-Low -> Short
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 101, 102, 98, 99, 5000),     # closes 99 < 100 -> breakdown Short
        ("10:00", 97, 98, 92, 93, 1200),       # runs down
    ]
    loaders = make_loaders(
        {("AMD", up_day): intraday(up_day, up_bars),
         ("AMD", down_day): intraday(down_day, down_bars)},
        {"AMD": daily_frame()},
    )
    cfg = base_config(setup_conditions={"type": "support_resistance_break"},
                      date_range={"start": up_day, "end": down_day})
    out = run(cfg, loaders)
    by_date = {t["date"]: t for t in out["trades"]}
    assert by_date[up_day]["level_type"] == "Y-High" and by_date[up_day]["direction"] == "Long"
    assert by_date[down_day]["level_type"] == "Y-Low" and by_date[down_day]["direction"] == "Short"


def test_breakout_gap_open_is_no_trade_until_back_in_range():
    # Gaps above Y-High (110) at the open -> no trade, even though it keeps
    # closing above 110, because price never returns into yesterday's range.
    day = "2026-06-01"
    bars = [
        ("09:30", 115, 116, 114, 115, 1000), ("09:35", 115, 116, 114, 115, 1000),
        ("09:40", 115, 116, 114, 115, 1000), ("09:45", 116, 118, 114, 117, 5000),
        ("10:00", 118, 120, 116, 119, 5000),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    cfg = base_config(setup_conditions={"type": "support_resistance_break"})
    out = run(cfg, loaders)
    assert out["trades"] == []


def test_proximity_zero_is_a_touch_not_exact_equality():
    # A trader setting proximity_pct=0 means "wick must touch the level," not
    # "match it to the penny." Candle wicks exactly to Y-Low (100) and closes above.
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 101, 102, 100.0, 101, 5000),  # low == Y-Low exactly, closes above
        ("10:00", 111, 114, 110, 113, 1200),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    cfg = base_config(setup_conditions={"type": "support_resistance_bounce",
                                        "use_yesterday_levels": True, "proximity_pct": 0})
    out = run(cfg, loaders)
    assert len(out["trades"]) == 1 and out["trades"][0]["direction"] == "Long"


def test_diagnostics_explain_a_zero_trade_run():
    # Levels are touched but the volume multiplier is never met -> 0 trades, and
    # diagnostics show touches > 0, spikes == 0 so the cause is visible.
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 101, 102, 99.9, 101, 1000),   # touches Y-Low but no volume spike
        ("09:50", 101, 102, 99.9, 101, 1000),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    out = run(base_config(), loaders)
    assert out["trades"] == []
    diag = out["diagnostics"]
    assert diag["candles_evaluated"] >= 1
    assert diag["level_touches"] >= 1 and diag["volume_spikes"] == 0
    assert diag["setups_detected"] == 0


def test_memorial_day_holiday_is_not_a_session():
    # 2026-05-25 is Memorial Day (NYSE closed) — it must not appear as a session.
    assert "2026-05-25" not in engine._session_dates("2026-05-22", "2026-05-27")
    assert "2026-05-22" in engine._session_dates("2026-05-22", "2026-05-27")  # Friday


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
    out = run(cfg, loaders)
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
    out = run(cfg, loaders)
    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["outcome"] == "Skip" and t["spy_direction"] == "Down"
    assert out["summary"]["skips"] == 1 and out["summary"]["total_trades"] == 0


def test_missing_intraday_reported_as_coverage_gap():
    loaders = make_loaders({}, {"AMD": daily_frame()})
    out = run(base_config(), loaders)
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
    t = run(cfg, loaders)["trades"][0]
    assert t["stop_price"] == 99.0 and t["target_price"] == 105.0  # entry 101 -2 stop, +2*2 target

    cfg2 = base_config(stop_logic="just_beyond_level", stop_params={"buffer_pct": 1.0})
    t2 = run(cfg2, loaders)["trades"][0]
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


# ---------------------------------------------------------------------------
# Service: backfill pulls *daily* too, and coverage flags missing daily history
# ---------------------------------------------------------------------------
class _FakeProvider(Provider):
    """Stands in for Schwab: serves both daily and intraday for any symbol."""
    name = "schwab"

    def get_daily_bars(self, symbol, start):
        return daily_frame(end="2026-06-02", periods=30)

    def get_intraday_bars(self, symbol, start, end, interval_min=5, extended_hours=False):
        rows = [("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000)]
        idx = pd.to_datetime([f"{start} {r[0]}" for r in rows]).tz_localize("America/New_York")
        return pd.DataFrame(
            {"Open": [r[1] for r in rows], "High": [r[2] for r in rows], "Low": [r[3] for r in rows],
             "Close": [r[4] for r in rows], "Volume": [r[5] for r in rows]}, index=idx,
        )


def test_backfill_pulls_daily_and_intraday(fresh_db, monkeypatch):
    monkeypatch.setattr(backtest_service.time, "sleep", lambda s: None)
    monkeypatch.setattr(backtest_service, "build_chain", lambda: [_FakeProvider()])

    # A backtest ticker (CRWV) with no daily history -> coverage flags it.
    config = backtest_service._apply_default_sector_map(
        {"tickers": ["CRWV"], "date_range": {"start": "2026-06-01", "end": "2026-06-01"}})
    assert "CRWV" in backtest_service.coverage_report(config)["missingDaily"]

    out = backtest_service.backfill(["CRWV"], "2026-06-01", "2026-06-01", 5)
    assert out["ok"] and out["dailyWritten"] > 0 and out["rowsWritten"] > 0
    assert out["perSymbol"]["CRWV"]["daily"]["rowsWritten"] > 0

    # Daily history now present -> no longer flagged.
    assert backtest_service.coverage_report(config)["missingDaily"] == []
    assert db.get_bars("CRWV") is not None
