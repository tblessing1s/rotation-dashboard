"""Native Web Push (VAPID) — the browser/PWA push channel.

The PWA registers a service worker and subscribes to the browser's push
service; that subscription (endpoint + keys) is POSTed to /api/push/subscribe
and stored in state.json under ``alerts.push_subscriptions``. When an alert
batch is dispatched, :func:`send` signs a payload with the server's VAPID
private key and hands it to each subscription's push endpoint (FCM on Android
Chrome). The browser wakes the service worker, which shows the notification —
so alerts reach the phone's lock screen even when the app is closed.

Why this AND ntfy: ntfy needs a separate app; Web Push is self-contained in the
installed PWA. Both are wired as channels so either (or both) can be enabled.

Keys are NOT required config. If you set them explicitly (both env vars) they
are used verbatim; otherwise the app generates a keypair ONCE and persists it to
``DATA_DIR/.vapid_keys.json`` (0600, on the Fly volume), reused across deploys
and restarts — the same self-configuring pattern as the session-signing key in
auth.py. So push works out of the box with zero setup; explicit keys are only
for operators who want to manage them centrally.

    VAPID_PUBLIC_KEY   base64url raw public key — also handed to the browser as
                       the applicationServerKey at subscribe time.
    VAPID_PRIVATE_KEY  base64url raw private key — signs the push JWT.
    VAPID_SUBJECT      contact URI for the push service (mailto:you@… or a URL).

Keys stay STABLE once generated (rotating them invalidates every device's
subscription), which is exactly why they're persisted rather than regenerated
per boot.
"""
from __future__ import annotations

import base64
import json
import logging
import os

import config
import logging_handler as log

logger = logging.getLogger("cfm.alerts")

# Subscriptions the push service has permanently rejected get pruned on send.
_GONE_STATUS = {404, 410}

# Auto-generated keypair persisted here when the env vars are not set.
_VAPID_FILE = os.path.join(config.DATA_DIR, ".vapid_keys.json")
_cache: dict | None = None  # in-memory {"public":…, "private":…}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_keypair() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    priv = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
    pub = _b64url(key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint))
    return {"public": pub, "private": priv}


