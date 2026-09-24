"""Day-trade sleeve — Phase 1 (schema + scheduled ingestion), TRAVIS_EXTENSION.

Offline throughout: synthetic OHLCV frames, monkeypatched fetch functions, a
tmp_path store. No network, no live Schwab.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import config
from daytrade import bars, budget, scheduler, settings, signals, store, trial, universe

ET = ZoneInfo("America/New_York")


def _await_screen_status(timeout: float = 2.0) -> dict:
    """Poll universe.screen_status() until the background rescan finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = universe.screen_status()
        if not st["running"]:
            return st
        time.sleep(0.02)
    pytest.fail("background screen never finished")


def _frame(price: float, spread: float, volume: float, days: int = 30) -> pd.DataFrame:
    """A flat-price OHLCV frame whose Wilder ATR converges to `spread` and
    whose ATR% is therefore spread/price*100 — deterministic and easy to
    target against config.DAYTRADE_ATR_PCT_MIN/MAX."""
    idx = pd.bdate_range("2024-01-01", periods=days)
    close = pd.Series(price, index=idx, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close + spread / 2, "Low": close - spread / 2,
        "Close": close, "Volume": float(volume),
    }, index=idx)


# ===========================================================================
# store.py
# ===========================================================================
@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    return tmp_path


def test_save_and_load_screen_roundtrip(tmp_store):
    result = {"schema_version": 1, "date": "2026-09-14", "computed_at": "2026-09-14T21:00:00+00:00",
              "picks": [{"symbol": "ABC"}], "screened": [{"symbol": "ABC", "qualified": True}]}
    store.save_screen(result)
    loaded = store.load_screen("2026-09-14")
    assert loaded == result


def test_load_screen_missing_day_returns_none(tmp_store):
    assert store.load_screen("2026-01-01") is None


def test_load_screen_health_with_no_file_returns_empty_dict(tmp_store):
    assert store.load_screen_health() == {}


def test_save_screen_health_keys_by_trigger_without_clobbering(tmp_store):
    store.save_screen_health({"trigger": "scheduled", "succeeded_at": "2026-09-21T21:25:00+00:00",
                               "date": "2026-09-22", "picks": 12, "screened": 1298})
    store.save_screen_health({"trigger": "manual", "succeeded_at": "2026-09-22T13:14:00+00:00",
                               "date": "2026-09-22", "picks": 10, "screened": 1298})

    health = store.load_screen_health()

    assert health["scheduled"]["picks"] == 12
    assert health["manual"]["picks"] == 10

    # A later scheduled success overwrites only the scheduled entry.
    store.save_screen_health({"trigger": "scheduled", "succeeded_at": "2026-09-22T21:25:00+00:00",
                               "date": "2026-09-23", "picks": 15, "screened": 1298})
    health = store.load_screen_health()
    assert health["scheduled"]["date"] == "2026-09-23"
    assert health["manual"]["date"] == "2026-09-22"  # untouched


def test_append_and_load_bars_filters_by_symbol(tmp_store):
    store.append_bars("2026-09-14", [
        {"symbol": "ABC", "datetime": "2026-09-14T13:30:00+00:00", "close": 45.0},
        {"symbol": "XYZ", "datetime": "2026-09-14T13:30:00+00:00", "close": 20.0},
    ])
    store.append_bars("2026-09-14", [
        {"symbol": "ABC", "datetime": "2026-09-14T13:35:00+00:00", "close": 45.2},
    ])
    assert len(store.load_bars("2026-09-14")) == 3
    abc = store.load_bars("2026-09-14", "ABC")
    assert [b["close"] for b in abc] == [45.0, 45.2]
    assert store.load_bars("2026-09-14", "NONE") == []


def test_load_bars_missing_day_returns_empty(tmp_store):
    assert store.load_bars("2026-01-01") == []


