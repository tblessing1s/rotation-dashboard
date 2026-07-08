"""Transport layer: batched quotes (one request per cycle), 429 backoff, provider
failover with a degraded-data flag, budget accounting, the shed ladder, and
defense-level derivation. No real HTTP, no real sleep — providers and the clock
are injected."""
import numpy as np
import pandas as pd
import pytest

import config
import data_budget
import data_cache
import data_transport as dt
import market_scheduler as ms
from market_scheduler import QUOTE, Tier


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(data_budget, "PATH", str(tmp_path / "budget.json"))
    data_budget.reset(day="2026-07-08")
    data_cache.reset()
    # Providers configured by default; individual tests override the wrappers.
    monkeypatch.setattr(dt, "_schwab_configured", lambda: True)
    monkeypatch.setattr(dt, "_av_configured", lambda: True)
    yield
    data_budget.reset(day="2026-07-08")
    data_cache.reset()


def _node(price):
    return {"last": price, "mark": price, "close": price}


# ---- 6. Batching: N symbols -> exactly one Schwab request ------------------

def test_batched_quotes_single_request(monkeypatch):
    calls = {"n": 0, "syms": None}

    def fake_batch(syms):
        calls["n"] += 1
        calls["syms"] = list(syms)
        return {s: _node(100.0 + i) for i, s in enumerate(syms)}

    monkeypatch.setattr(dt, "_schwab_batch", fake_batch)
    due = {"AAPL": Tier.T0, "MSFT": Tier.T0, "NVDA": Tier.T1}
    res = dt.fetch_quotes_batched(due, fetched_at=1_000_000.0, day="2026-07-08")

    assert calls["n"] == 1                          # exactly ONE request
    assert set(calls["syms"]) == {"AAPL", "MSFT", "NVDA"}
    assert res["resolved"] == 3 and res["degraded"] == []
    # one schwab quote call attributed to the top tier present (T0)
    assert data_budget.counts("2026-07-08")["schwab"]["0"][QUOTE] == 1
    # live quotes recorded fresh in the staleness store
    _, age, is_stale = data_cache.get_with_staleness("AAPL", QUOTE, tier=Tier.T0, now=1_000_000.0)
    assert age == 0.0 and is_stale is False


# ---- 8. Provider failover surfaces a degraded flag -------------------------

def test_tier0_schwab_failure_routes_to_fallback_and_flags(monkeypatch):
    def boom(syms):
        raise RuntimeError("schwab down")

    monkeypatch.setattr(dt, "_schwab_batch", boom)
    monkeypatch.setattr(dt, "_av_quote", lambda s: {"last": 205.0})
    res = dt.fetch_quotes_batched({"AAPL": Tier.T0}, fetched_at=1_000_000.0, day="2026-07-08")

    assert res["quotes"]["AAPL"]["source"] == "alphavantage"
    assert res["tier0_degraded"] and res["tier0_degraded"][0]["symbol"] == "AAPL"
    # AV was billed; fallback value recorded with provider identity
    assert data_budget.counts("2026-07-08")["alphavantage"]["0"][QUOTE] == 1
    assert data_cache.record("AAPL", QUOTE)["provider"] == "alphavantage"


def test_cache_fallback_not_marked_fresh(monkeypatch):
    monkeypatch.setattr(dt, "_schwab_batch", lambda syms: {s: None for s in syms})  # no live nodes
    monkeypatch.setattr(dt, "_av_configured", lambda: False)
    monkeypatch.setattr(dt, "_cached_close", lambda s: 199.0)
    res = dt.fetch_quotes_batched({"AAPL": Tier.T0}, fetched_at=1_000_000.0, day="2026-07-08")

    assert res["quotes"]["AAPL"]["source"] == "cache"
    assert res["tier0_degraded"]                       # surfaced, not silent
    # a cache fallback must NOT create a fresh staleness record
    assert data_cache.record("AAPL", QUOTE) is None
    _, age, is_stale = data_cache.get_with_staleness("AAPL", QUOTE, tier=Tier.T0, now=1_000_000.0)
    assert age is None and is_stale is True


