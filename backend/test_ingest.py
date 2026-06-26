"""End-to-end ingestion: providers -> validation -> datastore -> snapshots ->
API. The crucial properties under test:

  1. the request path serves entirely from the datastore (providers can be on
     fire and the API still answers with the last good values),
  2. garbage bars are quarantined and never overwrite good data,
  3. manual overrides beat ingested values and survive refreshes.
"""
import json

import numpy as np
import pandas as pd
import pytest

import app as app_mod
import config as cfg
import db
import ingest
from providers import alphavantage
from providers.base import Provider, ProviderError


def clean_frame(symbol, periods=140, base=100.0):
    rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
    idx = pd.bdate_range(end="2026-06-10", periods=periods)
    close = pd.Series(base * np.cumprod(1 + rng.normal(0.0004, 0.008, periods)), index=idx)
    return pd.DataFrame({
        "Open": close * 0.998, "High": close * 1.012, "Low": close * 0.99,
        "Close": close, "Volume": rng.integers(1_000_000, 5_000_000, periods).astype(float),
    })


class FakeProvider(Provider):
    name = "schwab"

    def __init__(self, garbage_for=(), empty_for=()):
        self.garbage_for = set(garbage_for)
        self.empty_for = set(empty_for)
        self.calls = 0

    def get_daily_bars(self, symbol, start):
        self.calls += 1
        if symbol in self.empty_for:
            raise ProviderError(f"{symbol}: empty response")
        df = clean_frame(symbol)
        if symbol in self.garbage_for:
            df.iloc[-1, df.columns.get_loc("Close")] = df["Close"].iloc[-2] * 9  # absurd spike
        return df


class FakeYahoo(FakeProvider):
    name = "yahoo"


