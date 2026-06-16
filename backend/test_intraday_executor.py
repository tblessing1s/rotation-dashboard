"""Intraday Setup Executor (Phase 1 — detection) tests.

Drives the pure detection core with synthetic candles whose entry / stop /
target / position size are hand-computed, plus the signal-log round-trip in
db.py. Detection reuses the backtest engine's rules, so a breakout that fires
here is the same one the backtester would trade.
"""
import pandas as pd

import db
import intraday_executor as ix
from test_backtest import daily_frame, intraday, make_loaders


# Yesterday's bar ranges 100–110 (Wilder ATR == 10); the breakout day is the
# next trading session, so _prior_daily_bar resolves these as Y-High / Y-Low.
DAY = "2026-06-15"
DAILY = {"HOOD": daily_frame(end="2026-06-12", periods=20, high=110.0, low=100.0, close=105.0)}


def monitor_config(**over):
    """A long-breakout monitor config tuned for the synthetic session below."""
    cfg = {
        "tickers": ["HOOD"],
        "setup_conditions": {"type": "support_resistance_break", "proximity_pct": 0.0},
        # Small MA so the 4-candle session forms a full volume window; daily ATR
        # (== 10) keeps the hand-computed stop simple.
        "entry_rules": {"volume_multiplier": 2.0, "vol_avg_length": 3, "entry_timing": "candle_close"},
        "stop_logic": "atr_beyond_level",
        "stop_params": {"atr_multiplier": 2.0, "atr_period": 14, "atr_timeframe": "daily"},
        "risk_reward": 2.0,
        "fixed_risk_per_trade": 100.0,
    }
    cfg.update(over)
    c, errors = ix.validate_monitor_config(cfg)
    assert not errors, errors
    return c


def breakout_session(breakout_volume=1000):
    """ET candles: three quiet bars, then a 4th that closes above Y-High (110)
    on a volume spike. MA(3) at the breakout = (100+100+vol)/3."""
    rows = [
        ("09:30", 105, 106, 104, 105, 100),
        ("09:35", 105, 106, 104, 105, 100),
        ("09:40", 105, 106, 104, 105, 100),
        ("09:45", 109, 112, 109, 111, breakout_volume),  # close 111 > Y-High 110
    ]
    return {("HOOD", DAY): intraday(DAY, rows)}


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def test_validate_monitor_config_defaults():
    cfg, errors = ix.validate_monitor_config({})
    assert not errors
    assert cfg["tickers"] == ["CRWV", "HIMS", "CVNA", "HOOD", "TOST"]
    assert cfg["stop_logic"] == "atr_beyond_level"
    assert cfg["fixed_risk_per_trade"] == 20.0
    assert "date_range" not in cfg            # executor configs carry no date range


def test_validate_monitor_config_rejects_bad_risk():
    _, errors = ix.validate_monitor_config({"fixed_risk_per_trade": 0})
    assert any("fixed_risk_per_trade" in e for e in errors)


# ---------------------------------------------------------------------------
# Detection + order math
# ---------------------------------------------------------------------------
def test_breakout_signal_entry_stop_target_size():
    loaders = make_loaders(breakout_session(), DAILY)
    signals = ix.detect_signals(monitor_config(), get_intraday_range=loaders[0],
                               get_daily=loaders[1], on_date=DAY, mode="playback")
    assert len(signals) == 1
    sig = signals[0]
    assert sig["ticker"] == "HOOD"
    assert sig["direction"] == "Long"
    assert sig["level_type"] == "Y-High"
    assert sig["entry_price"] == 111.0          # candle close
    assert sig["stop_price"] == 90.0            # level 110 - 2*ATR(10)
    assert sig["target_price"] == 153.0         # entry + 2 * risk(21)
    assert sig["risk"] == 21.0 and sig["reward"] == 42.0
    assert sig["position_size"] == 4            # floor(100 / 21)
    assert sig["volume_ratio"] == 2.5           # 1000 / ((100+100+1000)/3)


def test_no_signal_without_volume_spike():
    loaders = make_loaders(breakout_session(breakout_volume=100), DAILY)
    signals = ix.detect_signals(monitor_config(), get_intraday_range=loaders[0],
                               get_daily=loaders[1], on_date=DAY, mode="playback")
    assert signals == []


# ---------------------------------------------------------------------------
# Live mode + as_of gating (a candle is only considered once it has closed)
# ---------------------------------------------------------------------------
def test_live_mode_waits_for_candle_close():
    loaders = make_loaders(breakout_session(), DAILY)
    cfg = monitor_config()

    # At 09:45 ET the breakout candle (09:45–09:50) has not closed yet → no signal.
    pending = ix.detect_signals(cfg, get_intraday_range=loaders[0], get_daily=loaders[1],
                               on_date=DAY, as_of="2026-06-15 09:45", mode="live")
    assert pending == []

    # Once it closes (09:50) the signal fires.
    fired = ix.detect_signals(cfg, get_intraday_range=loaders[0], get_daily=loaders[1],
                             on_date=DAY, as_of="2026-06-15 09:50", mode="live")
    assert len(fired) == 1 and fired[0]["entry_price"] == 111.0


