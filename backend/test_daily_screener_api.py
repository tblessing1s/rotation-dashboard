"""/api/daily-screener contract tests.

The screener snapshot/build is mocked — these assert the HTTP layer: query-param
parsing, that filters are applied to the cached snapshot, the building/stale
metadata reaches the client, and that a missing API key maps to a sane status.
"""
import pytest

import app as app_mod
import screener


@pytest.fixture()
def client(fresh_db, monkeypatch):
    # Keep the before_request catch-up from spawning a real background ingest
    # thread that would race other tests' datastore (see _catchup_if_stale).
    monkeypatch.setattr(app_mod.ingest, "is_stale", lambda *a, **k: False)
    # Pretend the key is configured unless a test overrides it.
    monkeypatch.setattr(app_mod, "alphavantage_configured", lambda: True)
    return app_mod.app.test_client()


_SNAPSHOT = {
    "rows": [
        {"symbol": "AAA", "price": 50.0, "atrPct": 6.0, "changePct": 2.3,
         "avgVol": 15_000_000, "rvol": 1.5, "sector": "", "source": "alphavantage"},
        {"symbol": "BBB", "price": 250.0, "atrPct": 7.0, "changePct": -1.0,
         "avgVol": 20_000_000, "rvol": 0.9, "sector": "", "source": "alphavantage"},
        {"symbol": "CCC", "price": 40.0, "atrPct": 2.0, "changePct": 0.5,
         "avgVol": 12_000_000, "rvol": 1.1, "sector": "", "source": "alphavantage"},
    ],
    "universeSize": 3,
    "builtAt": "2026-06-16T12:00:00Z",
    "source": "alphavantage",
}


def test_daily_screener_filters_snapshot_and_surfaces_meta(client, monkeypatch):
    monkeypatch.setattr(screener, "get_snapshot", lambda **k: (_SNAPSHOT, False))
    monkeypatch.setattr(screener, "is_fresh", lambda s: True)

    out = client.get("/api/daily-screener?price_min=20&price_max=100"
                     "&vol_min=10000000&atr_min=4&atr_max=9").get_json()

    # Only AAA passes price 20-100 + ATR% 4-9 + vol >= 10M (BBB price too high,
    # CCC ATR% too low).
    assert out["count"] == 1
    assert out["source"] == "alphavantage"
    assert out["results"][0]["symbol"] == "AAA"
    assert out["results"][0]["rvol"] == 1.5 and out["results"][0]["changePct"] == 2.3
    assert out["building"] is False
    assert out["universeSize"] == 3


def test_daily_screener_reports_building_when_no_snapshot(client, monkeypatch):
    monkeypatch.setattr(screener, "get_snapshot", lambda **k: (None, True))
    out = client.get("/api/daily-screener").get_json()
    assert out["building"] is True
    assert out["count"] == 0
    assert "Building" in out["message"]


def test_daily_screener_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(app_mod, "alphavantage_configured", lambda: False)
    resp = client.get("/api/daily-screener")
    assert resp.status_code == 503
    assert "ALPHAVANTAGE_API_KEY" in resp.get_json()["error"]


def test_daily_screener_rejects_bad_params(client):
    resp = client.get("/api/daily-screener?price_min=abc")
    assert resp.status_code == 400
