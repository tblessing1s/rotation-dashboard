"""/api/daily-screener contract tests.

The Finviz provider is mocked — these assert the HTTP layer: query-param
parsing, that the volume metadata (volFilterApplied/volPrecise) and per-row
fields reach the client, and that provider failures map to sane status codes.
"""
import pytest

import app as app_mod
from providers import finviz_screen


@pytest.fixture()
def client(fresh_db, monkeypatch):
    # Keep the before_request catch-up from spawning a real background ingest
    # thread that would race other tests' datastore (see _catchup_if_stale).
    monkeypatch.setattr(app_mod.ingest, "is_stale", lambda *a, **k: False)
    return app_mod.app.test_client()


def test_daily_screener_passes_filters_and_surfaces_vol_metadata(client, monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "results": [
                {"symbol": "AAA", "price": 50.0, "atrPct": 6.0, "changePct": 2.3,
                 "avgVol": 15_000_000, "rvol": 1.5, "sector": "Tech", "source": "finviz"},
            ],
            "volFilterApplied": "Over 2M",
            "volPrecise": True,
        }

    monkeypatch.setattr(finviz_screen, "run", fake_run)
    out = client.get("/api/daily-screener?price_min=20&price_max=100"
                     "&vol_min=10000000&atr_min=4&atr_max=9").get_json()

    assert out["count"] == 1
    assert out["source"] == "finviz"
    assert out["volFilterApplied"] == "Over 2M"
    assert out["volPrecise"] is True
    assert out["results"][0]["rvol"] == 1.5 and out["results"][0]["changePct"] == 2.3
    # Frontend sends shares; the endpoint forwards them verbatim as the floor.
    assert captured["vol_min_shares"] == 10_000_000
    assert captured["price_min"] == 20 and captured["price_max"] == 100


def test_daily_screener_maps_provider_runtimeerror_to_502(client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("Finviz screener unavailable: 403")

    monkeypatch.setattr(finviz_screen, "run", boom)
    resp = client.get("/api/daily-screener")
    assert resp.status_code == 502
    assert "Finviz" in resp.get_json()["error"]


def test_daily_screener_rejects_bad_params(client):
    resp = client.get("/api/daily-screener?price_min=abc")
    assert resp.status_code == 400