def test_monitor_survives_null_candle_fields():
    """SQLite NULLs surface as NaN in pandas; a forming/partial candle with a
    null open/volume must not crash the monitor (regression for a prod 500)."""
    import numpy as np
    rows = [
        ("09:30", 105, 106, 104, 105, 100),
        ("09:35", 105, 106, 104, 105, 100),
        ("09:40", 105, 106, 104, 105, 100),
        ("09:45", np.nan, np.nan, np.nan, 106, np.nan),  # only close prints
    ]
    loaders = make_loaders({("HOOD", DAY): intraday(DAY, rows)}, DAILY)
    rows_out = ix.monitor_status(monitor_config(), get_intraday_range=loaders[0],
                                get_daily=loaders[1], on_date=DAY)
    assert rows_out[0]["state"] == "monitoring"
    assert rows_out[0]["last_close"] == 106.0
    assert rows_out[0]["last_volume"] == 0          # null volume -> 0, no crash
    last = rows_out[0]["candles"][-1]
    assert last["open"] == 106.0 and last["volume"] == 0   # OHLC fall back to close


def test_no_signal_without_prior_daily_levels():
    loaders = make_loaders(breakout_session(), {})   # no daily bars → no Y-High/Low
    signals = ix.detect_signals(monitor_config(), get_intraday_range=loaders[0],
                               get_daily=loaders[1], on_date=DAY, mode="playback")
    assert signals == []


# ---------------------------------------------------------------------------
# Monitor status (the "ready to monitor" dashboard view)
# ---------------------------------------------------------------------------
def test_monitor_status_reports_levels_and_volume():
    loaders = make_loaders(breakout_session(), DAILY)
    rows = ix.monitor_status(monitor_config(), get_intraday_range=loaders[0],
                            get_daily=loaders[1], on_date=DAY)
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "monitoring"
    assert row["y_high"] == 110.0 and row["y_low"] == 100.0
    assert row["last_close"] == 111.0
    assert row["volume_ratio"] == 2.5
    # Candle series is included for charting (window candles in Central time).
    assert len(row["candles"]) == 4
    last = row["candles"][-1]
    assert last["close"] == 111.0 and last["high"] == 112.0 and last["volume"] == 1000


# ---------------------------------------------------------------------------
# Signal log round-trip (dedup per closed candle)
# ---------------------------------------------------------------------------
def test_record_setup_signal_is_idempotent(fresh_db):
    sig = {"date": DAY, "ticker": "HOOD", "candle_time": "08:45", "direction": "Long",
           "level_type": "Y-High", "level": 110.0, "entry_price": 111.0,
           "stop_price": 90.0, "target_price": 153.0, "position_size": 4,
           "volume_ratio": 2.5}
    assert db.record_setup_signal(sig) is True
    assert db.record_setup_signal(sig) is False        # same candle → no duplicate
    stored = db.recent_setup_signals(DAY)
    assert len(stored) == 1
    assert stored[0]["ticker"] == "HOOD" and stored[0]["entry_price"] == 111.0


def test_execute_paper_order_logs_open_trade_and_dedupes(fresh_db):
    sig = {"date": DAY, "ticker": "HOOD", "candle_time": "08:45", "direction": "Long",
           "level_type": "Y-High", "entry_price": 111.0, "stop_price": 90.0,
           "target_price": 153.0, "position_size": 4, "volume_ratio": 2.5}

    first = ix.execute_paper_order(sig)
    second = ix.execute_paper_order(sig)

    assert first["ok"] is True and first["mode"] == "PAPER"
    assert second["ok"] is True
    assert first["trade"]["id"] == second["trade"]["id"]
    assert first["trade"]["account_type"] == "PAPER"
    assert first["trade"]["outcome"] == "OPEN"
    assert first["trade"]["order_id"] == "PAPER-2026-06-15-HOOD-08:45"

    trades = db.list_intraday_trades(DAY)
    assert len(trades) == 1
    assert trades[0]["ticker"] == "HOOD" and trades[0]["direction"] == "LONG"


def test_execute_paper_order_rejects_invalid_signal(fresh_db):
    out = ix.execute_paper_order({"ticker": "HOOD", "direction": "Long"})
    assert out["ok"] is False
    assert any("entry_price" in e for e in out["errors"])


def test_list_intraday_trades_filters_by_ticker(fresh_db):
    common = {"date": DAY, "level_type": "Y-High", "stop_price": 90.0,
              "target_price": 153.0, "position_size": 4}
    db.record_intraday_trade({**common, "ticker": "HOOD", "direction": "LONG",
                              "entry_price": 111.0, "entry_time": "08:45", "outcome": "OPEN"})
    db.record_intraday_trade({**common, "ticker": "CVNA", "direction": "LONG",
                              "entry_price": 250.0, "entry_time": "08:50", "outcome": "WIN"})

    assert {t["ticker"] for t in db.list_intraday_trades(DAY)} == {"HOOD", "CVNA"}
    only = db.list_intraday_trades(DAY, ticker="cvna")          # case-insensitive
    assert len(only) == 1 and only[0]["ticker"] == "CVNA"
    wins = db.list_intraday_trades(DAY, status="WIN")
    assert len(wins) == 1 and wins[0]["ticker"] == "CVNA"