def _persisted_keys() -> dict:
    """Load the persisted VAPID pair, generating+saving it once on first use.

    Never raises: if the keypair can't be persisted (read-only FS) it is kept in
    memory for this process — push still works until restart. Returns {} only if
    key generation itself fails (cryptography missing), disabling the channel.
    """
    global _cache
    if _cache:
        return _cache
    try:
        with open(_VAPID_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("public") and data.get("private"):
            _cache = {"public": data["public"], "private": data["private"]}
            return _cache
    except (OSError, ValueError):
        pass
    try:
        _cache = _generate_keypair()
    except Exception as e:  # noqa: BLE001 — no crypto -> channel simply stays off
        logger.error("VAPID key generation failed: %s", e)
        return {}
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        fd = os.open(_VAPID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_cache, fh)
    except OSError:
        pass  # in-memory only (resets on restart) — still usable this run
    return _cache


def _keys() -> dict:
    """Explicit env pair (only when BOTH are set) else the persisted pair."""
    epub = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
    epriv = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    if epub and epriv:
        return {"public": epub, "private": epriv}
    return _persisted_keys()


def public_key() -> str:
    """The VAPID application server key the browser needs to subscribe."""
    return _keys().get("public", "")


def _private_key() -> str:
    return _keys().get("private", "")


def _subject() -> str:
    # A valid VAPID "sub" claim must be a mailto: or https: URI.
    subj = (os.environ.get("VAPID_SUBJECT") or "").strip()
    return subj or "mailto:alerts@example.com"


def keys_configured() -> bool:
    """True when a VAPID keypair is available (explicit or auto-generated)."""
    k = _keys()
    return bool(k.get("public") and k.get("private"))


# ---------------------------------------------------------------------------
# Subscription storage (state.json: alerts.push_subscriptions — list of dicts,
# each the raw browser PushSubscription JSON keyed by its unique endpoint URL).
# ---------------------------------------------------------------------------
def _subs_container(state: dict) -> list[dict]:
    return state.setdefault("alerts", {}).setdefault("push_subscriptions", [])


def list_subscriptions(state: dict | None = None) -> list[dict]:
    state = state or log.load_state()
    return list(_subs_container(state))


def subscription_count() -> int:
    return len(list_subscriptions())


def add_subscription(sub: dict) -> dict:
    """Persist a browser PushSubscription (idempotent on its endpoint)."""
    endpoint = (sub or {}).get("endpoint")
    if not endpoint or not (sub.get("keys") or {}).get("p256dh") or not sub["keys"].get("auth"):
        raise ValueError("invalid push subscription (missing endpoint/keys)")
    state = log.load_state()
    subs = _subs_container(state)
    record = {"endpoint": endpoint, "keys": sub["keys"],
              "added_at": log.utcnow()}
    for i, existing in enumerate(subs):
        if existing.get("endpoint") == endpoint:
            record["added_at"] = existing.get("added_at", record["added_at"])
            subs[i] = record
            log.save_state(state)
            return {"ok": True, "updated": True, "count": len(subs)}
    subs.append(record)
    log.save_state(state)
    return {"ok": True, "updated": False, "count": len(subs)}


def remove_subscription(endpoint: str) -> dict:
    state = log.load_state()
    subs = _subs_container(state)
    before = len(subs)
    subs[:] = [s for s in subs if s.get("endpoint") != endpoint]
    log.save_state(state)
    return {"ok": True, "removed": before - len(subs), "count": len(subs)}


def _prune(endpoints: set[str]) -> None:
    """Drop endpoints the push service has permanently rejected."""
    if not endpoints:
        return
    state = log.load_state()
    subs = _subs_container(state)
    subs[:] = [s for s in subs if s.get("endpoint") not in endpoints]
    log.save_state(state)


def configured() -> bool:
    """Channel is deliverable only when keys are set AND a device subscribed."""
    return keys_configured() and subscription_count() > 0


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def _payload(subject: str, body: str, alerts: list[dict]) -> str:
    worst = alerts[0].get("severity") if alerts else "ALERT"
    tickers = sorted({a["ticker"] for a in alerts if a.get("ticker")})
    return json.dumps({
        "title": subject,
        "body": body,
        "severity": worst,
        "count": len(alerts),
        "tickers": tickers,
        "tag": "cfm-alerts",
        "url": "/",
    })


def send(subject: str, body: str, alerts: list[dict]) -> None:
    """Push one batch to every stored subscription. Prunes dead subscriptions.

    Raises on a hard misconfiguration (missing library/keys) so the notifier
    records the channel as failed; per-subscription delivery errors are absorbed
    (one dead phone must not block the others).
    """
    if not keys_configured():
        raise RuntimeError("VAPID keys not configured")
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as e:  # pragma: no cover - dep missing in a stripped env
        raise RuntimeError("pywebpush not installed") from e

    subs = list_subscriptions()
    if not subs:
        return
    data = _payload(subject, body, alerts)
    claims = {"sub": _subject()}
    priv = _private_key()
    dead: set[str] = set()
    sent = 0
    for sub in subs:
        info = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
        try:
            webpush(subscription_info=info, data=data,
                    vapid_private_key=priv, vapid_claims=dict(claims), timeout=20)
            sent += 1
        except WebPushException as e:  # noqa: PERF203 — per-sub isolation
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in _GONE_STATUS:
                dead.add(sub["endpoint"])
            logger.error("web push to %s… failed (%s): %s",
                         sub["endpoint"][:40], code, e)
        except Exception as e:  # noqa: BLE001 — never let one sub sink the batch
            logger.error("web push to %s… errored: %s", sub["endpoint"][:40], e)
    _prune(dead)
    if sent == 0 and subs:
        raise RuntimeError(f"web push reached 0/{len(subs)} devices")
