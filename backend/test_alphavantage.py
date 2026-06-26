"""Alpha Vantage provider parsing tests (HTTP layer mocked)."""
import pandas as pd
import pytest

from providers import alphavantage
from providers.base import ProviderError


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-key")


def test_configured_reflects_env(monkeypatch):
    assert alphavantage.configured() is True
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY")
    assert alphavantage.configured() is False


def test_daily_bars_parses_and_sorts(monkeypatch):
    payload = {
        "Time Series (Daily)": {
            "2026-06-15": {"1. open": "10", "2. high": "12", "3. low": "9", "4. close": "11", "5. volume": "1000"},
            "2026-06-13": {"1. open": "8", "2. high": "9", "3. low": "7", "4. close": "8.5", "5. volume": "900"},
        }
    }
    monkeypatch.setattr(alphavantage, "_get", lambda *a, **k: payload)
    df = alphavantage.daily_bars("AAA")
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index[0] < df.index[-1]  # ascending
    assert float(df["Close"].iloc[-1]) == 11.0
    assert float(df["Volume"].iloc[-1]) == 1000.0


def test_get_raises_on_rate_limit_envelope(monkeypatch):
    # Alpha Vantage returns soft errors as HTTP 200 with a "Note"/"Information".
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"Information": "rate limit reached"}'

    monkeypatch.setattr(alphavantage, "urlopen", lambda *a, **k: FakeResp())
    with pytest.raises(ProviderError, match="rate limit"):
        alphavantage._get({"function": "X"}, timeout=5)


def test_top_movers_returns_lists(monkeypatch):
    payload = {
        "most_actively_traded": [{"ticker": "aaa"}, {"ticker": "BBB"}],
        "top_gainers": [{"ticker": "ccc"}],
        "top_losers": [],
    }
    monkeypatch.setattr(alphavantage, "_get", lambda *a, **k: payload)
    out = alphavantage.top_movers()
    assert out["most_actively_traded"] == ["AAA", "BBB"]
    assert out["top_gainers"] == ["CCC"]


def test_top_movers_degrades_to_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise ProviderError("down")
    monkeypatch.setattr(alphavantage, "_get", boom)
    out = alphavantage.top_movers()
    assert out == {"most_actively_traded": [], "top_gainers": [], "top_losers": []}


def test_economic_series_maps_and_parses(monkeypatch):
    captured = {}

    def fake_get(params, timeout):
        captured.update(params)
        return {"data": [{"date": "2026-05-01", "value": "4.1"},
                          {"date": "2026-04-01", "value": "4.0"}]}

    monkeypatch.setattr(alphavantage, "_get", fake_get)
    s = alphavantage.economic_series("UNRATE")
    assert captured["function"] == "UNEMPLOYMENT"
    assert isinstance(s, pd.Series)
    assert s.index[0] < s.index[-1]
    assert float(s.iloc[-1]) == 4.1


def test_economic_series_rejects_unknown_series():
    with pytest.raises(ProviderError, match="no mapping"):
        alphavantage.economic_series("NOPE")


@pytest.mark.parametrize("series_id,function,interval", [
    ("DFF", "FEDERAL_FUNDS_RATE", "daily"),
    ("CPIAUCSL", "CPI", "monthly"),
    ("GDPC1", "REAL_GDP", "quarterly"),
    ("UNRATE", "UNEMPLOYMENT", None),
])
def test_every_macro_series_hits_the_right_endpoint(monkeypatch, series_id, function, interval):
    """Each FRED id the dashboard ingests must map to the correct Alpha Vantage
    economic function at the cadence the calculators assume."""
    captured = {}

    def fake_get(params, timeout):
        captured.update(params)
        return {"data": [{"date": "2026-05-01", "value": "2.0"},
                         {"date": "2026-04-01", "value": "1.0"}]}

    monkeypatch.setattr(alphavantage, "_get", fake_get)
    s = alphavantage.economic_series(series_id)
    assert captured["function"] == function
    assert captured.get("interval") == interval  # None for UNEMPLOYMENT
    assert s.index[0] < s.index[-1]  # ascending despite newest-first payload
    assert float(s.iloc[-1]) == 2.0


def test_economic_series_drops_missing_dot_sentinel(monkeypatch):
    """Alpha Vantage emits '.' for a missing observation; it must be dropped,
    not coerced to a number, so the calculators never see a bogus value."""
    monkeypatch.setattr(alphavantage, "_get", lambda *a, **k: {"data": [
        {"date": "2026-05-01", "value": "4.2"},
        {"date": "2026-04-01", "value": "."},
        {"date": "2026-03-01", "value": "4.0"},
    ]})
    s = alphavantage.economic_series("UNRATE")
    assert list(s.values) == [4.0, 4.2]  # the '.' row is gone, order ascending


def test_economic_series_raises_when_all_observations_missing(monkeypatch):
    monkeypatch.setattr(alphavantage, "_get", lambda *a, **k: {"data": [
        {"date": "2026-05-01", "value": "."},
    ]})
    with pytest.raises(ProviderError, match="no numeric observations"):
        alphavantage.economic_series("CPIAUCSL")
