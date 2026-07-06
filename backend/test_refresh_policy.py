"""Smart intraday refresh: which stocks land in the frequently-refreshed "hot"
set, the cadence gate, and the market-hours guard."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import data_handler
import maintenance
import refresh_policy
import screening

ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    refresh_policy._last_refresh = None
    # Never touch a real provider: record what would be force-refreshed.
    calls = {"prefetch": []}
    monkeypatch.setattr(data_handler, "prefetch",
                        lambda syms, force=False: calls["prefetch"].append((list(syms), force)))
    yield calls
    refresh_policy._last_refresh = None


def _scorecard(rows):
    """Stub the memoized scorecard peek with the given candidate rows."""
    return lambda key, max_age=None: {"results": rows}


def test_hot_set_includes_positions_candidates_and_earnings(monkeypatch):
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: ["ON", "NVDA"])
    soon = (date.today() + timedelta(days=3)).isoformat()
    far = (date.today() + timedelta(days=60)).isoformat()
    rows = [
        {"ticker": "AVGO", "verdict": "GO", "earnings_date": far},        # GO candidate
        {"ticker": "MRVL", "verdict": "CAUTION", "earnings_date": soon},  # earnings-imminent
        {"ticker": "IBM", "verdict": "AVOID", "earnings_date": far},      # neither → excluded
    ]
    monkeypatch.setattr(screening, "peek_cached", _scorecard(rows))

    hot = refresh_policy.hot_tickers(state={})
    assert "ON" in hot and "NVDA" in hot          # positions
    assert "AVGO" in hot                          # GO candidate
    assert "MRVL" in hot                          # earnings within warn window
    assert "IBM" not in hot                       # AVOID + far earnings


def test_hot_set_dedupes_a_held_candidate(monkeypatch):
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: ["NVDA"])
    monkeypatch.setattr(screening, "peek_cached",
                        _scorecard([{"ticker": "NVDA", "verdict": "GO"}]))
    assert refresh_policy.hot_tickers(state={}).count("NVDA") == 1


def test_positions_are_never_dropped_by_the_cap(monkeypatch):
    # More open positions than HOT_TICKERS_MAX: every one must survive, and the
    # candidate tail is what gets truncated, not live-risk names.
    monkeypatch.setattr(config, "HOT_TICKERS_MAX", 3)
    positions = [f"P{i}" for i in range(5)]
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: positions)
    monkeypatch.setattr(screening, "peek_cached",
                        _scorecard([{"ticker": "CAND", "verdict": "GO"}]))
    hot = refresh_policy.hot_tickers(state={})
    assert all(p in hot for p in positions)   # all 5 positions kept
    assert "CAND" not in hot                   # candidate dropped past the cap


def test_stale_scorecard_memo_is_ignored(monkeypatch):
    # peek_cached returns None when the memo is older than HOT_CANDIDATE_MAX_AGE;
    # the hot set then falls back to positions only.
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: ["ON"])
    monkeypatch.setattr(screening, "peek_cached", lambda key, max_age=None: None)
    assert refresh_policy.hot_tickers(state={}) == ["ON"]


def test_cadence_gate_rate_limits_refreshes(_reset, monkeypatch):
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: ["ON"])
    monkeypatch.setattr(screening, "peek_cached", lambda key, max_age=None: None)
    t0 = datetime(2026, 7, 6, 10, 0, tzinfo=ET)

    first = refresh_policy.maybe_refresh_hot(t0)
    assert first and first["count"] == 1
    assert len(_reset["prefetch"]) == 1

    # 5 minutes later — inside the 15-min cadence, so skipped (no new prefetch).
    assert refresh_policy.maybe_refresh_hot(t0 + timedelta(minutes=5)) is None
    assert len(_reset["prefetch"]) == 1

    # 16 minutes later — cadence elapsed, refreshes again.
    assert refresh_policy.maybe_refresh_hot(t0 + timedelta(minutes=16)) is not None
    assert len(_reset["prefetch"]) == 2


def test_force_bypasses_the_cadence_gate(_reset, monkeypatch):
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: ["ON"])
    monkeypatch.setattr(screening, "peek_cached", lambda key, max_age=None: None)
    t0 = datetime(2026, 7, 6, 10, 0, tzinfo=ET)
    refresh_policy.maybe_refresh_hot(t0)
    # Immediately again with force — refreshes despite being inside the cadence.
    assert refresh_policy.maybe_refresh_hot(t0, force=True) is not None
    assert len(_reset["prefetch"]) == 2


def test_market_hours_guard():
    import alert_scheduler
    # Monday 10:00 ET — open.
    assert alert_scheduler._market_hours(datetime(2026, 7, 6, 10, 0, tzinfo=ET)) is True
    # Monday 08:00 ET — pre-open.
    assert alert_scheduler._market_hours(datetime(2026, 7, 6, 8, 0, tzinfo=ET)) is False
    # Monday 16:30 ET — after close.
    assert alert_scheduler._market_hours(datetime(2026, 7, 6, 16, 30, tzinfo=ET)) is False
    # Saturday 11:00 ET — weekend.
    assert alert_scheduler._market_hours(datetime(2026, 7, 11, 11, 0, tzinfo=ET)) is False


def test_enabled_toggle(monkeypatch):
    monkeypatch.setenv("CFM_HOT_REFRESH", "0")
    assert refresh_policy.enabled() is False
    monkeypatch.delenv("CFM_HOT_REFRESH", raising=False)
    assert refresh_policy.enabled() is True  # on by default


def test_peek_cached_is_read_only_and_age_bounded(monkeypatch):
    import time
    screening.clear_cache()
    assert screening.peek_cached("k") is None          # miss → None, never computes
    screening._results["k"] = (time.time() - 100, {"v": 1})
    assert screening.peek_cached("k") == {"v": 1}       # present → returned
    assert screening.peek_cached("k", max_age=50) is None   # too stale for the bound
    assert screening.peek_cached("k", max_age=200) == {"v": 1}
    screening.clear_cache()


def test_status_reports_shape(monkeypatch):
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: ["ON"])
    monkeypatch.setattr(screening, "peek_cached", lambda key, max_age=None: None)
    st = refresh_policy.status()
    assert st["cadence_minutes"] == config.HOT_REFRESH_MINUTES
    assert st["tickers"] == ["ON"] and st["count"] == 1
    assert st["last_refresh"] is None  # nothing refreshed yet
