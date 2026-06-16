"""Access-protection (single-user password gate) tests.

Assert the HTTP layer: with no password the app is open; with DASHBOARD_PASSWORD
set every non-exempt route is gated, the login form issues a session cookie, and
the exempt paths (login flow, health probe, cron ingest hook) stay reachable.
"""
import pytest

import app as app_mod


@pytest.fixture()
def client(fresh_db, monkeypatch):
    # Keep the before_request catch-up from spawning a real ingest thread.
    monkeypatch.setattr(app_mod.ingest, "is_stale", lambda *a, **k: False)
    app_mod.app.secret_key = "test-secret-key"
    return app_mod.app.test_client()


def test_open_when_no_password(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert client.get("/api/config").status_code == 200


def test_api_requires_auth_when_password_set(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    r = client.get("/api/config")
    assert r.status_code == 401
    assert r.get_json()["error"] == "Authentication required."


def test_page_redirects_to_login(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login")
    assert client.get("/login").status_code == 200          # form is reachable


def test_wrong_password_rejected(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401
    assert client.get("/api/config").status_code == 401      # still locked out


def test_login_then_access_granted(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    r = client.post("/login", data={"password": "s3cret"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert client.get("/api/config").status_code == 200      # cookie carries auth
    # Logout clears the session and re-locks the API.
    client.get("/logout")
    assert client.get("/api/config").status_code == 401


def test_exempt_paths_reachable_when_locked(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    assert client.get("/healthz").status_code == 200
    # The cron ingest hook is exempt from the session gate; it enforces its own
    # INGEST_TOKEN, so without that token it is rejected by the route (not 401'd
    # by the login gate, and certainly not run).
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    r = client.post("/api/ingest")
    assert r.status_code != 302                              # not bounced to login
