"""End-to-end: a position that needs attention -> alert engine -> notifier ->
Web Push on the phone.

The unit tests around this cover each link in isolation (one evaluator, dedup,
channel gating, payload shape). These tests walk the WHOLE chain the way the
scheduler does — a real state file holding a real-shaped position with a short
call about to expire, ``alerts.run()`` exactly as the tick calls it, the real
``notifier.dispatch`` and ``webpush.send`` — and assert what arrives at the push
service. One of them runs the real ``pywebpush`` encryption + VAPID signing and
decrypts the message with the "phone's" private key, so the bytes the service
worker would receive are checked, not just the call. The only thing faked is
the network POST. No provider keys, no Schwab: run offline with
    python -m pytest backend/test_alert_delivery.py -q
"""
import base64
import json
import os
import tempfile
from urllib.parse import urlsplit

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-delivery-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import alerts  # noqa: E402
import app as app_module  # noqa: E402
import config  # noqa: E402
import data_handler  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import notifier  # noqa: E402
import webpush  # noqa: E402

TICKER = "NVDA"
STRIKE = 124


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _frame(values):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e6},
                        index=idx)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own state file + data dir, no VAPID env (keys auto-generate into tmp), no
    email/ntfy env, dry-run env off, flat prices so only the engineered condition
    trips. Mirrors production wiring otherwise."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(webpush, "_VAPID_FILE", str(tmp_path / ".vapid_keys.json"))
    monkeypatch.setattr(webpush, "_cache", None)
    for k in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT", "CFM_ALERTS_DRY_RUN",
              "SMTP_HOST", "ALERT_EMAIL_TO", "ALERT_NTFY_TOPIC"):
        monkeypatch.delenv(k, raising=False)
    frames = {"SPY": _frame([500.0] * 260), "XLK": _frame([200.0] * 260),
              TICKER: _frame(list(np.linspace(120, 130, 260)))}
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: frames.get(str(s).upper()))
    yield tmp_path


def _position_needing_attention() -> dict:
    """100 real shares + one weekly covered call expiring tomorrow (not rolled):
    the EXPIRY_FRIDAY condition, the most routine "needs attention" page."""
    return {
        "ticker": TICKER, "sector": "XLK", "status": "active",
        "shares": {"count": 100, "cap": 100},
        # Spot ~130 vs strike 124: 6.00 intrinsic + 0.40 extrinsic, sold at the
        # same price — healthy ITM weekly, so no buyback / assignment-risk /
        # extrinsic alert competes; the expiry is the ONE thing needing attention.
        "short_calls": [{"strike": STRIKE, "contracts": 1, "dte": 1,
                         "current_bid": 6.40, "entry_premium_total": 640.0,
                         "open_date": "2026-08-28", "expiration": "2026-09-04"}],
    }


def _seed(*positions) -> None:
    log.save_state({"schema_version": migrations.CURRENT_VERSION, "metadata": {},
                    "positions": list(positions), "executions": [],
                    "alerts": migrations.default_alert_state()})


def _phone_subscription(endpoint="https://fcm.googleapis.com/fcm/send/device-1"):
    """A browser-side PushSubscription with a REAL P-256 keypair, so pywebpush
    can encrypt for it and the test can decrypt as the phone would."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    p256dh = priv.public_key().public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    sub = {"endpoint": endpoint, "keys": {"p256dh": _b64url(p256dh), "auth": _b64url(auth)}}
    return sub, priv, auth


def _fake_pywebpush(monkeypatch):
    """Stand-in pywebpush that records every send (no crypto, no network)."""
    import sys
    import types
    calls = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)

    mod = types.ModuleType("pywebpush")
    mod.webpush = fake_webpush

    class WebPushException(Exception):
        pass
    mod.WebPushException = WebPushException
    monkeypatch.setitem(sys.modules, "pywebpush", mod)
    return calls


# ---------------------------------------------------------------------------
# 1. The scheduler's path: state -> alerts.run() -> dispatch -> webpush.send
# ---------------------------------------------------------------------------
def test_position_needing_attention_reaches_the_phone(monkeypatch):
    _seed(_position_needing_attention())
    webpush.add_subscription(_phone_subscription()[0])
    calls = _fake_pywebpush(monkeypatch)

    result = alerts.run()  # exactly what alert_scheduler's tick calls

    fired = {a["type"]: a for a in result["fired"]}
    assert "EXPIRY_FRIDAY" in fired, fired.keys()
    roll = fired["EXPIRY_FRIDAY"]
    assert roll["ticker"] == TICKER
    assert roll["action_url"] == f"/?action=roll&ticker={TICKER}&reason=scheduled"
    # Delivered through the webpush channel, and the report says so.
    assert result["dry_run"] is False
    assert {"channel": "webpush", "ok": True} in result["delivery"]
    assert len(calls) == 1
    payload = json.loads(calls[0]["data"])
    # The phone gets: the ticker in the title, the roll deep link as the tap
    # target (the digest rides in the same batch and must not hide it), and the
    # worst severity in the batch (not the LOW digest that leads it).
    assert TICKER in payload["title"] and "[CFM" in payload["title"]
    assert payload["url"] == roll["action_url"]
    worst = min(result["fired"], key=lambda a: notifier.SEVERITY_ORDER[a["severity"]])
    assert payload["severity"] == worst["severity"]
    assert payload["tickers"] == [TICKER]
    assert f"short {STRIKE} expires in 1 day(s)" in payload["body"]
    # Delivery parameters that make it survive a sleeping phone.
    assert calls[0]["ttl"] == webpush.TTL_SECONDS and calls[0]["headers"]["Urgency"] == "high"
    # The alert is persisted as active so the dashboard bell agrees with the phone.
    assert any(a["type"] == "EXPIRY_FRIDAY" for a in alerts.view()["active"])


def test_same_condition_is_not_re_pushed_and_resolves_when_rolled(monkeypatch):
    """The condition stays true across every slot of the day: ONE push, not one
    per tick. Rolling the short clears it; the next week's expiry pages again."""
    _seed(_position_needing_attention())
    webpush.add_subscription(_phone_subscription()[0])
    calls = _fake_pywebpush(monkeypatch)

    alerts.run()
    alerts.run()
    alerts.run()
    assert len(calls) == 1

    # The operator rolled: the short now has a week to run.
    state = log.load_state()
    state["positions"][0]["short_calls"][0].update({"dte": 7, "expiration": "2026-09-11"})
    log.save_state(state)
    r = alerts.run()
    assert any(a["type"] == "EXPIRY_FRIDAY" for a in r["resolved"])
    assert not any(a["type"] == "EXPIRY_FRIDAY" for a in alerts.view()["active"])
    assert len(calls) == 1  # resolving is silent

    # A week later it is expiring again -> a fresh page.
    state = log.load_state()
    state["positions"][0]["short_calls"][0].update({"dte": 1})
    log.save_state(state)
    r = alerts.run()
    assert any(a["type"] == "EXPIRY_FRIDAY" for a in r["fired"])
    assert len(calls) == 2


