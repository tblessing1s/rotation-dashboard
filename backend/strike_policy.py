"""Weekly short strike selection: market regime x operator risk posture.

Reference: the "Genius System" market-timing table — for each market regime
(green/yellow/red) and risk posture (aggressive/conservative) it specifies an
ATR multiplier and a minimum ITM% floor for the weekly short strike. The two
candidates are combined by taking whichever sits further below spot (see
indicators.short_strike_from_table); config.STRIKE_TABLE holds the numbers.

Posture is an operator-editable, persisted setting (like the demo/live
toggle) stored in state metadata so it survives restarts and is per-store
(live and demo can hold different postures). RED still blocks new entries
(the Level 1 regime gate is unchanged) — the RED row here only feeds the
defend/roll-down strike selector for an already-open position.
"""
from __future__ import annotations

import config
import indicators
import logging_handler as log


def get_posture(state: dict | None = None) -> str:
    state = state or log.load_state()
    posture = (state.get("metadata") or {}).get("strike_posture")
    return posture if posture in config.STRIKE_POSTURES else config.DEFAULT_STRIKE_POSTURE


def set_posture(posture: str) -> dict:
    posture = (posture or "").strip().lower()
    if posture not in config.STRIKE_POSTURES:
        raise ValueError(f"posture must be one of {config.STRIKE_POSTURES}")
    state = log.load_state()
    state.setdefault("metadata", {})["strike_posture"] = posture
    log.save_state(state)
    return {"posture": posture}


def table_entry(regime_status: str | None, posture: str | None = None) -> dict:
    """(atr_mult, itm_pct) for one regime/posture cell. Unknown/missing regime
    falls back to yellow (matches the old REGIME_ATR_MULT fallback)."""
    posture = posture if posture in config.STRIKE_POSTURES else get_posture()
    row = config.STRIKE_TABLE.get(regime_status or "", config.STRIKE_TABLE["yellow"])
    atr_mult, itm_pct = row.get(posture, row[config.DEFAULT_STRIKE_POSTURE])
    return {"regime": regime_status, "posture": posture, "atr_mult": atr_mult, "itm_pct": itm_pct}


def suggest_strike(price: float, atr_value: float, regime_status: str | None,
                   posture: str | None = None) -> dict:
    """Full suggestion: the table cell plus the resolved strike."""
    entry = table_entry(regime_status, posture)
    strike = indicators.short_strike_from_table(price, atr_value, entry["atr_mult"], entry["itm_pct"])
    return {**entry, "strike": strike}
