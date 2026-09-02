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
    """Start every test from an empty current-schema state, clear VAPID env, and
    reset the auto-generated key cache/file so tests don't leak keys."""
    log.save_state({"schema_version": migrations.CURRENT_VERSION,
                    "metadata": {}, "positions": [], "executions": [],
                    "alerts": migrations.default_alert_state()})
    for k in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        os.environ.pop(k, None)
    webpush._cache = None
    try:
        os.remove(webpush._VAPID_FILE)
    except OSError:
        pass
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


def test_explicit_env_keys_take_precedence():
    _set_keys()
    assert webpush.keys_configured() is True
    assert webpush.public_key() == os.environ["VAPID_PUBLIC_KEY"]


def test_keys_autogenerate_and_persist_when_no_env():
    # No env vars set -> a keypair is generated, persisted, and stays stable.
    assert webpush.keys_configured() is True
    pub = webpush.public_key()
    assert pub and os.path.exists(webpush._VAPID_FILE)
    # A fresh in-memory cache reloads the SAME persisted key (stability matters:
    # rotating it would invalidate every device subscription).
    webpush._cache = None
    assert webpush.public_key() == pub


def test_env_only_public_falls_back_to_persisted_pair():
    # A half-set env (public only) must NOT mix with a persisted private key.
    os.environ["VAPID_PUBLIC_KEY"] = _b64url(b"\x04" + b"Q" * 64)
    pub, priv = webpush.public_key(), webpush._private_key()
    # Both come from the persisted pair, so they are a matched set.
    assert pub != os.environ["VAPID_PUBLIC_KEY"]
    assert pub and priv


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


def test_send_raises_when_all_deliveries_fail():
    # Keys auto-configure; a subscription with a bogus p256dh key fails to
    # encrypt, so 0/1 devices are reached and send() surfaces that as an error
    # (offline: pywebpush rejects the key before any network call).
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


# ---------------------------------------------------------------------------
# Delivery parameters — exercised against a fake ``pywebpush`` so the tests run
# without the real library (its http-ece dep doesn't build everywhere) and
# without a network.
# ---------------------------------------------------------------------------
class _FakeWebPushException(Exception):
    def __init__(self, msg, response=None):
        super().__init__(msg)
        self.response = response


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _fake_pywebpush(monkeypatch, outcome):
    """Install a stand-in pywebpush whose webpush() records its kwargs and
    either succeeds or raises per ``outcome(endpoint)``."""
    import sys
    import types
    calls = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)
        err = outcome(kwargs["subscription_info"]["endpoint"])
        if err is not None:
            raise err

    mod = types.ModuleType("pywebpush")
    mod.webpush = fake_webpush
    mod.WebPushException = _FakeWebPushException
    monkeypatch.setitem(sys.modules, "pywebpush", mod)
    return calls


def test_send_uses_a_real_ttl_and_high_urgency(monkeypatch):
    """pywebpush defaults to TTL 0 — 'deliver now or discard' — so a phone that
    is asleep / off-network at that instant never sees the alert. Every push
    must carry a positive TTL and high urgency (Android Doze delivery)."""
    _set_keys()
    webpush.add_subscription(_sub())
    calls = _fake_pywebpush(monkeypatch, lambda ep: None)
    webpush.send("s", "b", [{"severity": "HIGH", "ticker": "NVDA"}])
    assert len(calls) == 1
    assert calls[0]["ttl"] == webpush.TTL_SECONDS > 0
    assert calls[0]["headers"]["Urgency"] == "high"
    assert calls[0]["vapid_private_key"] == os.environ["VAPID_PRIVATE_KEY"]
    assert calls[0]["vapid_claims"]["sub"] == "mailto:test@example.com"


def test_send_prunes_gone_and_key_mismatch_subscriptions(monkeypatch):
    """404/410 (device unsubscribed) and FCM's 403 VapidPkHashMismatch (enrolled
    against a previous server key) are permanent: drop them so they stop
    failing every batch. A transient 5xx is kept."""
    _set_keys()
    webpush.add_subscription(_sub("https://push.example/gone"))
    webpush.add_subscription(_sub("https://push.example/stale-key"))
    webpush.add_subscription(_sub("https://push.example/flaky"))
    webpush.add_subscription(_sub("https://push.example/ok"))

    def outcome(ep):
        if ep.endswith("/gone"):
            return _FakeWebPushException("Push failed: 410 Gone", _Resp(410))
        if ep.endswith("/stale-key"):
            return _FakeWebPushException(
                "Push failed: 403 Forbidden\nResponse body:VapidPkHashMismatch",
                _Resp(403, "the key in the authorization header does not match "
                           "the key used to create the subscription: VapidPkHashMismatch"))
        if ep.endswith("/flaky"):
            return _FakeWebPushException("Push failed: 503", _Resp(503, "try later"))
        return None

    _fake_pywebpush(monkeypatch, outcome)
    webpush.send("s", "b", [])  # one device reached -> no error
    left = {s["endpoint"] for s in webpush.list_subscriptions()}
    assert left == {"https://push.example/flaky", "https://push.example/ok"}


def test_send_error_names_the_push_service_reason(monkeypatch):
    """When no device is reached, the raised error carries the service's status
    and message so the 'Send test' toast / alert log explain the failure."""
    _set_keys()
    webpush.add_subscription(_sub())
    _fake_pywebpush(monkeypatch, lambda ep: _FakeWebPushException(
        "Push failed: 401 Unauthorized\nResponse body:bad jwt", _Resp(401, "bad jwt")))
    with pytest.raises(RuntimeError) as ei:
        webpush.send("s", "b", [])
    msg = str(ei.value)
    assert "0/1" in msg and "401" in msg and "Unauthorized" in msg
