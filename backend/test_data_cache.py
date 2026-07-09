"""Staleness cache: get_with_staleness, the STALE_BLOCKS_GO hard rule, and the
staleness surfaced to the frontend. No wall-clock — every check passes an explicit
epoch ``now``."""
import time

import pytest

import config
import data_cache
import market_scheduler as ms
from market_scheduler import BARS, QUOTE, Tier

T0_QUOTE_MAXAGE = config.POLL_T0_SECONDS * config.MAX_AGE_POLL_MULT


@pytest.fixture(autouse=True)
def _reset():
    data_cache.reset()
    yield
    data_cache.reset()


def test_fresh_quote_not_stale():
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 200.0, "schwab", Tier.T0, fetched_at=now - 10)
    value, age, is_stale = data_cache.get_with_staleness("AAPL", QUOTE, tier=Tier.T0, now=now)
    assert value == 200.0 and age == 10 and is_stale is False


def test_stale_quote_beyond_maxage():
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 200.0, "schwab", Tier.T0, fetched_at=now - (T0_QUOTE_MAXAGE + 5))
    _, age, is_stale = data_cache.get_with_staleness("AAPL", QUOTE, tier=Tier.T0, now=now)
    assert age > T0_QUOTE_MAXAGE and is_stale is True


def test_unknown_datum_is_stale():
    value, age, is_stale = data_cache.get_with_staleness("ZZZ", QUOTE, tier=Tier.T0, now=1.0)
    assert value is None and age is None and is_stale is True   # unknown-fresh blocks


def test_bars_fallback_to_parquet_mtime(monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: 40.0)  # > 30h tolerance
    _, age, is_stale = data_cache.get_with_staleness("AAPL", BARS, tier=Tier.T0, now=time.time())
    assert age == 40.0 * 3600.0 and is_stale is True
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: 2.0)
    _, age, is_stale = data_cache.get_with_staleness("AAPL", BARS, tier=Tier.T0, now=time.time())
    assert is_stale is False


# ---- 5. GO blocked when any input is stale ---------------------------------

def test_go_not_blocked_when_all_fresh():
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 200.0, "schwab", Tier.T1, fetched_at=now - 10)
    data_cache.put("AAPL", BARS, "df", "schwab", Tier.T1, fetched_at=now - 60)
    blocked, stale = data_cache.stale_blocks_go("AAPL", Tier.T1, now=now,
                                                market_open=True, live=True)
    assert blocked is False and stale == []


def test_go_blocked_when_quote_stale():
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 200.0, "schwab", Tier.T1,
                   fetched_at=now - (config.POLL_T1_SECONDS * config.MAX_AGE_POLL_MULT + 5))
    data_cache.put("AAPL", BARS, "df", "schwab", Tier.T1, fetched_at=now - 60)
    blocked, stale = data_cache.stale_blocks_go("AAPL", Tier.T1, now=now,
                                                market_open=True, live=True)
    assert blocked is True
    assert any(s["kind"] == QUOTE and s["reason"] == "stale" for s in stale)


def test_go_blocked_when_input_missing_live_and_open():
    # no records at all: live + open => unknown-fresh blocks GO
    blocked, stale = data_cache.stale_blocks_go("AAPL", Tier.T1, now=1.0,
                                                market_open=True, live=True)
    assert blocked is True and {s["kind"] for s in stale} == {QUOTE, BARS}


def test_missing_input_does_not_block_offline():
    # offline/warm-scan context (not live, or market closed) => absent records
    # don't block, so the existing scan pipeline and tests behave normally.
    blocked, _ = data_cache.stale_blocks_go("AAPL", Tier.T1, now=1.0,
                                            market_open=False, live=False)
    assert blocked is False


def test_stale_record_blocks_even_offline():
    # a record that EXISTS and is stale blocks regardless of live/market flags.
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 1.0, "schwab", Tier.T0, fetched_at=now - 10)
    data_cache.put("AAPL", BARS, "df", "schwab", Tier.T0,
                   fetched_at=now - (config.EOD_MAX_AGE_HOURS * 3600.0 + 100))
    blocked, stale = data_cache.stale_blocks_go("AAPL", Tier.T0, now=now,
                                                market_open=False, live=False)
    assert blocked is True and any(s["kind"] == BARS for s in stale)


def test_go_not_blocked_when_bars_have_no_record_but_fresh_parquet(monkeypatch):
    # Bars are never written to the staleness store in production — a missing bars
    # RECORD is normal. With a fresh parquet cache the GO must NOT be blocked, even
    # live + open. (Regression: previously every GO was held on "no fresh data" bars.)
    import data_handler
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: 2.0)  # fresh parquet
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 200.0, "schwab", Tier.T1, fetched_at=now - 10)
    blocked, stale = data_cache.stale_blocks_go("AAPL", Tier.T1, now=now,
                                                market_open=True, live=True)
    assert blocked is False and stale == []


def test_go_blocked_when_bars_parquet_stale(monkeypatch):
    # No bars record, but the parquet cache is older than the EOD tolerance -> the
    # parquet-derived staleness blocks (and is labelled with its real age).
    import data_handler
    monkeypatch.setattr(data_handler, "cache_age_hours", lambda s: 40.0)  # > 30h
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 200.0, "schwab", Tier.T1, fetched_at=now - 10)
    blocked, stale = data_cache.stale_blocks_go("AAPL", Tier.T1, now=now,
                                                market_open=True, live=True)
    assert blocked is True
    bars = [s for s in stale if s["kind"] == BARS]
    assert bars and bars[0]["reason"] == "stale" and bars[0]["age_seconds"] == 40.0 * 3600.0


def test_rule_disabled_never_blocks(monkeypatch):
    monkeypatch.setattr(config, "STALE_BLOCKS_GO", False)
    blocked, stale = data_cache.stale_blocks_go("AAPL", Tier.T1, now=1.0,
                                                market_open=True, live=True)
    assert blocked is False and stale == []


# ---- staleness surfaced to the frontend ------------------------------------

def test_panel_staleness_flags_offender():
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 1.0, "schwab", Tier.T0, fetched_at=now - 5)
    data_cache.put("MSFT", QUOTE, 1.0, "alphavantage", Tier.T0,
                   fetched_at=now - (T0_QUOTE_MAXAGE + 10))
    res = data_cache.panel_staleness(["AAPL", "MSFT"], tiers={"AAPL": Tier.T0, "MSFT": Tier.T0},
                                     kinds=(QUOTE,), now=now)
    assert res["stale"] is True
    assert [o["symbol"] for o in res["offenders"]] == ["MSFT"]
    assert res["offenders"][0]["provider"] == "alphavantage"


def test_summary_counts_stale():
    now = 1_000_000.0
    data_cache.put("AAPL", QUOTE, 1.0, "schwab", Tier.T0, fetched_at=now - 5)
    data_cache.put("MSFT", QUOTE, 1.0, "schwab", Tier.T0, fetched_at=now - (T0_QUOTE_MAXAGE + 10))
    s = data_cache.summary(now=now)
    assert s["count"] == 2 and s["stale_count"] == 1
    assert s["stale"][0]["symbol"] == "MSFT"