def test_dry_run_and_disabled_channel_divert_to_the_log(monkeypatch):
    """The two settings that silently swallow a real page. Both must show up in
    the run report as the log channel, and nothing may reach the push service."""
    _seed(_position_needing_attention())
    webpush.add_subscription(_phone_subscription()[0])
    calls = _fake_pywebpush(monkeypatch)

    alerts.update_settings({"dry_run": True})
    r = alerts.run()
    assert any(a["type"] == "EXPIRY_FRIDAY" for a in r["fired"])
    assert r["delivery"] == [{"channel": "log", "ok": True, "dry_run": True}] and calls == []

    _seed(_position_needing_attention())  # fresh dedup state
    alerts.update_settings({"dry_run": False, "channels": {"webpush": False}})
    r = alerts.run()
    assert any(a["type"] == "EXPIRY_FRIDAY" for a in r["fired"])
    assert r["delivery"] == [{"channel": "log", "ok": True}] and calls == []


# ---------------------------------------------------------------------------
# 2. The real pywebpush: encrypt + VAPID-sign, decrypt as the phone would.
# ---------------------------------------------------------------------------
def test_real_pywebpush_encrypts_and_signs_what_the_phone_decrypts(monkeypatch):
    pytest.importorskip("pywebpush")
    http_ece = pytest.importorskip("http_ece")
    import requests
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives import serialization

    _seed(_position_needing_attention())
    sub, phone_priv, auth = _phone_subscription()
    webpush.add_subscription(sub)

    posts = []

    class _Resp:
        status_code = 201
        text = ""
        headers = {}

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)  # pywebpush posts via the module

    result = alerts.run()
    assert {"channel": "webpush", "ok": True} in result["delivery"], result["delivery"]
    assert len(posts) == 1
    url, req = posts[0]
    assert url == sub["endpoint"]
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["ttl"] == str(webpush.TTL_SECONDS)
    assert headers["urgency"] == "high"

    # The phone's side: decrypt with the subscription's private key + auth secret.
    plaintext = http_ece.decrypt(req["data"], private_key=phone_priv, auth_secret=auth,
                                 version="aes128gcm")
    payload = json.loads(plaintext)
    roll = next(a for a in result["fired"] if a["type"] == "EXPIRY_FRIDAY")
    assert payload["url"] == roll["action_url"]
    assert TICKER in payload["title"] and payload["tickers"] == [TICKER]

    # The push service's side: a VAPID JWT signed by the server's key, for THIS
    # push service's origin, with the contact subject. Verified with the public
    # key the phone enrolled with (the one /api/push/vapid-key hands out).
    authz = headers["authorization"]
    assert authz.startswith("vapid t=")
    parts = dict(p.strip().split("=", 1) for p in authz[len("vapid "):].split(","))
    assert parts["k"] == webpush.public_key()
    header_b64, claims_b64, sig_b64 = parts["t"].split(".")
    assert json.loads(_b64url_decode(header_b64))["alg"] == "ES256"
    claims = json.loads(_b64url_decode(claims_b64))
    origin = urlsplit(sub["endpoint"])
    assert claims["aud"] == f"{origin.scheme}://{origin.netloc}"
    assert claims["sub"] == webpush._subject()
    sig = _b64url_decode(sig_b64)
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), _b64url_decode(webpush.public_key()))
    pub.verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    assert isinstance(phone_priv, ec.EllipticCurvePrivateKey)  # keeps the import honest
    assert serialization  # noqa: B018 — imported for the key export above


