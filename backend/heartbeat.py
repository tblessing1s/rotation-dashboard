"""Dead-man's switch — make the alert scheduler's SILENCE page you.

The alert engine is excellent, but the in-process scheduler is the single point
of failure in the whole alerting promise: if its daemon thread wedges, throws
its way out of the loop, or the Fly machine stops, then no condition is ever
evaluated and *nothing tells you*. Sixteen conditions and native push are worth
nothing if the thing that runs them goes quiet.

The fix is an outbound heartbeat to an external dead-man service (healthchecks.io
or any URL that pages on missed pings). The scheduler pings it every tick while
it's alive; miss enough pings and the service alerts you. The point being: the
detector lives OUTSIDE the process it's watching, so it survives exactly the
failures the in-process scheduler can't self-report.

Config (inert when unset — the app runs fine without it):
  HEALTHCHECK_URL            base ping URL (e.g. https://hc-ping.com/<uuid>).
  HEALTHCHECK_MIN_INTERVAL   seconds between liveness pings (default 300). Keeps
                             the every-30s tick loop from hammering the service;
                             /fail pings ignore it.

Semantics: a plain ping = "alive and ticking" (throttled); a ``/fail`` ping =
"I'm alive but my alert run just threw" (sent immediately, so a persistently
broken evaluator pages too, not only a dead thread). Configure the service's
period+grace to your taste (e.g. period 1h, grace 1h catches a wedge/stop within
~2h any day, including weekends when no alert slots fire).
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests

logger = logging.getLogger("cfm.alerts")

_lock = threading.Lock()
_last_ping = 0.0  # monotonic seconds of the last throttled liveness ping


def url() -> str:
    return (os.environ.get("HEALTHCHECK_URL") or "").strip()


def configured() -> bool:
    return bool(url())


def _min_interval() -> float:
    try:
        return float(os.environ.get("HEALTHCHECK_MIN_INTERVAL", "300"))
    except ValueError:
        return 300.0


def ping(suffix: str = "", force: bool = False) -> bool:
    """Ping the dead-man service. Liveness pings (no suffix) are throttled to
    HEALTHCHECK_MIN_INTERVAL; ``/fail`` and forced pings always send. Returns
    True if a request was sent. Never raises — a failed ping must not kill the
    scheduler thread that calls it.
    """
    global _last_ping
    base = url()
    if not base:
        return False
    if not suffix and not force:
        with _lock:
            now = time.monotonic()
            if _last_ping and (now - _last_ping) < _min_interval():
                return False
            _last_ping = now
    try:
        requests.post(base.rstrip("/") + suffix, timeout=10)
        return True
    except Exception as e:  # noqa: BLE001 — a failed ping must never propagate
        logger.warning("heartbeat ping%s failed: %s", suffix or "", e)
        return False
