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


def suggest_earnings_strike(price: float, atr_value: float, regime_status: str | None,
                            posture: str | None = None) -> dict:
    """Deep-ITM protective strike for rolling a short THROUGH an earnings report.
    Takes the deeper of the regime/posture cell and the earnings floors
    (config.EARNINGS_ROLL_*), so it never rolls shallower than the regime would."""
    entry = table_entry(regime_status, posture)
    atr_mult = max(entry["atr_mult"], config.EARNINGS_ROLL_ATR_MULT)
    itm_pct = max(entry["itm_pct"], config.EARNINGS_ROLL_ITM_PCT)
    strike = indicators.short_strike_from_table(price, atr_value, atr_mult, itm_pct)
    return {**entry, "atr_mult": atr_mult, "itm_pct": itm_pct,
            "strike": strike, "earnings_protected": True}


def regime_target_strike(price: float, atr_value: float, regime_status: str | None,
                         current_strike: float | None = None,
                         itm_floor_pct: float | None = None) -> dict:
    """The Roll dialog's OWN target strike — Travis's documented regime-depth
    policy (config.STRIKE_ATR_MULT_GREEN/YELLOW, HARD_CFM_RULE), independent of
    STRIKE_TABLE and the operator posture toggle. Scoped to the roll dialog only
    (option_chain.roll_options): entry, defend, and the roll-down selector keep
    calling ``suggest_strike``/STRIKE_TABLE unchanged — this does not resolve the
    STRIKE_TABLE-vs-documented-multiples reconciliation noted in config.py, it is
    a roll-dialog-scoped application of the numbers Travis specified.

    GREEN uses 1.5xATR, YELLOW (and any unrecognized regime — never silently
    thinner than documented) uses 2.0xATR. RED additionally caps the target at
    ``current_strike`` — no roll-UP is permitted in RED, roll down/out only
    (TRAVIS_EXTENSION). The ITM% floor (default
    config.ROLL_REGIME_TARGET_ITM_FLOOR_PCT, TRAVIS_EXTENSION) applies under all
    three regimes as a hard floor beneath whichever ATR-multiple target results.
    Rounded to $0.50 via the same convention as ``short_strike_from_table``."""
    floor_pct = (itm_floor_pct if itm_floor_pct is not None
                else config.ROLL_REGIME_TARGET_ITM_FLOOR_PCT)
    atr_mult = (config.STRIKE_ATR_MULT_GREEN if regime_status == "green"
               else config.STRIKE_ATR_MULT_YELLOW)
    atr_strike = price - atr_mult * atr_value
    itm_strike = price * (1 - floor_pct)
    raw_rule = min(atr_strike, itm_strike)
    rule_strike = round(raw_rule * 2) / 2
    roll_up_blocked = regime_status == "red" and current_strike is not None
    raw = min(raw_rule, current_strike) if roll_up_blocked else raw_rule
    strike = round(raw * 2) / 2
    return {
        "regime": regime_status, "atr_mult": atr_mult, "itm_pct": floor_pct,
        "atr_strike": round(atr_strike, 2), "itm_strike": round(itm_strike, 2),
        # rule_strike/raw_rule_target: what the ATR-mult/ITM-floor rule alone
        # implies, BEFORE any RED roll-up cap — kept distinct from strike/
        # raw_target (the final, possibly-capped value) so a RED roll-up-blocked
        # dialog can show both "what the rule wants" and "what you're capped to".
        "rule_strike": rule_strike, "raw_rule_target": round(raw_rule, 4),
        "raw_target": round(raw, 4), "strike": strike,
        "roll_up_blocked": roll_up_blocked,
    }


def apply_deadband(prior_strike: float | None, raw_target: float | None,
                   atr_value: float | None,
                   deadband_atr_mult: float | None = None) -> dict:
    """Should the Roll dialog's DISPLAYED DEFAULT strike move off ``prior_strike``
    to track a fresh ``raw_target`` (the continuous, pre-rounding regime target)?

    Holds ``prior_strike`` unless the continuous target has drifted more than
    ``deadband_atr_mult`` ATRs (default config.ROLL_TARGET_DEADBAND_ATR_MULT,
    PROPOSED_DEFAULT) past it — so a few-cent spot wobble can't flip the default
    strike shown mid-decision. Display-only: the full strike LIST a caller offers
    alongside this must keep tracking ``raw_target`` live regardless of what this
    returns (see option_chain.roll_options). PURE."""
    mult = deadband_atr_mult if deadband_atr_mult is not None else config.ROLL_TARGET_DEADBAND_ATR_MULT
    if prior_strike is None or raw_target is None or atr_value is None:
        return {"strike": None, "held": False}
    band = mult * atr_value
    if abs(raw_target - prior_strike) <= band:
        return {"strike": prior_strike, "held": True}
    return {"strike": round(raw_target * 2) / 2, "held": False}
