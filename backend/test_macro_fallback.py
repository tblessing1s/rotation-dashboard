"""Level 1 macro resilience: Alpha Vantage backs up FRED when it fails."""
import pandas as pd
import pytest

import ingest
from providers import alphavantage, fred
from providers.base import ProviderError


def _series():
    return pd.Series([4.0, 4.1], index=pd.to_datetime(["2026-04-01", "2026-05-01"]))


def test_fetch_macro_prefers_fred(monkeypatch):
    monkeypatch.setattr(fred, "fetch_series", lambda sid, **k: _series())
    series, source = ingest.fetch_macro_series("UNRATE")
    assert source == "fred"
    assert float(series.iloc[-1]) == 4.1


def test_fetch_macro_falls_back_to_alphavantage(monkeypatch):
    monkeypatch.setattr(fred, "fetch_series", lambda *a, **k: (_ for _ in ()).throw(ProviderError("403")))
    monkeypatch.setattr(alphavantage, "configured", lambda: True)
    monkeypatch.setattr(alphavantage, "economic_series", lambda sid, **k: _series())
    series, source = ingest.fetch_macro_series("UNRATE")
    assert source == "alphavantage"
    assert float(series.iloc[-1]) == 4.1


def test_fetch_macro_raises_when_both_fail(monkeypatch):
    monkeypatch.setattr(fred, "fetch_series", lambda *a, **k: (_ for _ in ()).throw(ProviderError("fred down")))
    monkeypatch.setattr(alphavantage, "configured", lambda: True)
    monkeypatch.setattr(alphavantage, "economic_series", lambda *a, **k: (_ for _ in ()).throw(ProviderError("av down")))
    with pytest.raises(ProviderError, match="Alpha Vantage fallback failed"):
        ingest.fetch_macro_series("UNRATE")


def test_fetch_macro_reraises_fred_error_without_key(monkeypatch):
    monkeypatch.setattr(fred, "fetch_series", lambda *a, **k: (_ for _ in ()).throw(ProviderError("fred down")))
    monkeypatch.setattr(alphavantage, "configured", lambda: False)
    with pytest.raises(ProviderError, match="fred down"):
        ingest.fetch_macro_series("UNRATE")
