"""Autopilot — the ONE master switch over every unattended action.

The per-trigger grants stay exactly as they were (circuit_breaker's auto-exit
conditions, recommendation_auto_execute's roll/defend triggers): they say WHAT
autopilot may do. This says WHETHER it is running at all, so the operator can
turn everything off when they are at the screen and back on when they walk away,
without touching — or losing — the individual grants.

  * ON  -> the runner acts on whichever triggers are granted (unchanged).
  * OFF -> nothing acts unattended; recommendations still emit and notify, and
           every card is still executable by hand.

Never set means ON, so a book that already had triggers granted before this
switch existed keeps behaving exactly as it did. Persisted in state.metadata
(per account / per store), like the grants themselves.
"""
from __future__ import annotations

import logging_handler as log

_KEY = "autopilot_enabled"


def enabled(state: dict | None = None) -> bool:
    state = state if state is not None else log.load_state()
    stored = (state.get("metadata") or {}).get(_KEY)
    return True if stored is None else bool(stored)


def set_enabled(on: bool) -> dict:
    log.mutate_state(lambda st: st.setdefault("metadata", {}).__setitem__(_KEY, bool(on)))
    return status()


def status(state: dict | None = None) -> dict:
    import circuit_breaker
    import recommendation_auto_execute as auto_exec
    state = state if state is not None else log.load_state()
    exits = circuit_breaker.get_auto_exit_permissions(state)
    rolls = auto_exec.get_permissions(state)
    return {"enabled": enabled(state),
            "granted": sorted([f"exit:{k}" for k, v in exits.items() if v]
                              + [f"roll:{k}" for k, v in rolls.items() if v]),
            "circuit_breaker_auto_exit": exits, "roll_defend_auto_execute": rolls}
