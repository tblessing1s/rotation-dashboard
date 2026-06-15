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


def make_loaders(intraday_map, daily_map, fine_map=None):
    def get_intraday_range(sym, start, end, interval=5):
        src = intraday_map if interval >= 5 else (fine_map or {})
        frames = [df for (s, d), df in src.items() if s == sym and start <= d <= end]
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
        "time_window": {"start_time": "08:30", "end_time": "10:00"},
        "risk_reward": 2,
        "stop_logic": "atr_divided_by_2",
        # Daily ATR (== 10 from daily_frame) keeps the hand-computed stops; small
        # MA so the short worked-example sessions form a full volume window.
        "stop_params": {"atr_period": 14, "atr_timeframe": "daily"},
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


def test_time_window_is_interpreted_in_central_time():
    day = "2026-06-01"  # CDT, so 08:30 CT == 09:30 ET.
    bars = [
        ("09:25", 101, 102, 99.9, 101, 5000),  # before the 08:30 CT window
        ("09:30", 105, 106, 104, 105, 1000),
        ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 101, 102, 99.9, 101, 5000),
        ("09:45", 111, 114, 110, 113, 1200),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})

    out = run(base_config(), loaders)

    assert len(out["trades"]) == 1
    assert out["trades"][0]["entry_time"] == "08:40"


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
    assert (t["risk_amount"], t["reward_amount"]) == (6.0, 12.0)
    assert t["outcome"] == "Win" and t["exit_price"] == 113.0 and t["r_result"] == 2.0
    assert (t["entry_time"], t["exit_time"]) == ("08:45", "09:00")
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
    assert (outcomes[loss_day]["risk_amount"], outcomes[loss_day]["reward_amount"]) == (6.0, 12.0)
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


def test_atr_timeframe_intraday_is_tighter_than_daily():
    # Same breakout, two ATR timeframes. Daily ATR = 10 -> stop 110 - 5 = 105.
    # Intraday ATR over gentle 5-minute bars is far smaller, so the stop sits
    # much closer to the broken level (proportional to the trade's timeframe).
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 105.5, 104.5, 105, 1000), ("09:35", 105, 105.5, 104.5, 105, 1000),
        ("09:40", 105, 105.5, 104.5, 105, 1000),
        ("09:45", 109, 111, 108.8, 110.5, 5000),  # closes 110.5 > Y-High 110 -> Long
        ("10:00", 111, 112, 110, 111.5, 1200),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    setup = {"type": "support_resistance_break"}
    cfg_i = base_config(setup_conditions=setup, stop_params={"atr_period": 14, "atr_timeframe": "intraday"})
    cfg_d = base_config(setup_conditions=setup, stop_params={"atr_period": 14, "atr_timeframe": "daily"})
    ti = run(cfg_i, loaders)["trades"][0]
    td = run(cfg_d, loaders)["trades"][0]

    assert td["stop_price"] == 105.0                       # daily ATR 10 -> level - 5
    assert ti["stop_price"] > td["stop_price"]             # intraday ATR is tighter
    assert abs(ti["entry_price"] - ti["stop_price"]) < abs(td["entry_price"] - td["stop_price"])


def test_fixed_distance_session_end_uses_binary_r_multiple():
    # With fixed-distance stops, a trade that has not hit the target by the end
    # of the test window should still report table R as one full risk unit lost
    # instead of a fractional mark-to-close value.
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000),
        ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 101, 102, 99.9, 101, 5000),
        ("10:00", 101, 101.2, 100.4, 100.5, 1200),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    cfg = base_config(stop_logic="fixed_distance", stop_params={"fixed_distance": 1})
    out = run(cfg, loaders)

    t = out["trades"][0]
    assert t["outcome"] == "Loss"
    assert t["exit_price"] == 100.5
    assert t["r_result"] == -1.0
    assert out["summary"]["avg_loss_r"] == -1.0


def test_fixed_distance_target_hit_uses_configured_reward_multiple():
    day = "2026-06-01"
    bars = [
        ("09:30", 105, 106, 104, 105, 1000),
        ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 101, 102, 99.9, 101, 5000),
        ("10:00", 102, 104, 101.5, 103, 1200),
    ]
    loaders = make_loaders({("AMD", day): intraday(day, bars)}, {"AMD": daily_frame()})
    cfg = base_config(stop_logic="fixed_distance", stop_params={"fixed_distance": 1})
    out = run(cfg, loaders)

    t = out["trades"][0]
    assert t["outcome"] == "Win"
    assert t["r_result"] == 2.0
    assert out["summary"]["avg_win_r"] == 2.0


