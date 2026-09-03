"""Coverage-miss acknowledgements: the v23 store, the API, and the alert.

An acknowledgement classifies a COVERAGE_MISS with a coded reason. It never
removes one — coverage and graduation are pinned unchanged in
test_trust_derive; this file covers the persistence path, the route's
validation, and the alert that stops paging once the operator has answered.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-ack-test-"))
# Importing app starts the in-process scheduler (and the scan warm-up thread)
# unless disabled — a background writer under the suite is a state race.
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")

import alerts  # noqa: E402
import app as app_module  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import rec_types  # noqa: E402


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return app_module.app.test_client()


def _seed_miss():
    """A scheduled roll pair with no recommendation behind it -> one ROLL_OUT
    coverage miss on the derived resolutions."""
    at = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    st = log.load_state()
    st["metadata"]["trust_layer_since"] = "2026-01-01T00:00:00Z"
    st["executions"] = [
        {"id": "exec_005", "ticker": "SPCX", "action": "close_short", "date": at,
         "strike": 50.0, "contracts": 1, "roll_group_id": "r1", "roll_reason": "scheduled",
         "close_price_per_share": 1.0, "live_transmitted": True},
        {"id": "exec_006", "ticker": "SPCX", "action": "sell_short", "date": at,
         "strike": 49.0, "contracts": 1, "roll_group_id": "r1", "roll_reason": "scheduled",
         "premium_per_share": 2.0, "live_transmitted": True},
    ]
    log.recompute_derived(st)
    log.save_state(st)
    misses = [r for r in st["recommendation_resolutions"] if r["status"] == "COVERAGE_MISS"]
    assert len(misses) == 1 and misses[0]["miss_key"] == "exec_005,exec_006"
    return misses[0]


def test_v23_seeds_the_ack_store_and_default_state_carries_it():
    out = migrations._v22_to_v23({"schema_version": 22, "positions": []})
    assert out["coverage_miss_acks"] == []
    assert log._default_state()["coverage_miss_acks"] == []
    assert migrations.CURRENT_VERSION == 23


def test_reason_vocabulary():
    assert rec_types.is_miss_ack_reason("OPERATOR_DISCRETION")
    assert not rec_types.is_miss_ack_reason("DISAGREE_TIMING")   # an override reason, not an ack
    assert rec_types.miss_ack_requires_note("OTHER")
    assert not rec_types.miss_ack_requires_note("RULE_GAP")


def test_acknowledge_route_validates_and_persists(client):
    _seed_miss()
    r = client.post("/api/recommendations/acknowledge-miss", json={"reason": "OPERATOR_DISCRETION"})
    assert r.status_code == 400 and "execution_ids" in r.get_json()["error"]
    r = client.post("/api/recommendations/acknowledge-miss",
                    json={"execution_ids": ["exec_005", "exec_006"], "reason": "NOPE"})
    assert r.status_code == 400
    r = client.post("/api/recommendations/acknowledge-miss",
                    json={"execution_ids": ["exec_005", "exec_006"], "reason": "OTHER"})
    assert r.status_code == 400 and "note" in r.get_json()["error"]
    r = client.post("/api/recommendations/acknowledge-miss",
                    json={"execution_ids": ["exec_999"], "reason": "RULE_GAP"})
    assert r.status_code == 404

    # ids in any order, reason case-folded
    r = client.post("/api/recommendations/acknowledge-miss",
                    json={"execution_ids": ["exec_006", "exec_005"],
                          "reason": "operator_discretion", "note": "rolled off-schedule"})
    assert r.status_code == 200, r.get_json()
    ack = r.get_json()["acknowledgement"]
    assert ack["id"] == "ack_00001" and ack["reason"] == "OPERATOR_DISCRETION"
    assert ack["execution_ids"] == ["exec_005", "exec_006"]
    assert ack["ticker"] == "SPCX" and ack["action_type"] == "ROLL_OUT"

    st = log.load_state()
    assert st["coverage_miss_acks"][0]["id"] == "ack_00001"
    miss = [x for x in st["recommendation_resolutions"] if x["status"] == "COVERAGE_MISS"][0]
    assert miss["acknowledged"]["reason"] == "OPERATOR_DISCRETION"
    assert miss["acknowledged"]["note"] == "rolled off-schedule"
    # the miss is classified, not gone
    assert st["trust_scoreboard"]["totals"]["coverage_misses"] == 1
    assert st["trust_scoreboard"]["totals"]["coverage_misses_acknowledged"] == 1
    board = client.get("/api/trust-scoreboard").get_json()
    shown = board["scoreboard"]["by_action_type"]["ROLL_OUT"]["coverage"]["misses"]
    assert shown[0]["acknowledged"]["id"] == "ack_00001"

    # second acknowledgement of the same miss is refused; the record stays single
    r = client.post("/api/recommendations/acknowledge-miss",
                    json={"execution_ids": ["exec_005", "exec_006"], "reason": "RULE_GAP"})
    assert r.status_code == 409
    assert len(log.load_state()["coverage_miss_acks"]) == 1


def test_acknowledged_miss_stops_paging(client):
    _seed_miss()
    st = log.load_state()
    assert [a["type"] for a in alerts.check_trust_coverage_miss(st)] == ["TRUST_COVERAGE_MISS"]
    r = client.post("/api/recommendations/acknowledge-miss",
                    json={"execution_ids": ["exec_005", "exec_006"], "reason": "OPERATOR_DISCRETION"})
    assert r.status_code == 200
    assert alerts.check_trust_coverage_miss(log.load_state()) == []
