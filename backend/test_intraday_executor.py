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


# ---------------------------------------------------------------------------
# Auto-close — resolve open brackets against stored candles
# ---------------------------------------------------------------------------
# The entry candle below is ET 09:45 (DAY is in June -> EDT), which is 08:45 in
# Central time, so the paper trade's entry_time matches the bracket's candle.
OPEN_SIG = {"date": DAY, "ticker": "HOOD", "candle_time": "08:45", "direction": "Long",
            "level_type": "Y-High", "entry_price": 111.0, "stop_price": 90.0,
            "target_price": 153.0, "position_size": 4, "volume_ratio": 2.5}


def _store_session(rows):
    """Persist an ET intraday session for HOOD into the (fresh) datastore."""
    db.append_intraday_bars("HOOD", intraday(DAY, rows), "test", interval_min=5)


def test_auto_close_fills_target(fresh_db):
    ix.execute_paper_order(OPEN_SIG)
    _store_session([
        ("09:45", 109, 112, 109, 111, 1000),   # entry candle
        ("09:50", 112, 155, 150, 152, 800),    # high 155 reaches target 153
    ])
    out = ix.auto_close_open_trades(DAY)
    assert len(out["closed"]) == 1
    trade = out["closed"][0]
    assert trade["outcome"] == "WIN"
    assert trade["exit_price"] == 153.0        # filled at the target level
    assert trade["r_result"] == 2.0            # (153-111)/(111-90)
    assert trade["exit_time"] == "08:50"
    # Idempotent: a second pass finds nothing open to close.
    assert ix.auto_close_open_trades(DAY)["closed"] == []


def test_auto_close_fills_stop(fresh_db):
    ix.execute_paper_order(OPEN_SIG)
    _store_session([
        ("09:45", 109, 112, 109, 111, 1000),
        ("09:50", 110, 112, 85, 95, 800),      # low 85 reaches stop 90
    ])
    out = ix.auto_close_open_trades(DAY)
    assert len(out["closed"]) == 1
    assert out["closed"][0]["outcome"] == "LOSS"
    assert out["closed"][0]["exit_price"] == 90.0
    assert out["closed"][0]["r_result"] == -1.0


def test_auto_close_gap_through_target_fills_at_open(fresh_db):
    """A candle that opens above the target gapped through it — the realistic
    fill is the open, not the target level, so the recorded R reflects the gap."""
    ix.execute_paper_order(OPEN_SIG)
    _store_session([
        ("09:45", 109, 112, 109, 111, 1000),
        ("09:50", 160, 165, 158, 162, 800),    # opens 160, already past target 153
    ])
    out = ix.auto_close_open_trades(DAY)
    assert len(out["closed"]) == 1
    assert out["closed"][0]["outcome"] == "WIN"
    assert out["closed"][0]["exit_price"] == 160.0          # gap fill at the open
    assert out["closed"][0]["r_result"] == round((160 - 111) / 21, 2)


def test_auto_close_gap_through_stop_fills_at_open(fresh_db):
    """A gap down through the stop fills at the open (adverse slippage)."""
    ix.execute_paper_order(OPEN_SIG)
    _store_session([
        ("09:45", 109, 112, 109, 111, 1000),
        ("09:50", 80, 85, 78, 82, 800),        # opens 80, already below stop 90
    ])
    out = ix.auto_close_open_trades(DAY)
    assert len(out["closed"]) == 1
    assert out["closed"][0]["outcome"] == "LOSS"
    assert out["closed"][0]["exit_price"] == 80.0
    assert out["closed"][0]["r_result"] == round((80 - 111) / 21, 2)


def test_auto_close_leaves_open_when_neither_hit(fresh_db):
    ix.execute_paper_order(OPEN_SIG)
    _store_session([
        ("09:45", 109, 112, 109, 111, 1000),
        ("09:50", 112, 120, 100, 115, 800),    # stays between stop 90 and target 153
    ])
    out = ix.auto_close_open_trades(DAY)
    assert out["closed"] == []
    assert db.list_intraday_trades(DAY, status="OPEN")


def test_auto_close_leaves_open_when_candle_straddles_both(fresh_db):
    """One candle reaching both stop and target without a gap is ambiguous at
    5m resolution, so the trade is left open for manual review."""
    ix.execute_paper_order(OPEN_SIG)
    _store_session([
        ("09:45", 109, 112, 109, 111, 1000),
        ("09:50", 111, 155, 85, 120, 800),     # opens between; range covers both
    ])
    out = ix.auto_close_open_trades(DAY)
    assert out["closed"] == []
    assert db.list_intraday_trades(DAY, status="OPEN")
