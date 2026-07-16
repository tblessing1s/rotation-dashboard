"""The single shared scan VERDICT — the worst-signal-wins composition of the three
scan signals into READY / CAUTION / WATCH / BLOCKED.

This is the ONE place the composition lives. The entry gate, the scorecard
display, and /api/scan/ready all call ``compose_verdict`` instead of re-deriving a
verdict of their own (today there are three divergent computations — the stock
lights verdict, the gate's cleared-level, and the scorecard's GO/CAUTION/AVOID;
this replaces them with one shared function, the same principle as the
recommendation record sharing the future automation code path).

Inputs — each an already-computed signal, so this module stays PURE (no I/O, no
clock, no fetching):

  * Market Genius (regime)  — the published market regime color. This is the
    INVISIBLE input: there is no per-row regime column, but a RED regime BLOCKS
    every row (the Level-1 entry rule, unchanged).
  * Symbol Genius (SYM)     — the per-name four-light color (``symbol_genius``).
  * Structure               — the (BaseStage, InstFlow) cell, mapped through
    ``structure_classifier.structure_entrability``.

Each input is placed on one severity ladder and the WORST (most restrictive)
input wins:

    READY  <  CAUTION  <  WATCH  <  BLOCKED

    regime : green -> READY    yellow -> WATCH    red -> BLOCKED
    symbol : green -> READY    yellow -> WATCH    red -> BLOCKED   (YELLOW = watchlist, never enterable)
    struct : READY / CAUTION / WATCH / BLOCKED straight from the grid

READY therefore means ALL THREE inputs are clear; a single blocking input (a red
regime, a red SYM, or a topping/declining/distributing structure) forces BLOCKED
regardless of the other two. A missing/unknown regime or SYM color degrades to
WATCH (never READY) — the scan never emits an entrable verdict it can't stand behind.
"""
from __future__ import annotations

import genius_lights
import structure_classifier as sclf

# Final verdict vocabulary (also the structure grid's vocabulary, so the two align).
READY = "READY"
CAUTION = "CAUTION"
WATCH = "WATCH"
BLOCKED = "BLOCKED"

# Severity ladder — worst (highest) wins.
_SEVERITY = {READY: 0, CAUTION: 1, WATCH: 2, BLOCKED: 3}

# A Genius color (regime or SYM) placed on the ladder. GREEN clears, YELLOW is a
# watchlist "wait" (never entrable), RED blocks. Anything unknown -> WATCH.
_COLOR_LEVEL = {
    genius_lights.GREEN: READY,
    genius_lights.YELLOW: WATCH,
    genius_lights.RED: BLOCKED,
}


def _color_level(color: str | None) -> str:
    return _COLOR_LEVEL.get(color, WATCH)


def compose_verdict(regime_color: str | None, symbol_color: str | None,
                    base_stage: str, inst_flow: str) -> dict:
    """Compose the three scan signals into a single VERDICT (worst-signal-wins).

    ``regime_color`` / ``symbol_color`` are Genius colors ("green"/"yellow"/"red");
    ``base_stage`` / ``inst_flow`` are the classifier enums. Returns the verdict,
    the structure-cell entrability it derived, the per-input levels, and the
    reason(s) — the input(s) at the worst level — for the detail drawer / blocked_by.
    PURE: no I/O.
    """
    structure = sclf.structure_entrability(base_stage, inst_flow)
    inputs = {
        "regime": _color_level(regime_color),
        "symbol": _color_level(symbol_color),
        "structure": structure,
    }
    verdict = max(inputs.values(), key=lambda level: _SEVERITY[level])
    worst = _SEVERITY[verdict]
    # The input(s) that drove the verdict (excluding any that are merely READY).
    reasons = [f"{name}:{level}" for name, level in inputs.items()
               if _SEVERITY[level] == worst and level != READY]
    return {
        "verdict": verdict,
        "structure_entrability": structure,
        "inputs": inputs,
        "reasons": reasons,
    }


def is_ready(verdict: dict) -> bool:
    """True iff the composed verdict is READY — the Ready-to-Enter membership test."""
    return verdict.get("verdict") == READY