def test_latest_bars_keeps_the_last_occurrence_per_symbol(tmp_store):
    # Two ingest ticks, interleaved symbols — latest_bars must pick each
    # symbol's LAST appended row, not just whichever comes last in the file
    # overall.
    store.append_bars("2026-09-14", [
        {"symbol": "ABC", "datetime": "2026-09-14T13:30:00+00:00", "close": 45.0},
        {"symbol": "XYZ", "datetime": "2026-09-14T13:30:00+00:00", "close": 20.0},
    ])
    store.append_bars("2026-09-14", [
        {"symbol": "ABC", "datetime": "2026-09-14T13:35:00+00:00", "close": 45.5},
    ])

    latest = store.latest_bars("2026-09-14")
    assert latest["ABC"]["close"] == 45.5
    assert latest["XYZ"]["close"] == 20.0


def test_latest_bars_missing_day_returns_empty(tmp_store):
    assert store.latest_bars("2026-01-01") == {}


def test_iter_all_trades_yields_every_day_oldest_first(tmp_store):
    store.save_trades("2026-09-15", "primary", {"t2": {"symbol": "XYZ"}})
    store.save_trades("2026-09-14", "primary", {"t1": {"symbol": "ABC"}})

    days = list(store.iter_all_trades("primary"))

    assert [d for d, _ in days] == ["2026-09-14", "2026-09-15"]
    assert days[0][1] == {"t1": {"symbol": "ABC"}}


def test_iter_all_trades_with_no_files_yields_nothing(tmp_store):
    assert list(store.iter_all_trades("primary")) == []


def test_iter_all_trades_only_yields_the_requested_account(tmp_store):
    store.save_trades("2026-09-14", "primary", {"t1": {"symbol": "ABC"}})
    store.save_trades("2026-09-14", "ira", {"t2": {"symbol": "XYZ"}})

    assert [d for d, _ in store.iter_all_trades("primary")] == ["2026-09-14"]
    assert [d for d, _ in store.iter_all_trades("ira")] == ["2026-09-14"]
    assert list(store.iter_all_trades("primary"))[0][1] == {"t1": {"symbol": "ABC"}}


# ===========================================================================
# universe.py — Rule 1
# ===========================================================================
def test_screen_picks_qualifying_names_and_records_prior_day_levels(tmp_store, monkeypatch):
    frames = {
        "GOOD": _frame(price=100.0, spread=3.0, volume=2_000_000),   # ATR% 3.0 — in band
        "CHEAP": _frame(price=10.0, spread=3.0, volume=2_000_000),   # price too low
        "THIN": _frame(price=100.0, spread=3.0, volume=500_000),     # volume too low
        "CALM": _frame(price=100.0, spread=1.0, volume=2_000_000),   # ATR% 1.0 — too low
    }
    monkeypatch.setattr(universe.data_handler, "get_daily", lambda t, force=False: frames[t])

    result = universe.screen(tickers=list(frames), now=datetime(2026, 9, 14, tzinfo=ET))

    assert result["date"] == "2026-09-14"
    assert [p["symbol"] for p in result["picks"]] == ["GOOD"]
    assert len(result["screened"]) == 4
    good = next(r for r in result["screened"] if r["symbol"] == "GOOD")
    assert good["qualified"] is True
    assert good["prior_day_high"] == pytest.approx(101.5)
    assert good["prior_day_low"] == pytest.approx(98.5)
    for symbol in ("CHEAP", "THIN", "CALM"):
        row = next(r for r in result["screened"] if r["symbol"] == symbol)
        assert row["qualified"] is False
        assert row["reason"]

    assert store.load_screen("2026-09-14")["picks"][0]["symbol"] == "GOOD"


def test_screen_forces_a_fresh_fetch_past_get_dailys_12h_cache(tmp_store, monkeypatch):
    """Regression: the screener's whole job is to capture the most recently
    COMPLETED session's high/low/close. get_daily's 12h cache is fine for a
    read-heavy path, but wrong here — a same-day-earlier cache entry (e.g.
    from a pre-market "Rescan now") can be under 12h old while still
    predating today's close, and a non-forced read would silently keep
    serving that stale pre-close frame forever after."""
    stale = _frame(price=90.0, spread=3.0, volume=2_000_000)   # what a non-forced read would serve
    fresh = _frame(price=100.0, spread=3.0, volume=2_000_000)  # the actual just-closed session

    def fake_get_daily(t, force=False):
        return fresh if force else stale
    monkeypatch.setattr(universe.data_handler, "get_daily", fake_get_daily)

    result = universe.screen(tickers=["GOOD"], now=datetime(2026, 9, 14, tzinfo=ET))

    assert result["screened"][0]["price"] == 100.0