def test_partial_schwab_then_av_for_remainder(monkeypatch):
    monkeypatch.setattr(dt, "_schwab_batch",
                        lambda syms: {"AAPL": _node(100.0), "MSFT": None})
    monkeypatch.setattr(dt, "_av_quote", lambda s: {"last": 50.0})
    res = dt.fetch_quotes_batched({"AAPL": Tier.T0, "MSFT": Tier.T1},
                                  fetched_at=1_000_000.0, day="2026-07-08")
    assert res["quotes"]["AAPL"]["source"] == "schwab"
    assert res["quotes"]["MSFT"]["source"] == "alphavantage"
    assert {d["symbol"] for d in res["degraded"]} == {"MSFT"}   # AAPL not degraded


# ---- 429 backoff -----------------------------------------------------------

def test_429_backoff_retries_then_succeeds(monkeypatch):
    attempts = {"n": 0}
    slept = []

    def flaky(syms):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("schwab quotes: HTTP 429 too many requests")
        return {s: _node(100.0) for s in syms}

    monkeypatch.setattr(dt, "_schwab_batch", flaky)
    res = dt.fetch_quotes_batched({"AAPL": Tier.T0}, fetched_at=1.0,
                                  sleep=lambda s: slept.append(s), day="2026-07-08")
    assert res["quotes"]["AAPL"]["source"] == "schwab"
    assert attempts["n"] == 3
    assert slept == [config.SCHWAB_BACKOFF_BASE_SECONDS,
                     config.SCHWAB_BACKOFF_BASE_SECONDS * 2]   # exponential
    # each HTTP attempt billed
    assert data_budget.counts("2026-07-08")["schwab"]["0"][QUOTE] == 3


def test_429_exhausts_then_fails_over(monkeypatch):
    slept = []
    monkeypatch.setattr(dt, "_schwab_batch",
                        lambda syms: (_ for _ in ()).throw(RuntimeError("HTTP 429")))
    monkeypatch.setattr(dt, "_av_quote", lambda s: {"last": 7.0})
    res = dt.fetch_quotes_batched({"AAPL": Tier.T0}, fetched_at=1.0,
                                  sleep=lambda s: slept.append(s), day="2026-07-08")
    assert res["quotes"]["AAPL"]["source"] == "alphavantage"
    assert len(slept) == config.SCHWAB_MAX_RETRIES - 1          # backed off between every attempt
    assert data_budget.counts("2026-07-08")["schwab"]["0"][QUOTE] == config.SCHWAB_MAX_RETRIES


def test_non_429_error_does_not_retry(monkeypatch):
    attempts = {"n": 0}

    def down(syms):
        attempts["n"] += 1
        raise RuntimeError("HTTP 500 server error")

    monkeypatch.setattr(dt, "_schwab_batch", down)
    monkeypatch.setattr(dt, "_av_quote", lambda s: {"last": 7.0})
    dt.fetch_quotes_batched({"AAPL": Tier.T0}, fetched_at=1.0,
                            sleep=lambda s: None, day="2026-07-08")
    assert attempts["n"] == 1                                   # no retry on a non-rate-limit error


# ---- 7. Shed ladder: T3 before T2 before T1; T0 untouched ------------------

def _load_provider_to_pct(pct, provider="schwab", day="2026-07-08"):
    limit = data_budget.provider_limit(provider)
    n = int(limit * pct / 100.0)
    data_budget.record(provider, Tier.T0, QUOTE, n=n, day=day)


