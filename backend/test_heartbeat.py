"""Dead-man's-switch tests — heartbeat throttling, inert-when-unset, /fail
semantics, and that the scheduler tick actually pings. No real network: the
requests.post call is monkeypatched to record calls. Run offline with:
    python -m pytest backend -q
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-hb-test-"))

import pytest  # noqa: E402

import heartbeat  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Clear env + throttle state and capture outbound pings per test."""
    for k in ("HEALTHCHECK_URL", "HEALTHCHECK_MIN_INTERVAL"):
        os.environ.pop(k, None)
    heartbeat._last_ping = 0.0
    calls = []
    monkeypatch.setattr(heartbeat.requests, "post",
                        lambda u, **kw: calls.append(u) or type("R", (), {})())
    return calls


def test_inert_when_unconfigured(_reset):
    assert heartbeat.configured() is False
    assert heartbeat.ping() is False
    assert _reset == []


def test_liveness_ping_hits_base_url(_reset):
    os.environ["HEALTHCHECK_URL"] = "https://hc-ping.com/abc"
    assert heartbeat.ping() is True
    assert _reset == ["https://hc-ping.com/abc"]


def test_liveness_ping_is_throttled(_reset):
    os.environ["HEALTHCHECK_URL"] = "https://hc-ping.com/abc"
    os.environ["HEALTHCHECK_MIN_INTERVAL"] = "9999"
    assert heartbeat.ping() is True          # first goes
    assert heartbeat.ping() is False         # second throttled
    assert heartbeat.ping() is False
    assert _reset == ["https://hc-ping.com/abc"]  # only one sent


def test_fail_ping_bypasses_throttle_and_appends_suffix(_reset):
    os.environ["HEALTHCHECK_URL"] = "https://hc-ping.com/abc/"
    os.environ["HEALTHCHECK_MIN_INTERVAL"] = "9999"
    heartbeat.ping()                         # arms the throttle
    assert heartbeat.ping("/fail") is True   # /fail is never throttled
    assert heartbeat.ping("/fail") is True
    assert _reset[-1] == "https://hc-ping.com/abc/fail"  # trailing slash trimmed


def test_ping_never_raises(monkeypatch, _reset):
    os.environ["HEALTHCHECK_URL"] = "https://hc-ping.com/abc"

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(heartbeat.requests, "post", boom)
    assert heartbeat.ping() is False  # swallowed, returns False


def test_scheduler_tick_pings_when_configured(monkeypatch, _reset):
    os.environ["HEALTHCHECK_URL"] = "https://hc-ping.com/abc"
    import alert_scheduler
    # Neutralize the rest of the tick so we isolate the heartbeat call.
    monkeypatch.setattr(alert_scheduler, "maintenance_due", lambda *a: False)
    monkeypatch.setattr(alert_scheduler, "due_slots", lambda *a, **k: [])
    alert_scheduler._tick()
    assert _reset == ["https://hc-ping.com/abc"]
