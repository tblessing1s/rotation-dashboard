"""Proposed-entries dry-powder filter: /api/recommendations hides ENTER recs
whose lot cost exceeds the CURRENT deployable capital, even though each was
affordable at the pass that emitted it — dry powder moves after a rec is
parked on the board and nothing else prunes it (open_recommendations only
drops a rec by resolution or expiry, never by wallet size).
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-dp-test-"))
# Importing app starts the in-process scheduler (and the scan warm-up thread)
# unless disabled — a background writer under the suite is a state race.
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")

import app as app_module  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return app_module.app.test_client()


def _enter_rec(rec_id, ticker, lot_cost, now):
    return {
        "rec_id": rec_id, "action_type": "ENTER", "ticker": ticker,
        "emitted_at": _iso(now - timedelta(hours=1)),
        "valid_until": _iso(now + timedelta(hours=21)),
        "trigger_rule": "GATE_ALL_PASS",
        "input_snapshot": {"lot_cost": lot_cost, "deployable": lot_cost},
        "proposed_ticket": {"action": "buy_shares", "legs": [], "estimates": {}},
    }


def test_recommendations_hides_enter_recs_over_current_dry_powder(client):
    # deployable = min(cap - deployed, operating_cash - reserve)
    #            = min(38000 - 0, 15000 - 13000) = 2000
    now = datetime.now(timezone.utc)
    st = log.load_state()
    st["metadata"]["operating_cash"] = 15000.0
    st["metadata"]["reserve_required"] = 13000.0
    st["recommendations"] = [
        _enter_rec("rec_affordable", "SCHD", 1500.0, now),
        _enter_rec("rec_over_budget", "GDX", 9733.0, now),
    ]
    log.save_state(st)

    r = client.get("/api/recommendations")
    assert r.status_code == 200
    body = r.get_json()
    ids = {rec["rec_id"] for rec in body["open"]}
    assert "rec_affordable" in ids
    assert "rec_over_budget" not in ids
    # The persisted log is untouched by the display filter — only what the
    # endpoint surfaces for the board is trimmed.
    assert body["total"] == 2

    actionable_ids = {rec["rec_id"] for rec in body["open_actionable"]}
    assert "rec_over_budget" not in actionable_ids


def test_recommendations_keeps_non_enter_recs_regardless_of_dry_powder(client):
    now = datetime.now(timezone.utc)
    st = log.load_state()
    st["metadata"]["operating_cash"] = 0.0
    st["metadata"]["reserve_required"] = 13000.0
    st["recommendations"] = [
        {"rec_id": "rec_roll", "action_type": "ROLL_OUT", "ticker": "AAPL",
         "emitted_at": _iso(now - timedelta(hours=1)),
         "valid_until": _iso(now + timedelta(hours=21)),
         "trigger_rule": "ROLL_SCHEDULED_WEEKLY", "input_snapshot": {}},
    ]
    log.save_state(st)

    r = client.get("/api/recommendations")
    assert r.status_code == 200
    ids = {rec["rec_id"] for rec in r.get_json()["open"]}
    assert "rec_roll" in ids


def test_recommendations_reappear_once_dry_powder_frees_up(client):
    now = datetime.now(timezone.utc)
    st = log.load_state()
    st["metadata"]["operating_cash"] = 0.0
    st["metadata"]["reserve_required"] = 13000.0
    st["recommendations"] = [_enter_rec("rec_gdx", "GDX", 9733.0, now)]
    log.save_state(st)

    r = client.get("/api/recommendations")
    assert "rec_gdx" not in {rec["rec_id"] for rec in r.get_json()["open"]}

    st = log.load_state()
    st["metadata"]["operating_cash"] = 23000.0
    log.save_state(st)

    r = client.get("/api/recommendations")
    assert "rec_gdx" in {rec["rec_id"] for rec in r.get_json()["open"]}
