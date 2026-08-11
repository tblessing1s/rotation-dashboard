"""POST /api/admin/reset-book — the browser-triggered one-time book reset.

Guarded three ways: login (before_request auth gate — disabled in tests), the
RESET_BOOK_ENABLED server flag, and a typed "RESET" confirmation. Safe for the
single-writer store: reset_book takes the same lock every save uses.
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import app as app_module  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return app_module.app.test_client()


def _seed_populated():
    st = log.load_state()
    st["positions"] = [{"ticker": "XLK", "shares": {"count": 100}}]
    st["executions"] = [{"id": "exec_001", "action": "buy_shares", "ticker": "XLK"}]
    st["extrinsic_payback"] = {"XLK": {"pct_complete": 5}}
    st.setdefault("alerts", {})["push_subscriptions"] = [{"endpoint": "x"}]
    st["metadata"]["operating_cash"] = 42000
    log.save_state(st)


def test_reset_disabled_returns_403(client, monkeypatch):
    monkeypatch.setattr(config, "RESET_BOOK_ENABLED", False)
    _seed_populated()
    r = client.post("/api/admin/reset-book", json={"confirm": "RESET"})
    assert r.status_code == 403 and r.get_json()["reset_disabled"] is True
    assert log.load_state()["positions"]  # book untouched


def test_reset_requires_typed_confirm(client, monkeypatch):
    monkeypatch.setattr(config, "RESET_BOOK_ENABLED", True)
    _seed_populated()
    r = client.post("/api/admin/reset-book", json={"confirm": "yes"})
    assert r.status_code == 400 and r.get_json()["confirm_required"] is True
    assert log.load_state()["executions"]  # book untouched


def test_reset_clears_book_keeps_subs_and_cash(client, monkeypatch):
    monkeypatch.setattr(config, "RESET_BOOK_ENABLED", True)
    _seed_populated()
    r = client.post("/api/admin/reset-book", json={"confirm": "RESET"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["cleared"] == {"positions": 1, "executions": 1}
    st = log.load_state()
    assert st["positions"] == [] and st["executions"] == []
    assert st["extrinsic_payback"] == {}
    # Kept by default (annoying to re-enter; the backup has them anyway).
    assert st["alerts"]["push_subscriptions"] == [{"endpoint": "x"}]
    assert st["metadata"]["operating_cash"] == 42000
    # Recoverable: a rotating backup + the pre-reset aside copy both exist.
    assert os.path.exists(body["backup"]) and os.path.exists(body["pre_reset"])


def test_reset_wipe_all_clears_settings(client, monkeypatch):
    monkeypatch.setattr(config, "RESET_BOOK_ENABLED", True)
    _seed_populated()
    r = client.post("/api/admin/reset-book", json={"confirm": "RESET", "wipe_all": True})
    assert r.status_code == 200 and r.get_json()["wipe_all"] is True
    st = log.load_state()
    assert not st["alerts"].get("push_subscriptions")   # cleared
    assert st["metadata"].get("operating_cash") in (0, None)  # back to default
