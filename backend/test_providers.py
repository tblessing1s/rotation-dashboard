"""Provider chain: Schwab first when configured, every value keeps its source,
and failures fall through to the next provider instead of erroring out."""
from unittest import mock

import pandas as pd
import pytest

import providers
import providers.base as base
from ingest import fetch_symbol
from providers.base import Provider, ProviderError, with_retries


def bars(close=100.0):
    idx = pd.bdate_range("2026-06-01", periods=5)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1e6}, index=idx)


class Good(Provider):
    name = "good"

    def get_daily_bars(self, symbol, start):
        return bars()


class Broken(Provider):
    name = "broken"

    def get_daily_bars(self, symbol, start):
        raise ProviderError("boom")


def test_chain_is_yahoo_only_without_schwab_credentials(monkeypatch):
    for var in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert [p.name for p in providers.build_chain()] == ["yahoo"]


def test_chain_puts_schwab_first_when_configured(monkeypatch):
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setenv("SCHWAB_REFRESH_TOKEN", "r")
    assert [p.name for p in providers.build_chain()] == ["schwab", "yahoo"]


def test_fetch_symbol_falls_through_to_next_provider(monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    got, source = fetch_symbol("SPY", [Broken(), Good()], "2026-01-01")
    assert source == "good"
    assert len(got) == 5


def test_fetch_symbol_reports_all_errors_when_every_provider_fails(monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    with pytest.raises(ProviderError, match="broken: "):
        fetch_symbol("SPY", [Broken(), Broken()], "2026-01-01")


def test_with_retries_retries_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(base.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("transient")
        return "ok"

    assert with_retries(flaky, attempts=3, base_delay=2.0, label="t") == "ok"
    assert calls["n"] == 3
    assert sleeps == [2.0, 4.0]  # exponential backoff


def test_schwab_auth_failure_is_soft_and_recorded(fresh_db, monkeypatch):
    import db
    from providers.schwab import SchwabProvider

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setenv("SCHWAB_REFRESH_TOKEN", "expired")
    resp = mock.Mock(status_code=401, text="token expired")
    with mock.patch("providers.schwab.requests.post", return_value=resp):
        with pytest.raises(ProviderError, match="schwab-auth"):
            SchwabProvider().get_daily_bars("SPY", "2026-01-01")
    flag = db.kv_get("schwab_auth_error")
    assert flag and flag["status"] == 401


def test_schwab_maps_index_symbols(monkeypatch):
    from providers.schwab import SYMBOL_MAP

    assert SYMBOL_MAP["^VIX"] == "$VIX"