# ---------------------------------------------------------------------------
# 3. Over HTTP — the routes the UI and an external cron hit.
# ---------------------------------------------------------------------------
@pytest.fixture()
def client():
    return app_module.app.test_client()


def test_run_route_pushes_the_alert(client, monkeypatch):
    _seed(_position_needing_attention())
    webpush.add_subscription(_phone_subscription()[0])
    calls = _fake_pywebpush(monkeypatch)
    body = client.post("/api/alerts/run", json={}).get_json()
    assert any(a["type"] == "EXPIRY_FRIDAY" for a in body["fired"])
    assert {"channel": "webpush", "ok": True} in body["delivery"]
    assert len(calls) == 1
    assert client.get("/api/alerts").get_json()["last_run"]["fired"] >= 1


def test_sample_alert_route_reports_a_deliverable_book(client, monkeypatch):
    """The operator's "would I actually get paged?" button: a sample alert for
    the book's own position, sent through the real channels, honestly reported."""
    _seed(_position_needing_attention())
    webpush.add_subscription(_phone_subscription()[0])
    calls = _fake_pywebpush(monkeypatch)
    before = log.load_state()["alerts"]

    body = client.post("/api/alerts/test").get_json()
    assert body["ok"] is True and body["delivered"] == ["webpush"]
    assert body["dry_run"] is False
    assert body["channels"]["webpush"] == {"configured": True, "enabled": True}
    assert body["alert"]["ticker"] == TICKER and body["alert"]["test"] is True
    assert body["alert"]["action_url"] == f"/?action=roll&ticker={TICKER}&reason=scheduled"
    assert "TEST" in body["alert"]["message"]
    payload = json.loads(calls[0]["data"])
    assert payload["title"].startswith("[CFM TEST · HIGH]") and TICKER in payload["title"]
    assert payload["url"] == body["alert"]["action_url"]
    assert payload["tag"] == "cfm-test"  # never replaces a real alert on the lock screen
    # Nothing persisted: not in the active set, not in the log, no last_run.
    assert log.load_state()["alerts"] == before


def test_sample_alert_route_exposes_the_settings_that_would_swallow_a_real_page(client, monkeypatch):
    _seed(_position_needing_attention())
    calls = _fake_pywebpush(monkeypatch)

    # No device enrolled, nothing else configured -> honest "would only hit the log".
    body = client.post("/api/alerts/test").get_json()
    assert body["ok"] is False and body["delivered"] == []
    assert body["delivery"] == [{"channel": "log", "ok": True}]
    assert "No channel" in body["verdict"] and calls == []

    # Device enrolled but dry run on -> still not delivered, and it says why.
    webpush.add_subscription(_phone_subscription()[0])
    client.post("/api/alerts/settings", json={"dry_run": True})
    body = client.post("/api/alerts/test").get_json()
    assert body["ok"] is False and body["dry_run"] is True
    assert "Dry run" in body["verdict"] and calls == []

    # Dry run off but the webpush channel switched off -> same honesty.
    client.post("/api/alerts/settings", json={"dry_run": False, "channels": {"webpush": False}})
    body = client.post("/api/alerts/test").get_json()
    assert body["ok"] is False and body["channels"]["webpush"]["enabled"] is False
    assert calls == []

    # Re-enable -> delivered.
    client.post("/api/alerts/settings", json={"channels": {"webpush": True}})
    body = client.post("/api/alerts/test").get_json()
    assert body["ok"] is True and len(calls) == 1


def test_sample_alert_route_reports_a_dead_device(client, monkeypatch):
    """A phone whose subscription the push service rejects: the report carries
    the service's reason instead of a green tick."""
    _seed(_position_needing_attention())
    webpush.add_subscription(_phone_subscription()[0])
    import sys
    import types

    class WebPushException(Exception):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    class _Resp:
        status_code = 401
        text = "bad jwt"

    mod = types.ModuleType("pywebpush")
    mod.WebPushException = WebPushException

    def fake_webpush(**kwargs):
        raise WebPushException("Push failed: 401 Unauthorized", _Resp())
    mod.webpush = fake_webpush
    monkeypatch.setitem(sys.modules, "pywebpush", mod)

    body = client.post("/api/alerts/test").get_json()
    assert body["ok"] is False
    assert "FAILED" in body["verdict"] and "401" in body["verdict"]


def test_sample_alert_without_positions_has_no_ticker():
    _seed()
    sample = alerts.sample_alert()
    assert sample["ticker"] is None and sample["action_url"] is None
    assert notifier.format_subject([sample]) == "[CFM TEST · HIGH] 1 alert(s) — portfolio"
