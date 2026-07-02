"""Tests for the CFM numeric scorecard — metric math, the composite verdict, and
the /api/scan/scorecard endpoint. Offline (no provider keys): OHLCV is mocked.
"""
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-scorecard-test-"))

from metrics import scorecard as sc  # noqa: E402
from metrics import thresholds as T  # noqa: E402


def _frame(values, vol=1e6, highs=None, lows=None):
    idx = pd.bdate_range("2023-06-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    high = c + 1 if highs is None else pd.Series(highs, index=idx, dtype=float)
    low = c - 1 if lows is None else pd.Series(lows, index=idx, dtype=float)
    v = pd.Series(vol, index=idx, dtype=float) if np.isscalar(vol) else pd.Series(vol, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": high, "Low": low, "Close": c, "Volume": v}, index=idx)


# ---- extension metrics -----------------------------------------------------
def test_pct_above_ma21_normal_and_edges():
    assert sc.pct_above_ma21(110.0, 100.0) == pytest.approx(10.0)
    assert sc.pct_above_ma21(90.0, 100.0) == pytest.approx(-10.0)   # below
    assert sc.pct_above_ma21(100.0, 0.0) is None                    # zero MA guard
    assert sc.pct_above_ma21(None, 100.0) is None


def test_pct_above_ma200_normal_and_edges():
    assert sc.pct_above_ma200(114.7, 100.0) == pytest.approx(14.7)
    assert sc.pct_above_ma200(100.0, None) is None


def test_atr_extension_units_and_zero_atr():
    # (price-ma21)/atr in ATR units: (110-100)/4 = 2.5
    assert sc.atr_extension(110.0, 100.0, 4.0) == pytest.approx(2.5)
    assert sc.atr_extension(110.0, 100.0, 0.0) is None   # never divide by zero
    assert sc.atr_extension(110.0, 100.0, None) is None
    # boundary around the AVOID threshold (3.0): 12/4 = 3.0 exactly (not > 3)
    assert sc.atr_extension(112.0, 100.0, 4.0) == pytest.approx(3.0)


# ---- weakness metrics ------------------------------------------------------
def test_below_ma50_and_ma200():
    assert sc.below_ma50(95.0, 100.0) is True
    assert sc.below_ma50(105.0, 100.0) is False
    assert sc.below_ma200(95.0, 100.0) is True
    assert sc.below_ma200(105.0, 100.0) is False
    assert sc.below_ma50(None, 100.0) is None


def test_ma50_slope_sign_and_insufficient():
    rising = pd.Series([float(i) for i in range(20)])
    assert sc.ma50_slope(rising, lookback=5) == pytest.approx(5.0)   # +1/day over 5
    falling = pd.Series([float(-i) for i in range(20)])
    assert sc.ma50_slope(falling, lookback=5) == pytest.approx(-5.0)  # rolling over
    assert sc.ma50_slope(pd.Series([1.0, 2.0]), lookback=5) is None   # too short
    assert sc.ma50_slope(None) is None


# ---- volume metrics --------------------------------------------------------
def test_volume_ratio_and_acceleration():
    assert sc.volume_ratio(900.0, 1000.0) == pytest.approx(0.9)
    assert sc.volume_ratio(900.0, 0.0) is None
    # boundary at VOLUME_RATIO_MIN (0.8): exactly 0.8 is NOT < 0.8
    assert sc.volume_ratio(800.0, 1000.0) == pytest.approx(T.VOLUME_RATIO_MIN)
    assert sc.volume_acceleration(1100.0, 1000.0) == pytest.approx(1.1)
    assert sc.volume_acceleration(1100.0, None) is None


# ---- trend / momentum ------------------------------------------------------
def test_obv_vs_ema_returns_above_and_distance():
    above, dist = sc.obv_vs_ema(1100.0, 1000.0)
    assert above is True and dist == pytest.approx(10.0)
    below, dist2 = sc.obv_vs_ema(900.0, 1000.0)
    assert below is False and dist2 == pytest.approx(-10.0)
    # negative EMA: distance uses |ema| so the sign reflects direction only
    above3, dist3 = sc.obv_vs_ema(-900.0, -1000.0)
    assert above3 is True and dist3 == pytest.approx(10.0)
    assert sc.obv_vs_ema(100.0, 0.0) == (None, None)


def test_mfi_standard_formula_against_known_value():
    # Hand-computed 3-period MFI. Typical price = close here (high=low=close).
    # closes: 10,11,12,11 ; vol all 100 -> raw_mf = tp*100.
    # diffs: +,+,-  -> over last 3 bars: pos = 11*100 + 12*100 = 2300; neg = 11*100 = 1100
    # ratio = 2300/1100; MFI = 100 - 100/(1+2300/1100) = 100 - 100/(3400/1100)
    df = _frame([10.0, 11.0, 12.0, 11.0], vol=100.0,
                highs=[10.0, 11.0, 12.0, 11.0], lows=[10.0, 11.0, 12.0, 11.0])
    val = sc.mfi(df["High"], df["Low"], df["Close"], df["Volume"], period=3)
    expected = 100.0 - 100.0 / (1.0 + 2300.0 / 1100.0)
    assert val == pytest.approx(expected)


def test_mfi_all_up_days_pins_to_100_and_insufficient_history():
    up = _frame([10.0, 11.0, 12.0, 13.0], vol=100.0,
                highs=[10.0, 11.0, 12.0, 13.0], lows=[10.0, 11.0, 12.0, 13.0])
    assert sc.mfi(up["High"], up["Low"], up["Close"], up["Volume"], period=3) == pytest.approx(100.0)
    short = _frame([10.0, 11.0])
    assert sc.mfi(short["High"], short["Low"], short["Close"], short["Volume"], period=14) is None


def test_atr_momentum_expanding_vs_contracting():
    assert sc.atr_momentum(1.2, 1.0) == pytest.approx(1.2)   # expanding (>1)
    assert sc.atr_momentum(0.8, 1.0) == pytest.approx(0.8)   # contracting (<1)
    assert sc.atr_momentum(1.0, 0.0) is None
    assert sc.atr_momentum(None, 1.0) is None


# ---- compute_inputs --------------------------------------------------------
def test_compute_inputs_matches_indicators_and_handles_empty():
    import indicators as ind
    df = _frame(100 + np.cumsum(np.random.RandomState(3).normal(0, 1, 260)))
    inp = sc.compute_inputs(df)
    # ATR scalar from the input bundle equals indicators.atr (same Wilder RMA).
    assert inp["atr"] == pytest.approx(ind.atr(df), rel=1e-9)
    assert inp["ma21"] == pytest.approx(ind.sma(df, 21))
    assert inp["ma200"] is not None  # 260 bars -> MA200 exists
    # Empty frame -> all-None bundle, no exception.
    empty = sc.compute_inputs(None)
    assert empty["price"] is None and empty["ma50_series"] is None


# ---- compute_verdict: each rule individually -------------------------------
def _clean_metrics(**over):
    """A metrics dict that is GO by default; override fields to fire rules."""
    base = {
        "rs3m_vs_spy": 8.0, "rs3m_vs_sector": 3.0, "pct_above_ma21": 2.0,
        "pct_above_ma200": 12.0, "atr_extension": 1.0, "below_ma50": False,
        "below_ma200": False, "ma50_slope": 0.2, "volume_ratio": 1.1,
        "volume_acceleration": 1.0, "obv_above_ema": True, "obv_pct_distance": 3.0,
        "mfi": 50.0, "atr_momentum": 0.9,
    }
    base.update(over)
    return base


def test_verdict_go_when_all_clean():
    assert sc.compute_verdict(_clean_metrics())["verdict"] == "GO"


def test_verdict_avoid_rules_fire_individually():
    assert sc.compute_verdict(_clean_metrics(rs3m_vs_sector=-1.1))["verdict"] == "AVOID"
    assert sc.compute_verdict(_clean_metrics(below_ma200=True))["verdict"] == "AVOID"
    assert sc.compute_verdict(_clean_metrics(atr_extension=3.5))["verdict"] == "AVOID"
    # boundary: exactly 3.0 is NOT > 3.0 -> not AVOID on that rule
    assert sc.compute_verdict(_clean_metrics(atr_extension=3.0))["verdict"] == "GO"


def test_verdict_caution_rules_fire_individually():
    for over in (
        {"mfi": 39.0}, {"mfi": 61.0}, {"volume_ratio": 0.79},
        {"atr_momentum": 1.01}, {"below_ma50": True}, {"ma50_slope": -0.1},
    ):
        v = sc.compute_verdict(_clean_metrics(**over))
        assert v["verdict"] == "CAUTION", over
    # MFI band boundaries are inclusive (40 and 60 are fine).
    assert sc.compute_verdict(_clean_metrics(mfi=40.0))["verdict"] == "GO"
    assert sc.compute_verdict(_clean_metrics(mfi=60.0))["verdict"] == "GO"


def test_verdict_avoid_dominates_and_collects_all_reasons():
    v = sc.compute_verdict(_clean_metrics(rs3m_vs_sector=-2.0, below_ma200=True,
                                          atr_extension=4.0, mfi=80.0))
    assert v["verdict"] == "AVOID"
    assert len(v["reasons"]) == 3  # all three AVOID reasons, no CAUTION ones
    assert any("sector" in r for r in v["reasons"])
    assert any("MA200" in r for r in v["reasons"])
    assert any("extension" in r for r in v["reasons"])


def test_verdict_collects_multiple_caution_reasons():
    v = sc.compute_verdict(_clean_metrics(mfi=80.0, volume_ratio=0.5, below_ma50=True))
    assert v["verdict"] == "CAUTION" and len(v["reasons"]) == 3


def test_verdict_skips_none_metrics():
    # All judgeable fields None -> no rule can fire -> GO with no reasons.
    none_metrics = {k: None for k in _clean_metrics()}
    v = sc.compute_verdict(none_metrics)
    assert v["verdict"] == "GO" and v["reasons"] == []


# ---- score_ticker gate layering --------------------------------------------
def test_score_ticker_surfaces_stock_level_gate_failure(monkeypatch):
    import data_handler
    df = _frame(100 + np.cumsum(np.random.RandomState(5).normal(0, 1, 260)))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    # Level 3 (stock beating peers) failed: cleared 2 -> first fail = 3 (stock-level).
    gate = {"verdict": "WAIT", "cleared_level": 2}
    row = sc.score_ticker("AAPL", df, "XLK", df, gate=gate)
    assert row["verdict"] == "AVOID"
    assert "entry gate level 3" in row["reasons"][0]
    # Numeric fields are still present (nothing hidden) even on a gate failure.
    assert "atr_extension" in row and "mfi" in row and row["ticker"] == "AAPL"


def test_score_ticker_market_regime_fail_does_not_blanket_avoid(monkeypatch):
    # A non-green market regime (Level 1) must NOT collapse a stock to AVOID — the
    # stock is still scored on its own merits so the table stays comparable. The
    # gate carries all four level pass flags; Levels 3 & 4 here pass.
    import data_handler
    df = _frame(100 + np.cumsum(np.random.RandomState(8).normal(0, 1, 260)))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    gate = {"verdict": "WAIT", "cleared_level": 0, "levels": [
        {"level": 1, "pass": False}, {"level": 2, "pass": True},
        {"level": 3, "pass": True}, {"level": 4, "pass": True}]}
    row = sc.score_ticker("AAPL", df, "XLK", df, gate=gate)
    # Verdict came from the scorecard rules, not a blanket gate AVOID.
    assert row["verdict"] in ("GO", "CAUTION", "AVOID")
    assert not any("entry gate" in r for r in row["reasons"])
    assert sc.compute_verdict(row)["verdict"] == row["verdict"]


def test_score_ticker_weak_sector_does_not_gate(monkeypatch):
    # Level 2 (sector strength) is NOT part of the stock verdict — only the stock's
    # own legs (L3/L4) gate. With a lagging sector but L3/L4 passing, the row is
    # scored on its own merits, not blanket-AVOID'd for its sector.
    import data_handler
    df = _frame(100 + np.cumsum(np.random.RandomState(13).normal(0, 1, 260)))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    gate = {"verdict": "WAIT", "cleared_level": 1, "levels": [
        {"level": 1, "pass": True}, {"level": 2, "pass": False},
        {"level": 3, "pass": True}, {"level": 4, "pass": True}]}
    row = sc.score_ticker("AAPL", df, "XLK", df, gate=gate)
    assert not any("entry gate" in r for r in row["reasons"])
    assert sc.compute_verdict(row)["verdict"] == row["verdict"]


def test_score_ticker_stock_level_fail_behind_regime_fail_still_avoids(monkeypatch):
    # Even when Level 1 also fails, a Level 4 (consolidating) miss is stock-level
    # and must short-circuit to AVOID — read from the level flags, not cleared.
    import data_handler
    df = _frame(100 + np.cumsum(np.random.RandomState(12).normal(0, 1, 260)))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    gate = {"verdict": "WAIT", "cleared_level": 0, "levels": [
        {"level": 1, "pass": False}, {"level": 2, "pass": True},
        {"level": 3, "pass": True}, {"level": 4, "pass": False}]}
    row = sc.score_ticker("AAPL", df, "XLK", df, gate=gate)
    assert row["verdict"] == "AVOID"
    assert "entry gate level 4" in row["reasons"][0]


def test_score_ticker_verdict_matches_displayed_values(monkeypatch):
    # The verdict must be derived from the SAME rounded numbers shown in the row,
    # so a displayed value can never silently disagree with the verdict.
    import data_handler
    df = _frame(100 + np.cumsum(np.random.RandomState(11).normal(0, 1, 260)))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    row = sc.score_ticker("AAPL", df, "XLK", df, gate={"verdict": "READY TO ENTER", "cleared_level": 4})
    # Re-running the verdict on exactly the row's displayed fields reproduces it.
    assert sc.compute_verdict(row)["verdict"] == row["verdict"]


def test_score_ticker_runs_scorecard_when_gate_passes(monkeypatch):
    import data_handler
    df = _frame(100 + np.cumsum(np.random.RandomState(6).normal(0, 1, 260)))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    gate = {"verdict": "READY TO ENTER", "cleared_level": 4}
    row = sc.score_ticker("MSFT", df, "XLK", df, gate=gate)
    assert row["verdict"] in ("GO", "CAUTION", "AVOID")
    assert row["gate_cleared_level"] == 4


# ---- integration: the endpoint ---------------------------------------------
def test_scorecard_endpoint_shape(monkeypatch):
    import app as flask_app
    import data_handler
    import screening
    from metrics import scorecard as scmod

    df = _frame(100 + np.cumsum(np.random.RandomState(9).normal(0, 1, 260)))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    # Stub the entry gate so the integration test stays offline and deterministic.
    monkeypatch.setattr(screening, "entry_gate",
                        lambda t: {"verdict": "READY TO ENTER", "cleared_level": 4})
    monkeypatch.setattr(scmod.sector_data, "sector_for", lambda t: "XLK")

    client = flask_app.app.test_client()
    res = client.get("/api/scan/scorecard?tickers=AAPL,MSFT")
    assert res.status_code == 200
    body = res.get_json()
    assert "as_of" in body and isinstance(body["results"], list)
    assert {r["ticker"] for r in body["results"]} == {"AAPL", "MSFT"}
    row = body["results"][0]
    # Every verdict-driving field is present in the row (no hidden state).
    for field in ("ticker", "sector", "price", "rs3m_vs_spy", "rs3m_vs_sector",
                  "pct_above_ma21", "pct_above_ma200", "atr_extension", "below_ma50",
                  "below_ma200", "ma50_slope", "volume_ratio", "volume_acceleration",
                  "obv_above_ema", "obv_pct_distance", "mfi", "atr_momentum",
                  "has_weeklies", "verdict", "reasons"):
        assert field in row, field
    # Response is JSON-serializable end to end (no numpy types leaked).
    import json
    json.dumps(body)


# ---- full-universe caching (avoids a redundant sweep when the Scan tab's
# Scorecard panel and Ready-to-Enter panel both request it concurrently) ------
@pytest.fixture()
def _reset_scan_cache():
    import screening
    screening.clear_cache()
    yield
    screening.clear_cache()  # never leak a cached full sweep into later tests


def test_scorecard_full_universe_is_cached_ticker_subset_is_not(monkeypatch, _reset_scan_cache):
    calls = []
    monkeypatch.setattr(sc, "_compute_scorecard", lambda names: calls.append(names) or {"as_of": "x", "results": []})
    monkeypatch.setattr(sc.sector_data, "all_tickers", lambda: ["AAA", "BBB"])

    sc.scorecard()          # full-universe (tickers=None) -> computes once
    sc.scorecard()          # second call within TTL -> cache hit, no recompute
    assert len(calls) == 1
    assert calls[0] == ["AAA", "BBB"]

    sc.scorecard(["ZZZ"])   # an explicit subset always computes fresh
    sc.scorecard(["ZZZ"])
    assert len(calls) == 3  # the two subset calls each added one more


def test_scorecard_cache_cleared_on_demo_live_switch(monkeypatch, _reset_scan_cache):
    import screening
    calls = []
    monkeypatch.setattr(sc, "_compute_scorecard", lambda names: calls.append(1) or {"as_of": "x", "results": []})
    monkeypatch.setattr(sc.sector_data, "all_tickers", lambda: ["AAA"])

    sc.scorecard()
    sc.scorecard()
    assert len(calls) == 1  # cached

    screening.clear_cache()  # what /api/mode calls on a demo<->live toggle
    sc.scorecard()
    assert len(calls) == 2  # recomputed after the cache was cleared
