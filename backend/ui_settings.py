"""Operator UI preferences persisted in state metadata (per-store, survives
restarts) — the same pattern as strike_policy's posture.

``legacy_view`` (§4.7): the LEAP diagonal is retired as an active structure, so its
affordances (burn card, LEAP roll, accumulate, the LEAP execute flow) are hidden by
default. Turning Legacy view ON reveals existing legacy positions READ-ONLY for
review — it never re-enables opening/rolling a LEAP (that stays blocked at the
executor boundary, config.LEAP_LEGACY_READ_ONLY).
"""
from __future__ import annotations

import logging_handler as log


def get_legacy_view(state: dict | None = None) -> bool:
    state = state or log.load_state()
    return bool((state.get("metadata") or {}).get("legacy_view", False))


def set_legacy_view(enabled: bool) -> dict:
    state = log.load_state()
    state.setdefault("metadata", {})["legacy_view"] = bool(enabled)
    log.save_state(state)
    return {"legacy_view": bool(enabled)}
