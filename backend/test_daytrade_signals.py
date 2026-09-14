"""Day-trade signal engine — Phase 2 (strategy rules 3-8) and its Phase 3
execution-adapter wiring, TRAVIS_EXTENSION.

Offline throughout: hand-built bar sequences and a tmp_path store, no network.
ATR14 is fixed at 4.0 with the default DAYTRADE_STOP_ATR_DIVISOR=4.0 so
risk-per-share is a clean 1.0 and every price in a test IS its R-multiple
distance from entry — easy to eyeball.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
from daytrade import adapters, signals, store

DAY = "2026-09-14"
ET = ZoneInfo("America/New_York")
ET_OFFSET = "-04:00"  # EDT, matches September


def _at(hm: str) -> str:
    return f"{DAY}T{hm}:00{ET_OFFSET}"


def _now(hm: str) -> datetime:
    h, m = (int(x) for x in hm.split(":"))
    return datetime(2026, 9, 14, h, m, tzinfo=ET)


def _bar(symbol: str, hm: str, o: float, h: float, l: float, c: float, v: float) -> dict:
    return {"symbol": symbol, "date": DAY, "datetime": _at(hm),
            "open": o, "high": h, "low": l, "close": c, "volume": v}


def _pick(symbol: str, prior_high: float, prior_low: float, atr14: float | None = 4.0) -> dict:
    return {"symbol": symbol, "prior_day_high": prior_high, "prior_day_low": prior_low,
            "atr14": atr14}


def _save_screen(picks: list[dict]) -> None:
    store.save_screen({"schema_version": 1, "date": DAY, "computed_at": "x",
                        "picks": picks, "screened": []})


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    return tmp_path


def _events(day=DAY, now="09:50", **kw):
    return signals.run_day(day, now=_now(now), **kw)["events"]


def _event_types(events):
    return [e["event"] for e in events]


# ===========================================================================
# Rule 3 — setup detection
# ===========================================================================
def test_first_bar_of_the_day_never_sets_up_no_volume_baseline(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, [_bar("ABC", "09:30", 101, 102, 100, 101, 500_000)])

    events = _events()
    assert events == []


def test_setup_requires_1_5x_running_average_volume(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, [
        _bar("ABC", "09:30", 95, 96, 94, 95, 100_000),          # seeds the baseline
        _bar("ABC", "09:35", 100.5, 101, 100.2, 101, 140_000),  # closes > prior high but < 1.5x
    ])

    events = _events()
    assert events == []  # no setup logged — volume rule 3 not cleared


def test_setup_long_arms_on_qualifying_breakout_candle(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, [
        _bar("ABC", "09:30", 95, 96, 94, 95, 100_000),
        _bar("ABC", "09:35", 100.6, 101.5, 100.5, 101, 200_000),  # close>100, vol>=1.5x100k
    ])

    events = _events()
    assert _event_types(events) == ["setup"]
    setup = events[0]
    assert setup["direction"] == "long"
    assert setup["high"] == 101.5 and setup["low"] == 100.5


def test_setup_short_on_close_below_prior_day_low(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, [
        _bar("ABC", "09:30", 95, 96, 94, 95, 100_000),
        _bar("ABC", "09:35", 89.5, 90, 88.5, 89, 200_000),  # close<90
    ])

    events = _events()
    assert _event_types(events) == ["setup"]
    assert events[0]["direction"] == "short"


def test_setup_skipped_when_pick_has_no_atr14(tmp_store):
    _save_screen([_pick("ABC", 100, 90, atr14=None)])
    store.append_bars(DAY, [
        _bar("ABC", "09:30", 95, 96, 94, 95, 100_000),
        _bar("ABC", "09:35", 100.6, 101.5, 100.5, 101, 200_000),
    ])

    events = _events()
    assert _event_types(events) == ["setup_skipped"]
    assert events[0]["reason"] == "no ATR14 on file"


# ===========================================================================
# Rule 4 — entry trigger / expiry
# ===========================================================================
def _setup_bars(symbol="ABC"):
    return [
        _bar(symbol, "09:30", 95, 96, 94, 95, 100_000),
        _bar(symbol, "09:35", 100.6, 101.5, 100.5, 101, 200_000),  # setup: high 101.5, low 100.5
    ]


def test_entry_triggers_on_break_of_setup_candle_high(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _setup_bars() + [
        _bar("ABC", "09:40", 101.6, 102, 101.4, 101.8, 50_000),  # high >= 101.5 -> trigger
    ])

    events = _events()
    assert _event_types(events) == ["setup", "entry"]
    entry = events[1]
    assert entry["entry"] == 101.5
    assert entry["stop"] == 100.5   # entry - ATR14/4 = 101.5 - 1.0
    assert entry["target1"] == 102.5
    assert entry["target2"] == 103.5
    assert entry["size"] > 0


def test_entry_triggers_on_the_second_and_last_allowed_candle(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _setup_bars() + [
        _bar("ABC", "09:40", 101, 101.2, 100.8, 101, 50_000),      # candle 1 — no trigger
        _bar("ABC", "09:45", 101.6, 102, 101.4, 101.8, 50_000),    # candle 2 — trigger
    ])

    events = _events()
    assert _event_types(events) == ["setup", "entry"]


def test_setup_expires_after_two_candles_without_a_trigger(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _setup_bars() + [
        _bar("ABC", "09:40", 101, 101.2, 100.8, 101, 50_000),
        _bar("ABC", "09:45", 101, 101.3, 100.9, 101.1, 50_000),   # still no trigger
    ])

    events = _events()
    assert _event_types(events) == ["setup", "expired"]


# ===========================================================================
# Rule 5/6 — stop / target management
# ===========================================================================
def _entered_bars(symbol="ABC"):
    return _setup_bars(symbol) + [
        _bar(symbol, "09:40", 101.6, 102, 101.4, 101.8, 50_000),  # entry at 101.5
    ]


def test_stop_out_before_half_target_is_a_full_loss(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "09:45", 101, 101.2, 100.4, 100.6, 30_000),  # low <= stop(100.5)
    ])

    events = _events()
    assert _event_types(events) == ["setup", "entry", "stop_out"]
    assert events[-1]["r"] == -1.0


def test_half_target_moves_stop_to_breakeven_then_breakeven_exit(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "09:45", 102, 102.6, 101.9, 102.5, 30_000),  # high >= target1(102.5)
        _bar("ABC", "09:50", 101.6, 101.7, 101.4, 101.5, 20_000),  # low <= breakeven(101.5)
    ])

    events = _events()
    assert _event_types(events) == ["setup", "entry", "half_target", "breakeven_exit"]
    assert events[2]["r"] == pytest.approx(1.0)
    assert events[3]["r"] == pytest.approx(0.5)


def test_half_target_then_full_target_nets_1_5R(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "09:45", 102, 102.6, 101.9, 102.5, 30_000),   # target1
        _bar("ABC", "09:50", 103, 103.6, 102.9, 103.5, 20_000),   # target2 (103.5)
    ])

    events = _events()
    assert _event_types(events) == ["setup", "entry", "half_target", "final_target"]
    assert events[-1]["r"] == pytest.approx(1.5)


def test_half_target_and_target2_in_the_same_bar_resolves_immediately(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "09:45", 102, 103.6, 101.9, 103.5, 30_000),  # spans target1 AND target2
    ])

    events = _events()
    assert _event_types(events) == ["setup", "entry", "half_target", "final_target"]


# ===========================================================================
# Rule 6 — the window/10:00 cutoff
# ===========================================================================
def test_open_trade_left_alone_while_the_window_is_still_running(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars())

    events = _events(now="09:50")  # well before DAYTRADE_WINDOW_END_ET (11:00)
    assert _event_types(events) == ["setup", "entry"]


def test_open_trade_force_closed_at_the_cutoff_using_the_last_bar_close(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "10:55", 101.9, 102.1, 101.7, 102.0, 20_000),  # neither stop nor target1
    ])

    events = _events(now=config.DAYTRADE_WINDOW_END_ET)
    assert _event_types(events) == ["setup", "entry", "time_cutoff"]
    cutoff = events[-1]
    assert cutoff["half_taken"] is False
    assert cutoff["r"] == pytest.approx(0.5)  # (102.0 - 101.5) / 1.0 risk-per-share


def test_open_remainder_force_closed_at_cutoff_after_half_target(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "09:45", 102, 102.6, 101.9, 102.5, 30_000),   # half target hit
        _bar("ABC", "10:55", 102.8, 103, 102.6, 102.9, 20_000),   # remainder still open
    ])

    events = _events(now=config.DAYTRADE_WINDOW_END_ET)
    assert _event_types(events) == ["setup", "entry", "half_target", "time_cutoff"]
    cutoff = events[-1]
    assert cutoff["half_taken"] is True
    # 0.5*1R (banked) + 0.5*((102.9-101.5)/1.0) remainder
    assert cutoff["r"] == pytest.approx(0.5 * 1.0 + 0.5 * 1.4)


# ===========================================================================
# Rule 7 — account-wide risk guardrails
#
# Each symbol below gets its OWN, strictly-later time window (A before B
# before C) so the merged chronological replay resolves A's and B's trades
# before C's setup candle is ever evaluated — the guardrail check at setup
# time has to see the true state as of that moment, not an interleaved one.
# ===========================================================================
def _losing_trade_bars(symbol: str, t_seed: str, t_setup: str, t_entry: str, t_stop: str) -> list[dict]:
    return [
        _bar(symbol, t_seed, 95, 96, 94, 95, 100_000),
        _bar(symbol, t_setup, 100.6, 101.5, 100.5, 101, 200_000),
        _bar(symbol, t_entry, 101.6, 102, 101.4, 101.8, 50_000),
        _bar(symbol, t_stop, 101, 101.2, 100.4, 100.6, 30_000),
    ]


def _winning_trade_bars(symbol: str, t_seed: str, t_setup: str, t_entry: str,
                         t_half: str, t_full: str) -> list[dict]:
    return [
        _bar(symbol, t_seed, 95, 96, 94, 95, 100_000),
        _bar(symbol, t_setup, 100.6, 101.5, 100.5, 101, 200_000),
        _bar(symbol, t_entry, 101.6, 102, 101.4, 101.8, 50_000),
        _bar(symbol, t_half, 102, 102.6, 101.9, 102.5, 30_000),
        _bar(symbol, t_full, 103, 103.6, 102.9, 103.5, 20_000),
    ]


def _setup_only_bars(symbol: str, t_seed: str, t_setup: str) -> list[dict]:
    return [
        _bar(symbol, t_seed, 95, 96, 94, 95, 100_000),
        _bar(symbol, t_setup, 100.6, 101.5, 100.5, 101, 200_000),
    ]


def test_a_third_setup_is_skipped_once_max_trades_per_day_is_reached(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_MAX_LOSSES_PER_DAY", 99)  # isolate the trade-count cap
    monkeypatch.setattr(config, "DAYTRADE_DAILY_STOP_R", 99.0)
    _save_screen([_pick("A", 100, 90), _pick("B", 100, 90), _pick("C", 100, 90)])
    bars = (_losing_trade_bars("A", "09:30", "09:35", "09:40", "09:45")
            + _losing_trade_bars("B", "09:50", "09:55", "10:00", "10:05")
            + _setup_only_bars("C", "10:10", "10:15"))
    store.append_bars(DAY, bars)

    events = _events(now="10:20")
    c_events = [e for e in events if e["symbol"] == "C"]
    assert _event_types(c_events) == ["setup_skipped"]
    assert c_events[0]["reason"] == "max trades/day reached"


def test_day_stops_after_two_losing_trades(tmp_store):
    _save_screen([_pick("A", 100, 90), _pick("B", 100, 90), _pick("C", 100, 90)])
    bars = (_losing_trade_bars("A", "09:30", "09:35", "09:40", "09:45")
            + _losing_trade_bars("B", "09:50", "09:55", "10:00", "10:05")
            + _setup_only_bars("C", "10:10", "10:15"))
    store.append_bars(DAY, bars)

    events = _events(now="10:20")
    c_events = [e for e in events if e["symbol"] == "C"]
    assert c_events[0]["event"] == "setup_skipped"
    assert c_events[0]["reason"] == "two losing trades"


def test_day_stops_after_plus_2r(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_MAX_TRADES_PER_DAY", 5)  # isolate the R cap
    monkeypatch.setattr(config, "DAYTRADE_MAX_LOSSES_PER_DAY", 99)
    _save_screen([_pick("A", 100, 90), _pick("B", 100, 90), _pick("C", 100, 90)])
    bars = (_winning_trade_bars("A", "09:30", "09:35", "09:40", "09:45", "09:50")
            + _winning_trade_bars("B", "09:55", "10:00", "10:05", "10:10", "10:15")
            + _setup_only_bars("C", "10:20", "10:25"))
    store.append_bars(DAY, bars)

    events = _events(now="10:30")
    a_and_b_r = sum(e["r"] for e in events if e["event"] == "final_target")
    assert a_and_b_r == pytest.approx(3.0)  # +1.5R each, past the +2R day stop
    c_events = [e for e in events if e["symbol"] == "C"]
    assert c_events[0]["event"] == "setup_skipped"
    assert c_events[0]["reason"] == "+2R reached"


# ===========================================================================
# Rule 8 — idempotent journaling
# ===========================================================================
def test_run_day_is_idempotent_across_repeated_calls(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars())

    first = _events(now="09:50")
    second = _events(now="09:50")
    assert first == second
    assert len(store.load_signals(DAY)) == len(first)


def test_a_later_call_with_more_bars_only_appends_new_events(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _setup_bars())

    after_setup = _events(now="09:38")
    assert _event_types(after_setup) == ["setup"]

    store.append_bars(DAY, [_bar("ABC", "09:40", 101.6, 102, 101.4, 101.8, 50_000)])
    after_entry = _events(now="09:41")
    assert _event_types(after_entry) == ["setup", "entry"]
    assert len(store.load_signals(DAY)) == 2


def test_run_day_with_no_screener_output_returns_no_events(tmp_store):
    assert _events() == []


def test_run_day_with_no_bars_returns_no_events(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    assert _events() == []


# ===========================================================================
# Phase 3 — execution adapter / trade log wiring
# ===========================================================================
def test_run_day_writes_a_matching_trade_log_row(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "09:45", 102, 102.6, 101.9, 102.5, 30_000),   # half target
        _bar("ABC", "09:50", 103, 103.6, 102.9, 103.5, 20_000),   # final target
    ])

    events = _events()
    entry_event = next(e for e in events if e["event"] == "entry")
    trade_id = entry_event["trade_id"]

    trades = store.load_trades(DAY)
    trade = trades[trade_id]
    assert trade["symbol"] == "ABC" and trade["direction"] == "long"
    assert trade["entry"]["price"] == entry_event["entry"]
    assert trade["entry"]["size"] == entry_event["size"]
    assert [e["kind"] for e in trade["exits"]] == ["half_target", "final_target"]
    assert trade["status"] == "closed"
    assert trade["realized_r"] == pytest.approx(1.5)
    # entry size 50 (see PaperAdapter tests) -> half 25 @ +1, remainder 25 @ +2
    assert trade["realized_pnl"] == pytest.approx(25 * 1.0 + 25 * 2.0)


def test_run_day_trade_log_is_idempotent_across_repeated_calls(tmp_store):
    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars() + [
        _bar("ABC", "09:45", 101, 101.2, 100.4, 100.6, 30_000),  # stop_out
    ])

    _events(now="09:50")
    trade_id = next(iter(store.load_trades(DAY)))
    first_pnl = store.load_trades(DAY)[trade_id]["realized_pnl"]

    _events(now="09:50")  # replay again — must not double the loss
    second = store.load_trades(DAY)[trade_id]
    assert second["realized_pnl"] == first_pnl
    assert len(second["exits"]) == 1


def test_run_day_uses_the_fill_price_an_injected_adapter_returns(tmp_store):
    """The engine must book whatever the adapter says filled, not the level
    it requested — the whole point of routing through an adapter at all."""
    class _SlippageAdapter(adapters.ExecutionAdapter):
        def enter(self, *, symbol, trade_id, direction, price, size, at):
            return adapters.Fill(price=price + 0.05, size=size, at=at)  # +5c slippage

        def exit(self, *, symbol, trade_id, kind, price, size, at, r):
            return adapters.Fill(price=price, size=size, at=at)

    _save_screen([_pick("ABC", 100, 90)])
    store.append_bars(DAY, _entered_bars())

    events = signals.run_day(DAY, now=_now("09:50"), adapter=_SlippageAdapter())["events"]
    entry_event = next(e for e in events if e["event"] == "entry")
    assert entry_event["entry"] == pytest.approx(101.55)  # 101.5 requested + 0.05
    assert entry_event["stop"] == pytest.approx(100.55)   # stop is relative to the FILL, not the request
