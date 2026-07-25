"""Read-only-legacy enforcement (v3.0): no NEW LEAP may be opened, rolled, or
recommended once the diagonal is retired as an active structure. Enforced at the
operator boundary (app.api_execute); the executor primitive stays capable so
legacy history, adoption/rebuild and the test surface are unaffected.

Run: python -m pytest backend/test_leap_read_only.py -q
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import config  # noqa: E402
import executor  # noqa: E402


# ---- Pure guard --------------------------------------------------------------
def test_leap_open_actions_blocked_when_read_only(monkeypatch):
    monkeypatch.setattr(config, "LEAP_LEGACY_READ_ONLY", True)
    for action in ("buy_leap", "roll_leap", "open_position_atomic"):
        reason = executor.leap_open_blocked({"action": action})
        assert reason is not None and "read-only legacy" in reason


def test_closing_and_shares_actions_never_blocked(monkeypatch):
    monkeypatch.setattr(config, "LEAP_LEGACY_READ_ONLY", True)
    for action in ("close_leap", "close_position_atomic", "roll_short", "close_short",
                   "buy_shares", "sell_shares", "close_shares_assigned", "sell_short",
                   "dividend_income", "adjustment"):
        assert executor.leap_open_blocked({"action": action}) is None


def test_allow_legacy_leap_bypass(monkeypatch):
    monkeypatch.setattr(config, "LEAP_LEGACY_READ_ONLY", True)
    assert executor.leap_open_blocked(
        {"action": "buy_leap", "allow_legacy_leap": True}) is None


def test_toggle_off_permits_leap_open(monkeypatch):
    monkeypatch.setattr(config, "LEAP_LEGACY_READ_ONLY", False)
    assert executor.leap_open_blocked({"action": "buy_leap"}) is None


# ---- Operator endpoint -------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(config, "LEAP_LEGACY_READ_ONLY", True)
    import app as app_module
    return app_module.app.test_client()


def test_api_execute_rejects_new_leap(client):
    resp = client.post("/api/execute", json={"action": "buy_leap", "ticker": "NVDA",
                                             "strike": 90, "contracts": 5,
                                             "execution_price": 5000, "stock_price": 120})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["leap_read_only"] is True
    assert "read-only legacy" in body["error"]


def test_api_execute_rejects_roll_leap(client):
    resp = client.post("/api/execute", json={"action": "roll_leap", "ticker": "NVDA"})
    assert resp.status_code == 400
    assert resp.get_json()["leap_read_only"] is True


def test_api_execute_allows_close_leap_through_the_guard(client):
    # close_leap is NOT blocked by the read-only guard — it may fail downstream for
    # other reasons (no position), but never with the leap_read_only rejection.
    resp = client.post("/api/execute", json={"action": "close_leap", "ticker": "NONE",
                                             "strike": 90, "contracts": 1,
                                             "close_price": 100, "stock_price": 120})
    body = resp.get_json() or {}
    assert not body.get("leap_read_only")
