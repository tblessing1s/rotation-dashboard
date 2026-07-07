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
    # Never touch a real quote provider; individual tests override as needed.
    monkeypatch.setattr(data_handler, "live_prices", lambda syms: {})
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


def test_refresh_tickers_forces_ticker_spy_and_sector_etf(_reset, monkeypatch):
    import sector_data
    from metrics import scorecard as scorecard_metrics
    monkeypatch.setattr(sector_data, "sector_for",
                        lambda t: None if t.upper() == "SPY" else "XLK")
    seen = {}

    def fake_scorecard(names, price_overrides=None):
        seen["names"] = list(names)
        return {"as_of": "2026-07-07T14:00:00Z",
                "results": [{"ticker": n, "price": 100.0} for n in names]}

    monkeypatch.setattr(scorecard_metrics, "scorecard", fake_scorecard)

    out = refresh_policy.refresh_tickers(["nvda"])
    syms, force = _reset["prefetch"][0]
    assert force is True                                   # bypasses the daily window
    assert {"NVDA", config.BENCHMARK, "XLK"} <= set(syms)  # ticker + SPY + its sector ETF
    assert out["tickers"] == ["NVDA"] and out["count"] == 1
    assert out["rows"][0]["ticker"] == "NVDA"
    assert out["as_of"] == "2026-07-07T14:00:00Z"
    assert seen["names"] == ["NVDA"]                       # scored only the requested name


def test_refresh_tickers_overlays_live_quote_and_tags_source(_reset, monkeypatch):
    import sector_data
    from metrics import scorecard as scorecard_metrics
    monkeypatch.setattr(sector_data, "sector_for", lambda t: None if t.upper() == "SPY" else "XLK")
    # The live quote (179.18) must be overlaid as the price, not the daily close.
    monkeypatch.setattr(data_handler, "live_prices",
                        lambda syms: {"NVDA": {"price": 179.18, "source": "schwab"}})
    seen = {}

    def fake(names, price_overrides=None):
        seen["overrides"] = price_overrides
        return {"as_of": "t",
                "results": [{"ticker": n, "price": (price_overrides or {}).get(n)} for n in names]}

    monkeypatch.setattr(scorecard_metrics, "scorecard", fake)

    out = refresh_policy.refresh_tickers(["nvda"])
    assert seen["overrides"] == {"NVDA": 179.18}           # live quote threaded to the scorecard
    assert out["rows"][0]["price"] == 179.18               # row shows the live price
    assert out["rows"][0]["price_source"] == "schwab"      # provenance tagged
    assert out["quote_sources"] == ["schwab"]


def test_refresh_tickers_flags_a_cache_fallback_source(_reset, monkeypatch):
    import sector_data
    from metrics import scorecard as scorecard_metrics
    monkeypatch.setattr(sector_data, "sector_for", lambda t: None)
    # Providers didn't answer -> live_prices degraded to the cached close.
    monkeypatch.setattr(data_handler, "live_prices",
                        lambda syms: {"NVDA": {"price": 183.57, "source": "cache"}})
    monkeypatch.setattr(scorecard_metrics, "scorecard",
                        lambda names, price_overrides=None: {"as_of": "t",
                        "results": [{"ticker": n} for n in names]})
    out = refresh_policy.refresh_tickers(["nvda"])
    assert out["rows"][0]["price_source"] == "cache"       # UI can flag it amber, not "live"
    assert out["quote_sources"] == ["cache"]


def test_refresh_tickers_dedupes_uppercases_and_skips_blanks(_reset, monkeypatch):
    import sector_data
    from metrics import scorecard as scorecard_metrics
    monkeypatch.setattr(sector_data, "sector_for", lambda t: None)
    monkeypatch.setattr(scorecard_metrics, "scorecard",
                        lambda names, price_overrides=None: {"as_of": "x",
                        "results": [{"ticker": n} for n in names]})
    out = refresh_policy.refresh_tickers([" nvda ", "NVDA", "on", ""])
    assert out["tickers"] == ["NVDA", "ON"]


def test_refresh_tickers_empty_is_a_noop(_reset):
    out = refresh_policy.refresh_tickers(["", "   "])
    assert out == {"tickers": [], "rows": [], "count": 0, "as_of": None, "quote_sources": []}
    assert _reset["prefetch"] == []                        # nothing fetched


def test_api_refresh_ticker_and_sector(_reset, monkeypatch):
    import sector_data
    from metrics import scorecard as scorecard_metrics
    monkeypatch.setattr(sector_data, "sector_for",
                        lambda t: None if t.upper() == "SPY" else "XLK")
    monkeypatch.setattr(sector_data, "sector_etfs", lambda: ["XLK", "XLP"])
    monkeypatch.setattr(sector_data, "constituents",
                        lambda e: ["NVDA", "AVGO"] if e.upper() == "XLK" else [])
    monkeypatch.setattr(scorecard_metrics, "scorecard",
                        lambda names, price_overrides=None: {"as_of": "t",
                        "results": [{"ticker": n} for n in names]})
    import app as app_module
    client = app_module.app.test_client()

    r = client.post("/api/refresh/ticker", json={"ticker": "nvda"})
    assert r.status_code == 200
    assert r.get_json()["tickers"] == ["NVDA"]

    r2 = client.post("/api/refresh/sector", json={"sector": "xlk"})
    assert r2.status_code == 200
    assert set(r2.get_json()["tickers"]) == {"XLK", "NVDA", "AVGO"}  # ETF + constituents

    assert client.post("/api/refresh/ticker", json={}).status_code == 400          # ticker required
    assert client.post("/api/refresh/sector", json={"sector": "NOPE"}).status_code == 400  # unknown


def test_status_reports_shape(monkeypatch):
    monkeypatch.setattr(maintenance, "open_tickers", lambda state=None: ["ON"])
    monkeypatch.setattr(screening, "peek_cached", lambda key, max_age=None: None)
    st = refresh_policy.status()
    assert st["cadence_minutes"] == config.HOT_REFRESH_MINUTES
    assert st["tickers"] == ["ON"] and st["count"] == 1
    assert st["last_refresh"] is None  # nothing refreshed yet
