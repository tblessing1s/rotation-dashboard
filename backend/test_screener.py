"""Unit tests for the screener's pure enrich/filter/build logic (no network)."""
import numpy as np
import pandas as pd

import screener


def _bars(n=40, price=50.0, vol=15_000_000, atr_frac=0.06):
    """Synthetic daily bars with a roughly constant ATR% and average volume."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    close = pd.Series(np.full(n, price), index=idx, dtype=float)
    rng = price * atr_frac
    high = close + rng / 2
    low = close - rng / 2
    vols = pd.Series(np.full(n, vol), index=idx, dtype=float)
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vols})


def test_enrich_computes_price_volume_atr():
    row = screener.enrich("AAA", _bars(price=50.0, vol=15_000_000, atr_frac=0.06))
    assert row["symbol"] == "AAA"
    assert row["price"] == 50.0
    assert row["avgVol"] == 15_000_000
    # ATR% ≈ range / price = 6% (flat closes => TR == high-low == 6% of price).
    assert 5.5 <= row["atrPct"] <= 6.5
    assert row["rvol"] == 1.0  # last vol == 20-day average


def test_enrich_returns_none_for_short_history():
    assert screener.enrich("AAA", _bars(n=10)) is None
    assert screener.enrich("AAA", None) is None


def test_filter_rows_applies_all_bounds_and_sorts():
    rows = [
        {"symbol": "IN", "price": 50, "atrPct": 6.0, "avgVol": 15_000_000},
        {"symbol": "HOT", "price": 80, "atrPct": 8.5, "avgVol": 30_000_000},
        {"symbol": "PRICEY", "price": 250, "atrPct": 7.0, "avgVol": 20_000_000},
        {"symbol": "QUIET", "price": 40, "atrPct": 2.0, "avgVol": 12_000_000},
        {"symbol": "THIN", "price": 45, "atrPct": 6.0, "avgVol": 1_000_000},
    ]
    out = screener.filter_rows(rows, price_min=20, price_max=100, vol_min=10_000_000,
                               atr_min=4, atr_max=9, limit=50)
    # PRICEY (price), QUIET (atr), THIN (volume) all excluded; HOT sorts first.
    assert [r["symbol"] for r in out] == ["HOT", "IN"]


def test_filter_rows_respects_limit():
    rows = [{"symbol": f"S{i}", "price": 50, "atrPct": 4 + i * 0.1, "avgVol": 15_000_000}
            for i in range(10)]
    out = screener.filter_rows(rows, 20, 100, 10_000_000, 4, 9, limit=3)
    assert len(out) == 3
    assert out[0]["atrPct"] >= out[1]["atrPct"] >= out[2]["atrPct"]


def test_build_rows_collects_errors_without_raising():
    def fetch(sym):
        if sym == "BAD":
            raise RuntimeError("boom")
        return _bars()

    rows, errors = screener.build_rows(["AAA", "BAD", "BBB"], fetch)
    assert {r["symbol"] for r in rows} == {"AAA", "BBB"}
    assert "BAD" in errors


def test_universe_dedupes_and_includes_movers():
    uni = screener.universe(movers=["aaa", "AAA", "ZZZZ"])
    assert uni.count("AAA") == 1
    assert "ZZZZ" in uni
    # Curated universe names are present.
    assert "SPY" in uni