def test_screen_prefetches_and_evaluates_with_force_true(tmp_store, monkeypatch):
    """The parallel prefetch warm-up must force too, or it warms the cache
    respecting the stale-same-day entry while _evaluate force-fetches
    anyway — wasting the parallel warm-up instead of doing the real fetching
    there."""
    calls: dict = {}

    def fake_prefetch(tickers, force=False):
        calls["prefetch_force"] = force
    monkeypatch.setattr(universe.data_handler, "prefetch", fake_prefetch)

    def fake_get_daily(t, force=False):
        calls.setdefault("get_daily_force", []).append(force)
        return _frame(price=100.0, spread=3.0, volume=2_000_000)
    monkeypatch.setattr(universe.data_handler, "get_daily", fake_get_daily)

    universe.screen(tickers=["GOOD"], now=datetime(2026, 9, 14, tzinfo=ET))

    assert calls["prefetch_force"] is True
    assert calls["get_daily_force"] == [True]


def test_screen_records_success_under_its_own_trigger(tmp_store, monkeypatch):
    monkeypatch.setattr(universe.data_handler, "get_daily",
                        lambda t, force=False: _frame(price=100.0, spread=3.0, volume=2_000_000))

    universe.screen(tickers=["GOOD"], now=datetime(2026, 9, 14, tzinfo=ET), trigger="scheduled")
    health = store.load_screen_health()
    assert health["scheduled"]["date"] == "2026-09-14"
    assert health["scheduled"]["picks"] == 1
    assert "manual" not in health

    universe.screen(tickers=["GOOD"], now=datetime(2026, 9, 15, tzinfo=ET))  # default trigger="manual"
    health = store.load_screen_health()
    assert health["manual"]["date"] == "2026-09-15"
    assert health["scheduled"]["date"] == "2026-09-14"  # untouched by the manual run


def test_screen_date_override_files_under_a_different_date_than_now(tmp_store, monkeypatch):
    """The scheduler's after-close run needs this: `now` stays the real run
    time (so computed_at is honest) while the saved file's date is the day
    the picks are FOR."""
    frames = {"GOOD": _frame(price=100.0, spread=3.0, volume=2_000_000)}
    monkeypatch.setattr(universe.data_handler, "get_daily", lambda t, force=False: frames[t])

    result = universe.screen(tickers=list(frames), now=datetime(2026, 9, 18, 21, 45, tzinfo=ET),
                              date_override=date(2026, 9, 21))

    assert result["date"] == "2026-09-21"
    assert result["computed_at"].startswith("2026-09-19")  # 21:45 ET == 01:45 UTC next day
    assert store.load_screen("2026-09-21") is not None
    assert store.load_screen("2026-09-18") is None


def test_screen_caps_picks_at_max_and_ranks_by_avg_volume(tmp_store, monkeypatch):
    frames = {f"T{i}": _frame(price=100.0, spread=3.0, volume=1_000_000 * (i + 1))
              for i in range(config.DAYTRADE_UNIVERSE_MAX + 2)}
    monkeypatch.setattr(universe.data_handler, "get_daily", lambda t, force=False: frames[t])

    result = universe.screen(tickers=list(frames), now=datetime(2026, 9, 14, tzinfo=ET))

    assert len(result["picks"]) == config.DAYTRADE_UNIVERSE_MAX
    volumes = [p["avg_volume"] for p in result["picks"]]
    assert volumes == sorted(volumes, reverse=True)
    assert len(result["screened"]) == config.DAYTRADE_UNIVERSE_MAX + 2