def test_ambiguous_5m_bar_is_refined_with_1m_data():
    # Breakout Long: entry 111, stop 105, target 123. A later 5-minute bar's
    # range straddles BOTH stop and target, so order-of-hit is ambiguous.
    day = "2026-06-01"
    five = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 109, 111, 108, 111, 5000),   # breakout Long (close 111 > Y-High 110)
        ("10:00", 112, 124, 104, 110, 1200),   # straddles stop 105 AND target 123
    ]
    one = [  # 1-minute path inside 10:00: target prints before the stop
        ("10:00", 121, 124, 120, 123, 300),    # tags target 123 first
        ("10:01", 123, 123, 104, 105, 300),    # only later tags stop 105
        ("10:02", 105, 106, 104, 105, 300), ("10:03", 105, 106, 104, 105, 300),
        ("10:04", 105, 106, 104, 105, 300),
    ]
    cfg = base_config(setup_conditions={"type": "support_resistance_break"})

    # No 1-minute data -> we can't tell which hit first, so flag for review
    # (don't silently assume a loss).
    out5 = run(cfg, make_loaders({("AMD", day): intraday(day, five)}, {"AMD": daily_frame()}))
    assert out5["trades"][0]["outcome"] == "Unresolved"
    assert out5["diagnostics"]["ambiguous_bars"] == 1 and out5["diagnostics"]["refined_bars"] == 0
    assert out5["summary"]["unresolved"] == 1 and out5["summary"]["total_trades"] == 0

    # With 1-minute data showing the target first -> Win.
    out1 = run(cfg, make_loaders({("AMD", day): intraday(day, five)}, {"AMD": daily_frame()},
                                 fine_map={("AMD", day): intraday(day, one)}))
    t = out1["trades"][0]
    assert t["outcome"] == "Win" and t["exit_price"] == 123.0 and "1m" in t["notes"]
    assert out1["diagnostics"]["refined_bars"] == 1


def test_unresolved_when_1m_ambiguous_then_manual_override():
    # Even the 1-minute bar that does the damage has BOTH stop and target inside
    # it -> outcome "Unresolved" (needs manual review). A saved manual resolution
    # then settles it on the next run.
    day = "2026-06-01"
    five = [
        ("09:30", 105, 106, 104, 105, 1000), ("09:35", 105, 106, 104, 105, 1000),
        ("09:40", 105, 106, 104, 105, 1000),
        ("09:45", 109, 111, 108, 111, 5000),   # breakout Long: entry 111, stop 105, target 123
        ("10:00", 112, 124, 104, 110, 1200),   # 5m straddles both
    ]
    one = [  # first 1m bar to touch anything hits BOTH 105 and 123
        ("10:00", 110, 124, 104, 108, 300), ("10:01", 108, 110, 106, 109, 300),
        ("10:02", 109, 110, 107, 108, 300), ("10:03", 108, 110, 107, 109, 300),
        ("10:04", 109, 110, 107, 108, 300),
    ]
    cfg = base_config(setup_conditions={"type": "support_resistance_break"})
    gir, gd = make_loaders({("AMD", day): intraday(day, five)}, {"AMD": daily_frame()},
                           fine_map={("AMD", day): intraday(day, one)})

    out = engine.run_backtest(cfg, get_intraday_range=gir, get_daily=gd)
    t = out["trades"][0]
    assert t["outcome"] == "Unresolved" and t["exit_price"] is None and t["r_result"] is None
    assert out["summary"]["unresolved"] == 1 and out["summary"]["total_trades"] == 0

    out2 = engine.run_backtest(cfg, get_intraday_range=gir, get_daily=gd,
                               manual_resolutions={f"AMD|{day}|09:45": "Win"})
    t2 = out2["trades"][0]
    assert t2["outcome"] == "Win" and t2["exit_price"] == 123.0 and "manual" in t2["notes"]

    out3 = engine.run_backtest(cfg, get_intraday_range=gir, get_daily=gd,
                               manual_resolutions={f"AMD|{day}|09:45": "Skip"})
    t3 = out3["trades"][0]
    assert t3["outcome"] == "Skip" and t3["exit_price"] is None and t3["r_result"] == 0.0
    assert out3["summary"]["skips"] == 1 and out3["summary"]["unresolved"] == 0


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


def test_intraday_roundtrip_resolves_duplicate_epochs(fresh_db):
    """A re-fetched candle whose values changed (e.g. a still-forming bar whose
    volume grew) lands as a second row for the same epoch; reads must resolve to
    the newest without crashing. Regression: get_intraday_bars omitted `id`,
    which _beats() reads, raising sqlite3 'No item with that key'."""
    idx = pd.to_datetime(["2026-06-01 09:30"]).tz_localize("America/New_York")
    first = pd.DataFrame({"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [100.0]}, index=idx)
    grown = pd.DataFrame({"Open": [1.0], "High": [3.0], "Low": [0.5], "Close": [2.5], "Volume": [250.0]}, index=idx)
    assert db.append_intraday_bars("AMD", first, "schwab", 5) == 1
    assert db.append_intraday_bars("AMD", grown, "schwab", 5) == 1   # different values -> new row

    got = db.get_intraday_bars("AMD", "2026-06-01", "2026-06-01", 5)
    assert len(got) == 1                       # one canonical candle per epoch
    assert list(got["Close"]) == [2.5]         # newest fetch wins
    assert list(got["Volume"]) == [250.0]


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
