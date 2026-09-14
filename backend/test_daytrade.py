"""Day-trade sleeve — Phase 1 (schema + scheduled ingestion), TRAVIS_EXTENSION.

Offline throughout: synthetic OHLCV frames, monkeypatched fetch functions, a
tmp_path store. No network, no live Schwab.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import config
from daytrade import bars, scheduler, store, universe

ET = ZoneInfo("America/New_York")


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
        if t == "BROKEN":
            raise RuntimeError("provider down")
        return good

    monkeypatch.setattr(universe.data_handler, "get_daily", _get_daily)

    result = universe.screen(tickers=["GOOD", "BROKEN"], now=datetime(2026, 9, 14, tzinfo=ET))

    assert [p["symbol"] for p in result["picks"]] == ["GOOD"]
    broken = next(r for r in result["screened"] if r["symbol"] == "BROKEN")
    assert broken["qualified"] is False
    assert "provider down" in broken["reason"]


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


# ===========================================================================
# scheduler.py — pure predicates
# ===========================================================================
def test_screen_due_fires_once_per_trading_day_after_threshold():
    threshold = datetime.strptime(config.DAYTRADE_SCREEN_ET, "%H:%M")
    before = datetime(2026, 9, 14, threshold.hour, threshold.minute - 1)
    at = datetime(2026, 9, 14, threshold.hour, threshold.minute)

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
