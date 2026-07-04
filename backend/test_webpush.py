"""Web Push channel tests — VAPID key exposure, subscription storage/dedup,
channel gating, and the notifier wiring. No network: pywebpush delivery to a
real endpoint is not exercised (that needs a live browser subscription), only
the storage/selection logic around it. Run offline with:
    python -m pytest backend -q
"""
import base64
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-push-test-"))

import pytest  # noqa: E402

import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import notifier  # noqa: E402
import webpush  # noqa: E402


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _clean_state():
    """Start every test from an empty current-schema state and clear VAPID env."""
    log.save_state({"schema_version": migrations.CURRENT_VERSION,
                    "metadata": {}, "positions": [], "executions": [],
                    "alerts": migrations.default_alert_state()})
    for k in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        os.environ.pop(k, None)
    yield


def _set_keys():
    os.environ["VAPID_PUBLIC_KEY"] = _b64url(b"\x04" + b"P" * 64)  # shape only
    os.environ["VAPID_PRIVATE_KEY"] = _b64url(b"K" * 32)
    os.environ["VAPID_SUBJECT"] = "mailto:test@example.com"


def _sub(endpoint="https://push.example/ep/1"):
    return {"endpoint": endpoint, "keys": {"p256dh": "BFakeKey", "auth": "authsecret"}}


def test_default_state_has_subscription_list():
    assert migrations.default_alert_state()["push_subscriptions"] == []


def test_migration_seeds_push_subscriptions():
    old = {"schema_version": 7, "metadata": {}, "positions": [], "executions": [],
           "alerts": {"active": {}, "log": [], "settings": {}, "last_run": None}}
    migrated, changed = migrations.migrate(old)
    assert changed
    assert migrated["schema_version"] == migrations.CURRENT_VERSION
    assert migrated["alerts"]["push_subscriptions"] == []


def test_keys_configured_reflects_env():
    assert webpush.keys_configured() is False
    _set_keys()
    assert webpush.keys_configured() is True
    assert webpush.public_key() == os.environ["VAPID_PUBLIC_KEY"]


def test_add_reject_invalid_subscription():
    with pytest.raises(ValueError):
        webpush.add_subscription({"endpoint": "x"})  # no keys
    with pytest.raises(ValueError):
        webpush.add_subscription({"keys": {"p256dh": "a", "auth": "b"}})  # no endpoint


def test_add_is_idempotent_on_endpoint():
    r1 = webpush.add_subscription(_sub())
    assert r1["count"] == 1 and r1["updated"] is False
    r2 = webpush.add_subscription(_sub())
    assert r2["count"] == 1 and r2["updated"] is True
    r3 = webpush.add_subscription(_sub("https://push.example/ep/2"))
    assert r3["count"] == 2 and r3["updated"] is False


def test_remove_subscription():
    webpush.add_subscription(_sub())
    out = webpush.remove_subscription("https://push.example/ep/1")
    assert out["removed"] == 1 and out["count"] == 0


def test_configured_requires_keys_and_a_device():
    _set_keys()
    assert webpush.configured() is False  # keys but no device
    webpush.add_subscription(_sub())
    assert webpush.configured() is True
    webpush.remove_subscription("https://push.example/ep/1")
    assert webpush.configured() is False  # device gone


def test_send_raises_without_keys():
    webpush.add_subscription(_sub())
    with pytest.raises(RuntimeError):
        webpush.send("s", "b", [])


def test_channel_registered_and_gated():
    names = [c.name for c in notifier.CHANNELS]
    assert "webpush" in names
    ch = next(c for c in notifier.CHANNELS if c.name == "webpush")
    assert ch.configured() is False
    _set_keys()
    webpush.add_subscription(_sub())
    assert ch.configured() is True


def test_dispatch_reports_webpush_without_crashing(monkeypatch):
    """A delivery exception must be caught and reported, never raised out of
    dispatch (the alerts are already persisted by then)."""
    _set_keys()
    webpush.add_subscription(_sub())

    def boom(subject, body, alerts):
        raise RuntimeError("simulated push failure")

    monkeypatch.setattr(webpush, "send", boom)
    report = notifier.dispatch(
        [{"type": "KILL_SWITCH_SECTOR", "severity": "CRITICAL", "ticker": "ON",
          "message": "m", "action": "a"}],
        settings={"channels": {"email": False, "ntfy": False}})
    entry = next(x for x in report if x["channel"] == "webpush")
    assert entry["ok"] is False and "simulated" in entry["error"]