@pytest.fixture()
def env(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    monkeypatch.setattr(ingest, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(alphavantage, "configured", lambda: True)
    monkeypatch.setattr(alphavantage, "economic_series", lambda sid: fake_fred(sid))
    monkeypatch.setattr(ingest, "is_stale", lambda *a, **k: False)  # no catch-up thread
    return monkeypatch


def fake_fred(series_id):
    if series_id == "GDPC1":
        idx = pd.date_range("2020-01-01", periods=25, freq="QS")
        return pd.Series(np.linspace(20000, 23000, 25), index=idx)
    idx = pd.date_range("2021-01-01", periods=64, freq="MS")
    if series_id == "CPIAUCSL":
        return pd.Series(300 * (1.0025 ** np.arange(64)), index=idx)
    if series_id == "UNRATE":
        return pd.Series(4.1, index=idx)
    return pd.Series(4.33, index=idx)  # DFF


def run_with(chain, monkeypatch, **kw):
    monkeypatch.setattr(ingest, "build_chain", lambda: list(chain))
    return ingest.run(trigger="test", **kw)


def test_full_cycle_writes_bars_snapshots_and_macro(env):
    result = run_with([FakeProvider()], env)
    assert result["status"] == "ok"
    assert result["bars"]["failed"] == {}
    assert result["indicatorSnapshots"] > 0
    snap = db.latest_snapshot("macro", "macro")
    assert set(snap["values"]) >= {"vix", "breadth", "fed", "growth", "inflation"}
    bars = db.get_bars("SPY")
    assert bars.attrs["source"] == "schwab"


def test_api_serves_from_datastore_when_providers_are_down(env):
    run_with([FakeProvider()], env)

    class OnFire(Provider):
        name = "schwab"

        def get_daily_bars(self, symbol, start):
            raise AssertionError("request path must never call a provider")

    env.setattr(ingest, "build_chain", lambda: [OnFire()])
    client = app_mod.app.test_client()

    quotes = client.get("/api/quotes").get_json()
    assert quotes["SPY"]["close"] > 0
    assert quotes["SPY"]["source"] == "schwab"
    assert "staleness" in quotes["SPY"] and "date" in quotes["SPY"]
    assert quotes["VIX"]["underlyingSymbol"] == cfg.VIX_PROXY_SYMBOL

    indicators = client.get("/api/indicators").get_json()
    xlv = indicators["XLV"]
    assert xlv["rs3m"] is not None and "staleness" in xlv

    macro = client.get("/api/macro").get_json()
    assert macro["values"]["vix"] > 0
    assert macro["fields"]["vix"]["source"] == f"schwab {cfg.VIX_PROXY_SYMBOL}"
    assert macro["staleness"] in ("fresh", "yellow", "red")


def test_garbage_bar_is_quarantined_and_last_good_value_served(env):
    run_with([FakeProvider(garbage_for={"XLV"})], env)
    client = app_mod.app.test_client()

    issues = client.get("/api/data-issues").get_json()
    reasons = [q["reason"] for q in issues["quarantine"] if q["symbol"] == "XLV"]
    assert any("moved" in r for r in reasons)

    # served close is the last GOOD bar, one session older than the rest
    quotes = client.get("/api/quotes").get_json()
    good = clean_frame("XLV")
    assert quotes["XLV"]["close"] == pytest.approx(round(good["Close"].iloc[-2], 2))
    assert quotes["XLV"]["date"] < quotes["SPY"]["date"]

    # re-running does not duplicate the quarantine entry
    before = len(issues["quarantine"])
    run_with([FakeProvider(garbage_for={"XLV"})], env)
    after = len(client.get("/api/data-issues").get_json()["quarantine"])
    assert after == before


def test_empty_primary_falls_through_to_yahoo(env):
    primary = FakeProvider(empty_for={"QQQ"})
    run_with([primary, FakeYahoo()], env)
    assert db.get_bars("QQQ").attrs["source"] == "yahoo"
    assert db.get_bars("SPY").attrs["source"] == "schwab"


def test_partial_failure_keeps_previous_data_current(env):
    run_with([FakeProvider()], env)
    spy_before = db.latest_bar("SPY")

    class SpyDown(FakeProvider):
        def get_daily_bars(self, symbol, start):
            if symbol == "SPY":
                raise ProviderError("SPY feed down")
            return super().get_daily_bars(symbol, start)

    result = run_with([SpyDown()], env)
    assert result["status"] == "partial"
    assert "SPY" in result["bars"]["failed"]
    assert db.latest_bar("SPY") == spy_before  # untouched, not deleted


def test_macro_override_beats_ingested_and_clears(env):
    run_with([FakeProvider()], env)
    client = app_mod.app.test_client()

    ingested_vix = client.get("/api/macro").get_json()["values"]["vix"]
    client.post("/api/overrides", data=json.dumps({"scope": "macro", "key": "vix", "value": 33.5}),
                content_type="application/json")
    macro = client.get("/api/macro").get_json()
    assert macro["values"]["vix"] == 33.5
    assert macro["fields"]["vix"]["override"] is True
    assert macro["fields"]["vix"]["source"] == "manual"
    assert macro["fields"]["vix"]["asOf"]  # timestamped

    # the override survives a fresh ingestion cycle
    run_with([FakeProvider()], env)
    assert client.get("/api/macro").get_json()["values"]["vix"] == 33.5

    client.post("/api/overrides", data=json.dumps({"scope": "macro", "key": "vix", "value": None}),
                content_type="application/json")
    assert client.get("/api/macro").get_json()["values"]["vix"] == ingested_vix


def test_macro_degraded_when_no_data(env):
    client = app_mod.app.test_client()
    macro = client.get("/api/macro").get_json()
    assert macro["degraded"] is True


def test_cross_check_flags_divergent_providers(env):
    class ShiftedYahoo(FakeYahoo):
        def get_daily_bars(self, symbol, start):
            df = super().get_daily_bars(symbol, start)
            if symbol == "SPY":
                df = df.assign(Close=df["Close"] * 1.05)
            return df

    run_with([FakeProvider(), ShiftedYahoo()], env)
    flags = [q for q in db.recent_quarantine() if q["kind"] == "divergence"]
    assert len(flags) == 1 and flags[0]["symbol"] == "SPY"
