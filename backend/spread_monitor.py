"""Trailing bid-ask spread baseline per traded option contract.

The execution gate's spread-quality check (``execution_gate.spread_quality``) needs
a *trailing average* spread to judge whether the current spread is abnormally wide.
This module maintains that baseline — with **no new polling**: samples are the
bid-ask spreads of quotes the data layer already fetches (the option chain is
cached every 5 min in ``option_chain``, and every executed roll/exit already has
the traded contract's bid/ask in hand). A short ring buffer per contract feeds a
mean; until ``SPREAD_BASELINE_MIN_SAMPLES`` samples exist there is **no baseline**
(the check returns a "no baseline" state rather than a fabricated average).

Pure state helpers: every function takes and returns the ``state`` dict, reads no
clock, does no I/O. The store lives under ``state["spread_baselines"]`` (seeded by
the schema-v18 migration; the helpers ``setdefault`` it so they are safe on an
un-migrated load too)."""
from __future__ import annotations

import config

# How many recent spread samples to retain per contract (a small trailing window;
# the baseline is the mean of these). PROPOSED_DEFAULT sizing.
_WINDOW = 20


def _store(state: dict) -> dict:
    return state.setdefault("spread_baselines", {})


def spread_of(bid, ask) -> float | None:
    """The bid-ask spread, or ``None`` when either side is missing / crossed. This
    is the same ``ask - bid`` convention as ``burn.exit_slippage_est``."""
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    spread = a - b
    return spread if spread >= 0 else None


def record(state: dict, symbol: str, bid, ask) -> float | None:
    """Append one spread sample for ``symbol`` (keyed by the option contract symbol)
    from an already-fetched quote. Returns the sample recorded (or ``None`` if the
    quote was unusable). Caps the ring buffer at ``_WINDOW``."""
    sym = (symbol or "").strip().upper()
    sample = spread_of(bid, ask)
    if not sym or sample is None:
        return None
    store = _store(state)
    rec = store.setdefault(sym, {"samples": []})
    samples = rec.setdefault("samples", [])
    samples.append(round(float(sample), 4))
    if len(samples) > _WINDOW:
        del samples[:-_WINDOW]
    return sample


def baseline(state: dict, symbol: str) -> float | None:
    """The trailing mean spread for ``symbol``, or ``None`` when fewer than
    ``SPREAD_BASELINE_MIN_SAMPLES`` samples exist ("no baseline" — never fabricated)."""
    sym = (symbol or "").strip().upper()
    rec = _store(state).get(sym) or {}
    samples = rec.get("samples") or []
    if len(samples) < config.SPREAD_BASELINE_MIN_SAMPLES:
        return None
    return sum(samples) / len(samples)
