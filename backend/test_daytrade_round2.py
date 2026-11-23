"""Day-trade sleeve — Round 2 rule updates (TRAVIS_EXTENSION).

Change 1: no new entries late in the window (last_entry_time,
          min_minutes_to_cutoff), rejections logged with a reason code.
Change 2: every open position is flattened at the cutoff — including one
          entered on the cutoff bar by an earlier run — plus an
          end-of-session ``eod_sweep`` safety net.
Change 3: fixed-dollar-risk sizing, a reported notional safety cap, and a
          minimum share count.
Change 4: stop/target/risk/minutes-to-cutoff on every trade-log row, and a
          separate rejected-setups log.

Offline: hand-built bars and a tmp_path store. ATR14 = 4.0 with the default
divisor 4.0 makes risk-per-share exactly $1.00, so a $50 risk is 50 shares.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
from daytrade import report, signals, store, trial

DAY = "2026-11-24"
ET = ZoneInfo("America/New_York")
OFF = "-05:00"  # EST
ACCOUNT = "primary"


def _at(hm: str) -> str:
    return f"{DAY}T{hm}:00{OFF}"


def _now(hm: str, day: int = 24) -> datetime:
    h, m = (int(x) for x in hm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=ET)


def _bar(hm, o, h, l, c, v, symbol="ABC"):
    return {"symbol": symbol, "date": DAY, "datetime": _at(hm),
            "open": o, "high": h, "low": l, "close": c, "volume": v}


def _screen(symbol="ABC"):
    store.save_screen({"schema_version": 1, "date": DAY, "computed_at": "x", "screened": [],
                        "picks": [{"symbol": symbol, "prior_day_high": 100,
                                   "prior_day_low": 90, "atr14": 4.0}]})


def _setup_at(setup_hm, trigger_hm=None, prior_hm="09:30"):
    """A quiet bar, a setup candle (high 101.5 / low 100.5 on 2x volume),
    and optionally a bar breaking the setup high (entry at 101.5)."""
    bars = [_bar(prior_hm, 95, 96, 94, 95, 100_000),
            _bar(setup_hm, 100.6, 101.5, 100.5, 101, 200_000)]
    if trigger_hm:
        bars.append(_bar(trigger_hm, 101.6, 102, 101.4, 101.8, 50_000))
    return bars


def _run(now="09:50", **kw):
    return signals.run_day(DAY, ACCOUNT, now=_now(now), **kw)["events"]


def _types(events):
    return [e["event"] for e in events]


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    # Pin the Round 2 defaults so a later config change doesn't silently
    # change what these tests prove.
    for name, value in {"DAYTRADE_SIZING_MODE": "fixed_usd", "DAYTRADE_RISK_PER_TRADE_USD": 50.0,
                        "DAYTRADE_MAX_NOTIONAL_PER_TRADE_USD": 10_000.0,
                        "DAYTRADE_MIN_SHARES": 10, "DAYTRADE_LAST_ENTRY_ET": "10:30",
                        "DAYTRADE_MIN_MINUTES_TO_CUTOFF": 25, "DAYTRADE_WINDOW_END_ET": "11:00",
                        "DAYTRADE_RULES_ROUND": 2}.items():
        monkeypatch.setattr(config, name, value)
    return tmp_path


# ===========================================================================
# Change 1 — entry window
# ===========================================================================
def test_setup_at_the_last_entry_time_is_not_armed(tmp_store):
    _screen()
    store.append_bars(DAY, _setup_at("10:30", prior_hm="10:25"))

    events = _run(now="10:35")

    assert _types(events) == ["setup_skipped"]
    assert events[0]["reason_code"] == "entry_window_closed"
    rejected = store.load_rejected(DAY, ACCOUNT)
    assert [(r["symbol"], r["direction"], r["reason"], r["stage"]) for r in rejected] == [
        ("ABC", "long", "entry_window_closed", "setup")]
    assert rejected[0]["at"] == _at("10:30")


def test_break_at_or_after_the_last_entry_time_is_rejected(tmp_store):
    _screen()
    store.append_bars(DAY, _setup_at("10:25", "10:30", prior_hm="10:20"))

    events = _run(now="10:35")

    assert _types(events) == ["setup", "entry_skipped"]
    assert events[1]["reason_code"] == "entry_window_closed"
    assert store.load_trades(DAY, ACCOUNT) == {}


def test_entry_too_close_to_the_cutoff_is_insufficient_time(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_LAST_ENTRY_ET", "11:00")  # isolate the minutes rule
    _screen()
    store.append_bars(DAY, _setup_at("10:30", "10:40", prior_hm="10:25"))  # 20 min left < 25

    events = _run(now="10:45")

    assert _types(events) == ["setup", "entry_skipped"]
    assert events[1]["reason_code"] == "insufficient_time"
    assert store.load_rejected(DAY, ACCOUNT)[-1]["reason"] == "insufficient_time"


def test_entry_with_enough_time_left_goes_through(tmp_store):
    _screen()
    store.append_bars(DAY, _setup_at("10:20", "10:25", prior_hm="10:15"))  # 35 min left

    events = _run(now="10:30")

    assert _types(events) == ["setup", "entry"]
    assert events[1]["minutes_to_cutoff"] == 35


# ===========================================================================
# Change 2 — nothing left open at the cutoff
# ===========================================================================
def test_setup_triggering_on_the_cutoff_bar_is_rejected(tmp_store):
    _screen()
    store.append_bars(DAY, _setup_at("10:50", "10:55", prior_hm="10:45"))

    events = _run(now="11:00")

    assert "entry" not in _types(events)
    assert store.load_trades(DAY, ACCOUNT) == {}


def test_trade_entered_on_the_cutoff_bar_by_an_earlier_run_is_closed_at_cutoff(tmp_store, monkeypatch):
    """The Round 1 bug (EWZ/GDX/NKE): an in-window run journals an entry on
    the 10:55 bar; the 11:00 finalize run rehydrates it with no bar of its
    own and used to leave it open forever."""
    monkeypatch.setattr(config, "DAYTRADE_LAST_ENTRY_ET", "23:59")  # recreate pre-Round-2 rules
    monkeypatch.setattr(config, "DAYTRADE_MIN_MINUTES_TO_CUTOFF", 0)
    _screen()
    store.append_bars(DAY, _setup_at("10:50", "10:55", prior_hm="10:45"))
    assert _types(_run(now="10:56")) == ["setup", "entry"]

    events = _run(now="11:00")

    assert _types(events) == ["setup", "entry", "time_cutoff"]
    assert events[-1]["r"] == pytest.approx(0.3)  # close 101.8 vs entry 101.5, 1R = $1
    trade = next(iter(store.load_trades(DAY, ACCOUNT).values()))
    assert trade["status"] == "closed"
    assert [x["kind"] for x in trade["exits"]] == ["time_cutoff"]


def test_eod_sweep_closes_a_trade_log_position_the_engine_lost(tmp_store, caplog):
    _screen()
    store.append_bars(DAY, [_bar("10:55", 101, 101.2, 100.8, 101.0, 10_000)])
    store.save_trades(DAY, ACCOUNT, {"ghost": {
        "trade_id": "ghost", "symbol": "ABC", "direction": "long", "date": DAY,
        "entry": {"price": 100.0, "size": 20, "at": _at("10:00")}, "exits": [],
        "status": "open", "realized_r": None, "realized_pnl": 0.0, "closed_at": None,
        "risk_per_share": 2.0}})

    with caplog.at_level(logging.ERROR, logger="cfm.daytrade"):
        events = _run(now="11:00")

    assert _types(events) == ["eod_sweep"]
    assert events[0]["r"] == pytest.approx(0.5)  # (101 - 100) / 2
    trade = store.load_trades(DAY, ACCOUNT)["ghost"]
    assert trade["status"] == "closed" and trade["exits"][0]["kind"] == "eod_sweep"
    assert trade["realized_pnl"] == pytest.approx(20.0)
    assert any("EOD sweep" in r.message for r in caplog.records)


def test_sweep_day_cleans_up_a_past_day_without_replaying_bars(tmp_store):
    _screen()
    # A setup+break sits in the bars, but sweep_day must NOT turn it into a trade.
    store.append_bars(DAY, _setup_at("09:35", "09:40"))
    store.save_trades(DAY, ACCOUNT, {"stuck": {
        "trade_id": "stuck", "symbol": "ABC", "direction": "short", "date": DAY,
        "entry": {"price": 102.0, "size": 10, "at": _at("10:55")}, "exits": [],
        "status": "open", "realized_r": None, "realized_pnl": 0.0, "closed_at": None}})
    store.append_signals(DAY, ACCOUNT, [{"event": "entry", "symbol": "ABC", "at": _at("10:55"),
                                         "id": "x", "trade_id": "stuck", "entry": 102.0,
                                         "stop": 103.0, "direction": "short"}])

    assert signals.open_trade_days(ACCOUNT, "2026-11-25") == [DAY]
    events = signals.sweep_day(DAY, ACCOUNT, now=_now("11:00", day=25))

    assert _types(events) == ["eod_sweep"]
    assert events[0]["r"] == pytest.approx(0.2)  # short: (102 - 101.8) / 1.0
    trades = store.load_trades(DAY, ACCOUNT)
    assert list(trades) == ["stuck"] and trades["stuck"]["status"] == "closed"
    assert signals.open_trade_days(ACCOUNT, "2026-11-25") == []
    assert signals.sweep_day(DAY, ACCOUNT, now=_now("11:00", day=25)) == []  # idempotent


# ===========================================================================
# Change 3 — fixed dollar risk
# ===========================================================================
def test_size_is_fixed_dollar_risk_over_stop_distance(tmp_store):
    _screen()
    store.append_bars(DAY, _setup_at("09:35", "09:40"))

    events = _run(account_equity=0.0)  # budget no longer drives size in fixed_usd mode

    entry = events[1]
    assert entry["event"] == "entry"
    assert entry["size"] == 50  # floor($50 / $1.00)
    assert entry["risk_usd"] == 50.0
    assert "size_capped" not in _types(events)


def test_notional_cap_binding_is_logged_not_silent(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_MAX_NOTIONAL_PER_TRADE_USD", 2_000.0)
    _screen()
    store.append_bars(DAY, _setup_at("09:35", "09:40"))

    events = _run()

    assert _types(events) == ["setup", "entry", "size_capped"]
    capped = events[2]
    assert capped["size"] == 19 and capped["uncapped_size"] == 50  # floor(2000 / 101.5)
    assert capped["risk_usd"] == 19.0 and capped["risk_usd_target"] == 50.0


def test_below_min_shares_is_skipped_as_size_too_small(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_MAX_NOTIONAL_PER_TRADE_USD", 500.0)  # 4 shares
    _screen()
    store.append_bars(DAY, _setup_at("09:35", "09:40"))

    events = _run()

    assert _types(events) == ["setup", "entry_skipped"]
    assert events[1]["reason_code"] == "size_too_small"
    assert store.load_rejected(DAY, ACCOUNT)[0]["reason"] == "size_too_small"
    assert store.load_trades(DAY, ACCOUNT) == {}


# ===========================================================================
# Change 4 — trade-log fields
# ===========================================================================
def test_trade_log_row_carries_risk_geometry(tmp_store):
    _screen()
    store.append_bars(DAY, _setup_at("09:35", "09:40"))

    _run()

    trade = next(iter(store.load_trades(DAY, ACCOUNT).values()))
    assert trade["stop_price"] == 100.5
    assert trade["target_price"] == 102.5
    assert trade["risk_usd"] == 50.0
    assert trade["minutes_to_cutoff_at_entry"] == 80
    assert trade["round"] == 2
    row = report.trade_rows(ACCOUNT)[0]
    assert (row["stop_price"], row["target_price"], row["risk_usd"],
            row["minutes_to_cutoff_at_entry"], row["round"]) == (100.5, 102.5, 50.0, 80, 2)


def test_trial_counts_only_the_current_rule_round(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 2)
    closed = {"status": "closed", "realized_r": 1.0, "realized_pnl": 10.0, "closed_at": "a"}
    store.save_trades("2026-10-01", ACCOUNT, {
        "r1a": dict(closed, trade_id="r1a"),                    # unstamped -> Round 1
        "r1b": dict(closed, trade_id="r1b", round=1),
        "r2a": dict(closed, trade_id="r2a", round=2),
    })

    status = trial.trial_status(ACCOUNT)

    assert status["round"] == 2
    assert status["completed_trades"] == 1
    assert status["status"] == "running"
