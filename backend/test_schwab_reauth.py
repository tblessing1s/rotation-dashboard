"""Hosted Schwab re-auth: the weekly token refresh must be a one-click browser
flow — state is single-use, the minted token lands in the datastore, and
failures surface in the browser instead of silently breaking ingestion."""
from unittest import mock

import pytest

import db


@pytest.fixture()
def client(fresh_db, monkeypatch):
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    import app as app_mod

    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


def test_start_redirects_to_schwab_consent(client):
    resp = client.get("/auth/schwab")
    assert resp.status_code == 302
    assert resp.location.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    assert "client_id=k" in resp.location
    state = db.kv_get("schwab_oauth_state")
    assert state and state["state"] in resp.location


def test_start_fails_clearly_without_app_credentials(client, monkeypatch):
    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    resp = client.get("/auth/schwab")
    assert resp.status_code == 400
    assert b"SCHWAB_APP_KEY" in resp.data


def test_callback_rejects_state_mismatch(client):
    client.get("/auth/schwab")  # mints a state
    resp = client.get("/auth/schwab/callback?code=abc&state=wrong")
    assert resp.status_code == 400
    assert db.kv_get("schwab_token") is None


def test_callback_stores_token_and_kicks_ingest(client, monkeypatch):
    import app as app_mod

    kicked = []
    monkeypatch.setattr(app_mod.ingest, "run_in_background", lambda *a, **k: kicked.append(a))

    client.get("/auth/schwab")
    state = db.kv_get("schwab_oauth_state")["state"]
    with mock.patch(
        "providers.schwab.exchange_code", return_value={"refresh_token": "new-tok"}
    ) as ex:
        resp = client.get(f"/auth/schwab/callback?code=abc&state={state}")
    assert resp.status_code == 200
    assert ex.call_args[0][0] == "abc"
    stored = db.kv_get("schwab_token")
    assert stored["refresh_token"] == "new-tok" and stored["minted_at"]
    assert db.kv_get("schwab_oauth_state") is None  # single-use
    assert kicked  # fresh data pulled immediately


def test_callback_surfaces_exchange_failure(client):
    from providers.base import ProviderError

    client.get("/auth/schwab")
    state = db.kv_get("schwab_oauth_state")["state"]
    with mock.patch("providers.schwab.exchange_code", side_effect=ProviderError("HTTP 400")):
        resp = client.get(f"/auth/schwab/callback?code=abc&state={state}")
    assert resp.status_code == 400
    assert db.kv_get("schwab_token") is None
