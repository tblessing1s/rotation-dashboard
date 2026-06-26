"""End-to-end macro pipeline: realistic Alpha Vantage economic payloads ->
economic_series parsing -> Level 1 calculators. Proves every series is pulled
at the right cadence and interpreted into sane regime values, not just that the
HTTP call succeeds."""
import pandas as pd
import pytest

import macro as macro_calc
from providers import alphavantage


def _av_points(dates, values):
    """Alpha Vantage economic shape: list of {date, value:str}, newest first."""
    return {"data": [{"date": d, "value": str(v)} for d, v in zip(dates, values)][::-1]}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-key")


@pytest.fixture()
def fake_av(monkeypatch):
    """Serve realistic payloads keyed by the requested AV function."""
    # 70 daily funds-rate obs (enough for the 63-step lookback), steady at 4.33.
    rate_dates = list(pd.date_range("2026-03-01", periods=70, freq="D").strftime("%Y-%m-%d"))
    rate_vals = [4.33] * 70
    # 16 monthly CPI obs rising ~0.2%/mo -> ~2.4% YoY.
    cpi_dates = list(pd.date_range("2025-03-01", periods=16, freq="MS").strftime("%Y-%m-%d"))
    cpi_vals = [round(300 * (1.002 ** i), 3) for i in range(16)]
    # 4 quarterly real-GDP obs rising steadily -> positive growth.
    gdp_dates = list(pd.date_range("2025-06-01", periods=4, freq="QS").strftime("%Y-%m-%d"))
    gdp_vals = [22000, 22150, 22320, 22500]
    # 6 monthly unemployment obs near 4.1.
    un_dates = list(pd.date_range("2026-01-01", periods=6, freq="MS").strftime("%Y-%m-%d"))
    un_vals = [4.2, 4.2, 4.1, 4.1, 4.1, 4.0]

    by_function = {
        "FEDERAL_FUNDS_RATE": _av_points(rate_dates, rate_vals),
        "CPI": _av_points(cpi_dates, cpi_vals),
        "REAL_GDP": _av_points(gdp_dates, gdp_vals),
        "UNEMPLOYMENT": _av_points(un_dates, un_vals),
    }
    monkeypatch.setattr(alphavantage, "_get", lambda params, timeout: by_function[params["function"]])
    return by_function


def test_full_macro_pipeline_produces_sane_values(fake_av):
    rate = alphavantage.economic_series("DFF")
    cpi = alphavantage.economic_series("CPIAUCSL")
    gdp = alphavantage.economic_series("GDPC1")
    unemp = alphavantage.economic_series("UNRATE")

    # Each series arrives ascending with the expected number of observations.
    assert len(rate) == 70 and rate.index.is_monotonic_increasing
    assert len(cpi) == 16 and len(gdp) == 4 and len(unemp) == 6

    fed = macro_calc.classify_fed_policy(rate, cpi, gdp, unemp)
    assert fed["value"] in ("hawkish", "holding", "dovish")
    assert fed["rate"] == 4.33                      # latest funds rate, percent
    assert 1.0 <= fed["cpiYoY"] <= 4.0              # ~2.4% YoY
    assert fed["unemployment"] == 4.0               # latest obs
    assert fed["qoqAnnualizedGrowth"] > 0           # GDP rising

    inflation = macro_calc.inflation_from_cpi(cpi)
    assert 1.0 <= inflation["value"] <= 4.0
    assert inflation["asOf"] == "2026-06-01"        # latest CPI month

    growth = macro_calc.growth_from_gdp(gdp)
    assert growth["value"] in ("accelerating", "stable", "slowing")
    assert growth["qoqAnnualized"] > 0


def test_pipeline_handles_a_gap_in_one_series(fake_av, monkeypatch):
    """A single missing ('.') observation must not poison the calculators."""
    gappy = {"data": [
        {"date": "2026-06-01", "value": "4.0"},
        {"date": "2026-05-01", "value": "."},
        {"date": "2026-04-01", "value": "4.1"},
        {"date": "2026-03-01", "value": "4.2"},
        {"date": "2026-02-01", "value": "4.2"},
        {"date": "2026-01-01", "value": "4.3"},
    ]}
    monkeypatch.setattr(alphavantage, "_get", lambda params, timeout: gappy)
    unemp = alphavantage.economic_series("UNRATE")
    assert "." not in [str(v) for v in unemp.values]
    assert float(unemp.iloc[-1]) == 4.0