def test_screen_handles_a_fetch_failure_without_aborting_the_sweep(tmp_store, monkeypatch):
    good = _frame(price=100.0, spread=3.0, volume=2_000_000)

    def _get_daily(t, force=False):
        # The real get_daily() never raises — a failed fetch degrades to a
        # cached/stale frame or None (data_handler.py's own contract), which
        # is what screen()'s prefetch() relies on. None here is the "no
        # provider, nothing cached" case.
        if t == "BROKEN":
            return None
        return good

    monkeypatch.setattr(universe.data_handler, "get_daily", _get_daily)

    result = universe.screen(tickers=["GOOD", "BROKEN"], now=datetime(2026, 9, 14, tzinfo=ET))

    assert [p["symbol"] for p in result["picks"]] == ["GOOD"]
    broken = next(r for r in result["screened"] if r["symbol"] == "BROKEN")
    assert broken["qualified"] is False
    assert broken["reason"] == "no data"


def test_screen_prefetches_tickers_in_parallel_before_scoring(tmp_store, monkeypatch):
    """On a cold cache, _evaluate()'s sequential get_daily() calls used to be
    the only fetch path — one ticker at a time, no parallelism. screen() must
    warm the cache via data_handler.prefetch() (the shared 8-worker pool)
    first, the same pattern CFM's own full-universe scan uses."""
    frame = _frame(price=100.0, spread=3.0, volume=2_000_000)
    prefetched = []
    monkeypatch.setattr(universe.data_handler, "prefetch", lambda syms, force=False: prefetched.append(list(syms)))
    monkeypatch.setattr(universe.data_handler, "get_daily", lambda t, force=False: frame)

    universe.screen(tickers=["A", "B", "C"], now=datetime(2026, 9, 14, tzinfo=ET))

    assert prefetched == [["A", "B", "C"]]


# ===========================================================================
# universe.py — on-demand rescan (the "Rescan now" button)
# ===========================================================================
def test_start_background_screen_runs_and_persists_todays_picks(tmp_store, monkeypatch):
    frames = {"GOOD": _frame(price=100.0, spread=3.0, volume=2_000_000)}
    monkeypatch.setattr(universe.data_handler, "get_daily", lambda t, force=False: frames[t])
    monkeypatch.setattr(universe.daytrade_tickers, "all_tickers", lambda: list(frames))

    out = universe.start_background_screen()
    assert out["running"] is True

    st = _await_screen_status()
    assert st["status"] == "done"
    assert st["error"] is None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert store.load_screen(today)["picks"][0]["symbol"] == "GOOD"