def test_shed_order_strict(monkeypatch):
    day = "2026-07-08"
    # under soft limit: nothing shed
    _load_provider_to_pct(config.BUDGET_SOFT_LIMIT_PCT - 5, day=day)
    assert data_budget.shed_level("schwab", day) == 0
    for t in (Tier.T0, Tier.T1, Tier.T2, Tier.T3):
        assert data_budget.drop_tier(t, "schwab", day) is False

    # crossing the soft limit -> Tier 3 sheds first
    data_budget.reset(day=day)
    _load_provider_to_pct(config.BUDGET_SOFT_LIMIT_PCT + 1, day=day)
    assert data_budget.shed_level("schwab", day) == 1
    assert data_budget.drop_tier(Tier.T3, "schwab", day) is True
    assert data_budget.drop_tier(Tier.T2, "schwab", day) is False
    assert data_budget.drop_tier(Tier.T0, "schwab", day) is False

    # deeper -> Tier 2 joins; Tier 1 still full cadence; Tier 0 never
    data_budget.reset(day=day)
    _load_provider_to_pct((config.BUDGET_SOFT_LIMIT_PCT + 100) / 2 + 1, day=day)
    assert data_budget.shed_level("schwab", day) == 2
    assert data_budget.drop_tier(Tier.T2, "schwab", day) is True
    assert data_budget.t1_cadence_multiplier("schwab", day) == 1.0
    assert data_budget.drop_tier(Tier.T0, "schwab", day) is False

    # at/over the hard limit -> Tier 1 cadence reduced; Tier 0 STILL never shed
    data_budget.reset(day=day)
    _load_provider_to_pct(100, day=day)
    assert data_budget.shed_level("schwab", day) == 3
    assert data_budget.t1_cadence_multiplier("schwab", day) == 2.0
    assert data_budget.drop_tier(Tier.T0, "schwab", day) is False


def test_shed_transition_logged():
    day = "2026-07-08"
    _load_provider_to_pct(config.BUDGET_SOFT_LIMIT_PCT + 1, day=day)
    data_budget.note_shed("schwab", data_budget.shed_level("schwab", day), day=day)
    snap = data_budget.snapshot(day)
    assert snap["shed_log"] and snap["shed_log"][-1]["provider"] == "schwab"
    assert snap["providers"]["schwab"]["shed"]["tier3"] is True
    assert snap["providers"]["schwab"]["shed"]["tier0"] is False


def test_tier0_never_shed_even_over_limit():
    day = "2026-07-08"
    _load_provider_to_pct(300, day=day)   # wildly over
    assert data_budget.drop_tier(Tier.T0, "schwab", day) is False
    assert data_budget.snapshot(day)["providers"]["schwab"]["shed"]["tier0"] is False


# ---- Defense-level derivation ----------------------------------------------

def _bars(closes, lows=None, highs=None):
    n = len(closes)
    lows = lows or [c - 2 for c in closes]
    highs = highs or [c + 2 for c in closes]
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"Open": closes, "High": highs, "Low": lows,
                         "Close": closes, "Volume": [1e6] * n}, index=idx)


def test_defense_levels_from_bars():
    closes = list(np.linspace(200, 185, 30))
    df = _bars(closes, lows=[c - 3 for c in closes])
    pos = {"ticker": "XLK", "short_calls": [{"strike": 190.0}, {"strike": 188.0}],
           "circuit_breaker": {"price": 170.0}}
    levels = dt.defense_levels(pos, df)
    assert levels["short_strike"] == 190.0            # highest short strike
    assert levels["circuit_breaker"] == 170.0
    assert levels["trailing_stop"] is not None and levels["trailing_stop"] < closes[-1]
    assert levels["consolidation_low"] is not None    # recent swing low derived


def test_defense_levels_none_when_underivable():
    pos = {"ticker": "AAA", "short_calls": [], "circuit_breaker": {}}
    levels = dt.defense_levels(pos, None)
    assert all(v is None for v in levels.values())


def test_atr_mult_override(monkeypatch):
    monkeypatch.setattr(config, "DEFENSE_ATR_MULT_OVERRIDES", {"APP": 1.0}, raising=False)
    assert dt._atr_mult_for("APP") == 1.0
    assert dt._atr_mult_for("XLK") == config.SHORT_ATR_MULT


def test_intraday_move_pct():
    df = _bars([100.0, 100.0, 100.0])
    assert dt.intraday_move_pct(101.0, df) == pytest.approx(1.0)
    assert dt.intraday_move_pct(None, df) == 0.0
