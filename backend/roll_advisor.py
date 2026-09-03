"""roll_readiness — an ADVISORY-ONLY "is it worth rolling this early" signal for
the short-roll picker (``option_chain.roll_options``).

Theta decay is not linear: it accelerates as expiration approaches, so a
contract early in its life decays slowly and a same-strike roll into a fresh
contract usually trades cheap, fast-burning extrinsic for expensive,
slow-burning extrinsic — net LESS extrinsic captured over time. Rolling early
only pays when one of two triggers fires:

  - most of this contract's extrinsic is already banked (nothing meaningful
    left to wait for), or
  - the strike itself no longer offers real downside cushion, independent of
    how much extrinsic has decayed.

This module computes that signal and nothing else. It carries ZERO authority:
it never blocks a roll, never changes which strikes/expirations the picker
offers, and it must never be used to gate execution — see CLAUDE.md "Shadow
mode" for the house convention this follows. PURE: no I/O, callers supply the
already-computed inputs (``position_manager.enrich_short`` is the usual
source for ``extrinsic_captured_pct`` and the ITM buffer).
"""
from __future__ import annotations

import config


def roll_readiness(extrinsic_captured_pct: float | None,
                   itm_buffer_pct: float | None,
                   dte: int | None) -> dict:
    """Is there still real theta to collect, or is this contract effectively
    "done" and safe to roll early?

    ``ready`` is True when either trigger fires:
      - ``extrinsic_captured_pct >= config.ROLL_READY_DECAY_PCT`` (default 80%)
      - ``itm_buffer_pct is not None and itm_buffer_pct < config.ROLL_READY_ITM_FLOOR_PCT``
        (default 3%) — pass None here when the short isn't ITM; a buffer only
        means something once the strike is already breached.

    Both inputs unmeasurable -> ``ready`` is None (unmeasured, not "not
    ready" and not "ready") rather than a false negative.
    """
    reasons: list[str] = []
    if extrinsic_captured_pct is not None and extrinsic_captured_pct >= config.ROLL_READY_DECAY_PCT:
        reasons.append("DECAY_CAPTURED")
    if itm_buffer_pct is not None and itm_buffer_pct < config.ROLL_READY_ITM_FLOOR_PCT:
        reasons.append("ITM_BUFFER_THIN")

    measured = extrinsic_captured_pct is not None or itm_buffer_pct is not None
    ready = None if not measured else bool(reasons)

    return {
        "ready": ready,
        "reasons": reasons,
        "extrinsic_captured_pct": extrinsic_captured_pct,
        "itm_buffer_pct": itm_buffer_pct,
        "decay_threshold_pct": config.ROLL_READY_DECAY_PCT,
        "itm_floor_pct": config.ROLL_READY_ITM_FLOOR_PCT,
        "dte": dte,
        "advisory": True,
    }