def test_start_background_screen_dedupes_a_concurrent_call(tmp_store, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_get_daily(t, force=False):
        started.set()
        release.wait(timeout=2)
        return _frame(price=100.0, spread=3.0, volume=2_000_000)

    monkeypatch.setattr(universe.data_handler, "get_daily", slow_get_daily)
    monkeypatch.setattr(universe.daytrade_tickers, "all_tickers", lambda: ["GOOD"])

    first = universe.start_background_screen()
    assert started.wait(timeout=2), "background thread never started fetching"
    thread_before = universe._scan_thread
    second = universe.start_background_screen()  # while the first is still in flight

    assert first["running"] is True and second["running"] is True
    assert universe._scan_thread is thread_before  # deduped: no second thread spawned

    release.set()
    _await_screen_status()


def test_start_background_screen_reports_an_error(tmp_store, monkeypatch):
    monkeypatch.setattr(universe, "screen", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    universe.start_background_screen()
    st = _await_screen_status()

    assert st["status"] == "error"
    assert "boom" in st["error"]


# ===========================================================================
# bars.py — Rule 2 ingestion
# ===========================================================================
class _FakeClient:
    def __init__(self, by_symbol):
        self._by_symbol = by_symbol

    def get_intraday_bars(self, symbol, minutes=5):
        val = self._by_symbol[symbol]
        if isinstance(val, Exception):
            raise val
        return val


def _bar_df(when: datetime, price: float) -> pd.DataFrame:
    idx = pd.DatetimeIndex([when])
    return pd.DataFrame({"Open": [price], "High": [price + 0.1], "Low": [price - 0.1],
                          "Close": [price], "Volume": [12345.0]}, index=idx)


def _bars_df(rows: list[tuple[datetime, float]]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([w for w, _ in rows])
    prices = [p for _, p in rows]
    return pd.DataFrame({"Open": prices, "High": [p + 0.1 for p in prices],
                          "Low": [p - 0.1 for p in prices], "Close": prices,
                          "Volume": [12345.0] * len(rows)}, index=idx)


def test_bars_ingest_writes_todays_picks_and_dedupes(tmp_store, monkeypatch):
    store.save_screen({"schema_version": 1, "date": "2026-09-14", "computed_at": "x",
                        "picks": [{"symbol": "ABC"}, {"symbol": "XYZ"}], "screened": []})
    when = datetime(2026, 9, 14, 9, 35, tzinfo=ET)
    fake = _FakeClient({"ABC": _bar_df(when, 45.0), "XYZ": _bar_df(when, 20.0)})
    monkeypatch.setattr(bars.data_handler, "client", lambda: fake)

    first = bars.ingest(now=when)
    assert first["written"] == 2
    assert first["errors"] == {}
    assert len(store.load_bars("2026-09-14")) == 2

    # Same candle fetched again (e.g. the next tick before a new bar formed):
    # already-logged (symbol, datetime) pairs are not duplicated.
    second = bars.ingest(now=when)
    assert second["written"] == 0
    assert len(store.load_bars("2026-09-14")) == 2


def test_bars_ingest_skips_a_failed_symbol_without_dropping_the_rest(tmp_store, monkeypatch):
    store.save_screen({"schema_version": 1, "date": "2026-09-14", "computed_at": "x",
                        "picks": [{"symbol": "ABC"}, {"symbol": "BROKEN"}], "screened": []})
    when = datetime(2026, 9, 14, 9, 35, tzinfo=ET)
    fake = _FakeClient({"ABC": _bar_df(when, 45.0), "BROKEN": RuntimeError("no data")})
    monkeypatch.setattr(bars.data_handler, "client", lambda: fake)

    result = bars.ingest(now=when)

    assert result["written"] == 1
    assert "BROKEN" in result["errors"]
    assert store.load_bars("2026-09-14", "ABC")


def test_bars_ingest_with_no_screener_picks_writes_nothing(tmp_store):
    result = bars.ingest(now=datetime(2026, 9, 14, 9, 35, tzinfo=ET))
    assert result == {"symbols": [], "written": 0, "errors": {}}


def test_bars_ingest_ignores_candles_padded_past_now(tmp_store, monkeypatch):
    """Regression: if the provider pads today's response with not-yet-elapsed
    slots (carrying the last traded price forward under a future timestamp),
    iloc[-1] would log a bar hours ahead of `now` and frozen at a stale
    price. The real, current candle must win even though it isn't last in
    the frame."""
    store.save_screen({"schema_version": 1, "date": "2026-09-14", "computed_at": "x",
                        "picks": [{"symbol": "ABC"}], "screened": []})
    now = datetime(2026, 9, 14, 9, 35, tzinfo=ET)
    padded = _bars_df([
        (datetime(2026, 9, 14, 9, 30, tzinfo=ET), 44.0),
        (now, 45.0),                                          # the real latest candle
        (datetime(2026, 9, 14, 15, 55, tzinfo=ET), 44.0),      # padded future slot
    ])
    monkeypatch.setattr(bars.data_handler, "client", lambda: _FakeClient({"ABC": padded}))

    result = bars.ingest(now=now)

    assert result["written"] == 1
    assert result["errors"] == {}
    loaded = store.load_bars("2026-09-14", "ABC")
    assert len(loaded) == 1
    assert loaded[0]["close"] == 45.0
    assert loaded[0]["datetime"].startswith("2026-09-14T09:35")


def test_bars_ingest_errors_when_every_candle_is_in_the_future(tmp_store, monkeypatch):
    store.save_screen({"schema_version": 1, "date": "2026-09-14", "computed_at": "x",
                        "picks": [{"symbol": "ABC"}], "screened": []})
    now = datetime(2026, 9, 14, 9, 35, tzinfo=ET)
    only_future = _bars_df([(datetime(2026, 9, 14, 15, 55, tzinfo=ET), 44.0)])
    monkeypatch.setattr(bars.data_handler, "client", lambda: _FakeClient({"ABC": only_future}))

    result = bars.ingest(now=now)

    assert result["written"] == 0
    assert "ABC" in result["errors"]
    assert store.load_bars("2026-09-14", "ABC") == []


def test_bars_ingest_ignores_a_whole_prior_trading_days_candles(tmp_store, monkeypatch):
    """Regression: right at/after today's open, before today's own candles
    exist yet, a provider can hand back an entirely PRIOR day's full session
    instead of today's partial one. Every one of those candles is trivially
    `<= now` (they're all in the past) — a `<= now` filter alone lets the
    whole stale session through, and iloc[-1] picks yesterday's last candle
    (~15:55-16:00 ET) as if it were today's latest. Must be dated `day`, not
    merely not-in-the-future."""
    store.save_screen({"schema_version": 1, "date": "2026-09-14", "computed_at": "x",
                        "picks": [{"symbol": "ABC"}], "screened": []})
    now = datetime(2026, 9, 14, 9, 35, tzinfo=ET)
    prior_day_only = _bars_df([
        (datetime(2026, 9, 11, 9, 30, tzinfo=ET), 40.0),
        (datetime(2026, 9, 11, 15, 55, tzinfo=ET), 44.0),   # yesterday's last candle
    ])
    monkeypatch.setattr(bars.data_handler, "client", lambda: _FakeClient({"ABC": prior_day_only}))

    result = bars.ingest(now=now)

    assert result["written"] == 0
    assert "ABC" in result["errors"]
    assert store.load_bars("2026-09-14", "ABC") == []


def test_bars_ingest_picks_todays_candle_over_a_mixed_prior_day_batch(tmp_store, monkeypatch):
    """The companion positive case: when the response mixes a real prior-day
    tail with a genuine today candle, the today candle must win — the fix
    isn't "reject anything old," it's "the latest candle must be FOR today"."""
    store.save_screen({"schema_version": 1, "date": "2026-09-14", "computed_at": "x",
                        "picks": [{"symbol": "ABC"}], "screened": []})
    now = datetime(2026, 9, 14, 9, 40, tzinfo=ET)
    mixed = _bars_df([
        (datetime(2026, 9, 11, 15, 55, tzinfo=ET), 44.0),   # yesterday's last candle
        (datetime(2026, 9, 14, 9, 35, tzinfo=ET), 46.5),    # today's real candle
    ])
    monkeypatch.setattr(bars.data_handler, "client", lambda: _FakeClient({"ABC": mixed}))

    result = bars.ingest(now=now)

    assert result["written"] == 1
    loaded = store.load_bars("2026-09-14", "ABC")
    assert loaded[0]["close"] == 46.5
    assert loaded[0]["datetime"].startswith("2026-09-14T09:35")


# ===========================================================================
# scheduler.py — pure predicates
# ===========================================================================
def test_screen_due_fires_once_per_trading_day_after_threshold():
    threshold = datetime.strptime(config.DAYTRADE_SCREEN_ET, "%H:%M")
    at = datetime(2026, 9, 14, threshold.hour, threshold.minute)
    before = at - timedelta(minutes=1)

    assert scheduler.screen_due(before, None) is False
    assert scheduler.screen_due(at, None) is True
    assert scheduler.screen_due(at, at.date()) is False       # already ran today
    assert scheduler.screen_due(at, at.date() - timedelta(days=1)) is True


def test_in_window_true_only_within_rule_2_hours_on_weekdays():
    monday = datetime(2026, 9, 14)  # a Monday
    saturday = datetime(2026, 9, 19)
    start_h, start_m = (int(x) for x in config.DAYTRADE_WINDOW_START_ET.split(":"))

    assert scheduler.in_window(monday.replace(hour=start_h, minute=start_m)) is True
    assert scheduler.in_window(monday.replace(hour=8, minute=0)) is False
    assert scheduler.in_window(saturday.replace(hour=start_h, minute=start_m)) is False


def test_bar_fetch_due_respects_interval_and_window():
    start_h, start_m = (int(x) for x in config.DAYTRADE_WINDOW_START_ET.split(":"))
    now = datetime(2026, 9, 14, start_h, start_m)  # Monday, in window

    assert scheduler.bar_fetch_due(now, None) is True
    just_fetched = now - timedelta(minutes=1)
    assert scheduler.bar_fetch_due(now, just_fetched) is False
    stale = now - timedelta(minutes=config.DAYTRADE_BAR_INTERVAL_MINUTES)
    assert scheduler.bar_fetch_due(now, stale) is True

    outside_window = now.replace(hour=7)
    assert scheduler.bar_fetch_due(outside_window, None) is False


def test_signals_finalize_due_fires_once_per_trading_day_after_window_end():
    end_h, end_m = (int(x) for x in config.DAYTRADE_WINDOW_END_ET.split(":"))
    at = datetime(2026, 9, 14, end_h, end_m)
    before = at - timedelta(minutes=1)

    assert scheduler.signals_finalize_due(before, None) is False
    assert scheduler.signals_finalize_due(at, None) is True
    assert scheduler.signals_finalize_due(at, at.date()) is False      # already ran today
    assert scheduler.signals_finalize_due(at, at.date() - timedelta(days=1)) is True


def test_run_signals_sizes_off_the_live_daytrade_budget(monkeypatch):
    """The scheduler — not signals.run_day's own default — is what wires the
    live dry-powder budget in, so a bad budget read can never silently
    resize a symbol-engine test's expectations (see daytrade/budget.py)."""
    seen = {}
    monkeypatch.setattr(budget, "daytrade_budget",
                        lambda aid: {"amount": 777.0, "source": "dry_powder", "detail": "x"})
    monkeypatch.setattr(trial, "trial_status",
                        lambda aid: {"status": "running", "completed_trades": 0, "target_trades": 50})

    def _fake_run_day(day, account_id, now=None, account_equity=None, adapter=None,
                       entries_enabled=True):
        seen["account_id"] = account_id
        seen["account_equity"] = account_equity
        seen["entries_enabled"] = entries_enabled
        return {"date": day, "events": []}
    monkeypatch.setattr(signals, "run_day", _fake_run_day)

    scheduler._run_signals(datetime(2026, 9, 14, 10, 0, tzinfo=ET), "primary")

    assert seen["account_id"] == "primary"
    assert seen["account_equity"] == 777.0
    assert seen["entries_enabled"] is True


def test_run_signals_disables_entries_once_the_trial_is_complete(monkeypatch):
    seen = {}
    monkeypatch.setattr(budget, "daytrade_budget",
                        lambda aid: {"amount": 777.0, "source": "dry_powder", "detail": "x"})
    monkeypatch.setattr(trial, "trial_status",
                        lambda aid: {"status": "complete", "completed_trades": 50, "target_trades": 50})

    def _fake_run_day(day, account_id, now=None, account_equity=None, adapter=None,
                       entries_enabled=True):
        seen["entries_enabled"] = entries_enabled
        return {"date": day, "events": []}
    monkeypatch.setattr(signals, "run_day", _fake_run_day)

    scheduler._run_signals(datetime(2026, 9, 14, 10, 0, tzinfo=ET), "primary")

    assert seen["entries_enabled"] is False


def test_maybe_screen_skips_when_no_account_is_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    monkeypatch.setattr(settings, "enabled_account_ids", lambda: [])
    called = []
    monkeypatch.setattr(scheduler, "_run_screen", lambda now: called.append(True))
    monkeypatch.setattr(scheduler, "_last_screen_day", None)

    scheduler._maybe_screen(datetime(2026, 9, 14, 17, 0, tzinfo=ET))  # past DAYTRADE_SCREEN_ET

    assert called == []


def test_maybe_screen_skips_once_every_enabled_accounts_trial_is_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    monkeypatch.setattr(settings, "enabled_account_ids", lambda: ["primary", "ira"])
    monkeypatch.setattr(trial, "trial_status", lambda aid: {"status": "complete"})
    called = []
    monkeypatch.setattr(scheduler, "_run_screen", lambda now: called.append(True))
    monkeypatch.setattr(scheduler, "_last_screen_day", None)

    scheduler._maybe_screen(datetime(2026, 9, 14, 17, 0, tzinfo=ET))  # past DAYTRADE_SCREEN_ET

    assert called == []


def test_maybe_screen_runs_while_any_enabled_account_still_has_a_running_trial(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    monkeypatch.setattr(settings, "enabled_account_ids", lambda: ["primary", "ira"])
    monkeypatch.setattr(trial, "trial_status",
                        lambda aid: {"status": "complete" if aid == "primary" else "running"})
    called = []
    monkeypatch.setattr(scheduler, "_run_screen", lambda now: called.append(True))
    monkeypatch.setattr(scheduler, "_last_screen_day", None)

    scheduler._maybe_screen(datetime(2026, 9, 14, 17, 0, tzinfo=ET))

    assert called == [True]


def test_maybe_screen_files_the_result_under_todays_own_date(monkeypatch, tmp_path):
    """Regression (and change of behavior): DAYTRADE_SCREEN_ET is a
    PRE-MARKET time, so the scheduled run happens the morning OF the
    trading day it's for — it must file under THAT day (today), not the
    next one, since bar ingest/signals look up "today's screen" by exact
    date match. (An earlier design ran this job after the close instead,
    filing under the next trading day for the same reason; moving the run
    to pre-market let the prior session's daily bar settle overnight
    first, and simplified this to "just file under today.")"""
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    monkeypatch.setattr(settings, "enabled_account_ids", lambda: ["primary"])
    monkeypatch.setattr(trial, "trial_status",
                        lambda aid: {"status": "running", "completed_trades": 0, "target_trades": 50})
    monkeypatch.setattr(universe.data_handler, "get_daily",
                        lambda t, force=False: _frame(price=100.0, spread=3.0, volume=2_000_000))
    monkeypatch.setattr(universe.daytrade_tickers, "all_tickers", lambda: ["GOOD"])
    monkeypatch.setattr(scheduler, "_last_screen_day", None)

    monday = datetime(2026, 9, 21, 4, 0, tzinfo=ET)  # at/past DAYTRADE_SCREEN_ET, pre-market
    scheduler._maybe_screen(monday)

    result = store.load_screen("2026-09-21")             # filed for TODAY, not tomorrow
    assert result is not None
    assert result["picks"][0]["symbol"] == "GOOD"

    health = store.load_screen_health()
    assert health["scheduled"]["date"] == "2026-09-21"
    assert "manual" not in health


def test_for_each_enabled_account_isolates_one_accounts_exception(monkeypatch):
    import contextlib
    import accounts
    monkeypatch.setattr(accounts, "use", lambda account_id: contextlib.nullcontext())
    monkeypatch.setattr(settings, "enabled_account_ids", lambda: ["primary", "ira"])
    seen = []

    def _fn(account_id):
        if account_id == "primary":
            raise RuntimeError("boom")
        seen.append(account_id)

    scheduler._for_each_enabled_account("test job", _fn)  # must not raise

    assert seen == ["ira"]


def test_digest_due_fires_once_per_day_after_threshold():
    end_h, end_m = (int(x) for x in config.DAYTRADE_DIGEST_ET.split(":"))
    at = datetime(2026, 9, 14, end_h, end_m)
    before = at - timedelta(minutes=1)

    assert scheduler.digest_due(before, None) is False
    assert scheduler.digest_due(at, None) is True
    assert scheduler.digest_due(at, at.date()) is False
    assert scheduler.digest_due(at, at.date() - timedelta(days=1)) is True
