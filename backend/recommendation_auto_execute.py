"""ROLL/DEFEND auto-execute permissions — opt-in, per-trigger, default OFF.

Generalizes the precedent in circuit_breaker.py's AUTO-EXIT PERMISSIONS
section (EXIT-only, circuit-breaker conditions) to the roll/defend trigger
family: the operator may grant ONE specific TriggerRule the authority to act
UNATTENDED. Only the four triggers in AUTO_EXECUTE_TRIGGERS are eligible —
every other trigger (every EXIT trigger included, which keeps its own
separate circuit-breaker permission set) has no automation path here at all.
Granting a trigger does not change what recommendation_engine.evaluate()
computes or displays one bit — it only decides whether
recommendation_runner._check_roll_defend_auto_execute is ALLOWED to call
executor.execute() on the SAME rec + proposed_ticket a human would otherwise
have to click "Execute" on. That function is the only caller.

Deliberately does NOT gate on the trust scoreboard's graduation criteria
(docs/trust-layer.md) — same posture as the existing circuit-breaker
auto-exit: opt-in + a confirm-to-enable UI is the safety rail, not an
automated trust-earning check (which is also currently unreachable —
RECONCILED_CLEAN is NOT_YET_IMPLEMENTED there). A future change could add
that gate; this module does not assume it.
"""
from __future__ import annotations

import logging_handler as log
from rec_types import TriggerRule

# Persisted like strike_policy's posture / circuit_breaker's auto-exit perms:
# state.metadata, per-store (live/demo never share a grant), surviving
# restarts. Default OFF for every trigger.
AUTO_EXECUTE_TRIGGERS = (
    TriggerRule.ROLL_SCHEDULED_WEEKLY,
    TriggerRule.ROLL_75PCT,
    TriggerRule.ROLL_EXTRINSIC_CAPTURED,
    TriggerRule.DEFEND_BELOW_STRIKE,
)
_AUTO_EXECUTE_KEY = "roll_defend_auto_execute"


def get_permissions(state: dict | None = None) -> dict:
    """{trigger_rule: bool} — always all four AUTO_EXECUTE_TRIGGERS keys,
    defaulting False for any trigger never explicitly granted."""
    state = state if state is not None else log.load_state()
    stored = (state.get("metadata") or {}).get(_AUTO_EXECUTE_KEY) or {}
    return {t: bool(stored.get(t)) for t in AUTO_EXECUTE_TRIGGERS}


def set_permission(trigger_rule: str, on: bool) -> dict:
    """Grant or revoke ONE trigger's auto-execute permission. Raises on an
    unrecognized trigger rather than silently no-op-ing a typo."""
    if trigger_rule not in AUTO_EXECUTE_TRIGGERS:
        raise ValueError(f"trigger_rule must be one of {AUTO_EXECUTE_TRIGGERS}")
    state = log.load_state()
    perms = state.setdefault("metadata", {}).setdefault(_AUTO_EXECUTE_KEY, {})
    perms[trigger_rule] = bool(on)
    log.save_state(state)
    return get_permissions(state)


def _leg(ticket: dict, instruction: str) -> dict:
    return next((l for l in (ticket.get("legs") or []) if l.get("instruction") == instruction), {})


def payload_from_ticket(rec: dict) -> dict | None:
    """The executor.execute() payload for a roll/defend rec's proposed_ticket —
    the SAME strikes/reason a human would submit from the card, plus
    ``source_rec_id`` (so the fill's trust-matching prefers this exact record)
    and a deterministic ``client_order_ref`` (so a repeated pass on a still-open
    rec can never double-submit a live order).

    Carries only ``to_dte`` for the new leg, never a computed expiration date —
    the ticket's own docstring is explicit that it is "estimates only... the
    staged order re-prices from the live chain". A live submission needs a
    REAL listed expiration; resolve_live_expiration() does that lookup
    separately, at submit time, so a stale/estimated date is never sent to the
    broker. Returns None when the ticket isn't the roll_short shape this needs
    (never true for a real ROLL_OUT/DEFEND rec, but defensive against a
    malformed or foreign record)."""
    ticket = rec.get("proposed_ticket") or {}
    if ticket.get("action") != "roll_short":
        return None
    close_leg = _leg(ticket, "BUY_TO_CLOSE")
    open_leg = _leg(ticket, "SELL_TO_OPEN")
    if close_leg.get("strike") is None or open_leg.get("strike") is None:
        return None
    return {
        "action": "roll_short",
        "ticker": rec.get("ticker"),
        "contracts": ticket.get("contracts"),
        "from_strike": close_leg.get("strike"),
        "from_expiration": close_leg.get("expiration"),
        "to_strike": open_leg.get("strike"),
        "to_dte": open_leg.get("dte"),
        "roll_reason": ticket.get("roll_reason"),
        "source_rec_id": rec.get("rec_id"),
        "client_order_ref": f"auto:{rec.get('rec_id')}",
    }


def emission_dte(rec: dict, strike, expiration) -> int | None:
    """The short's own DTE at the moment this rec was emitted, read from
    ``input_snapshot.shorts`` (present on every rec regardless of trigger —
    unlike ``trigger_detail``, which DEFEND_BELOW_STRIKE doesn't carry a dte
    on). Matched by strike + expiration since a position can hold more than
    one open short. None when no match is found (state has since diverged
    from what the rec was emitted against)."""
    shorts = (rec.get("input_snapshot") or {}).get("shorts") or []
    return next((s.get("dte") for s in shorts
                if s.get("strike") == strike and s.get("expiration") == expiration), None)


def resolve_live_expiration(ticker: str, from_expiration: str | None,
                            same_week: bool) -> str | None:
    """A REAL listed expiration to roll the new leg into, from the live chain —
    never a date computed by arithmetic on a target dte. ``same_week`` True
    keeps the exact contract the short already has (a same-expiration roll-up
    or defend roll-down); False picks the next weekly boundary after it.
    None when the chain has no such expiration (never guessed — the caller
    must refuse the auto-submission rather than send an invented date)."""
    if same_week:
        return from_expiration
    import option_chain
    import schwab_api
    payload = option_chain._fetch_chain(ticker)
    _, contracts = schwab_api.parse_call_chain(payload)
    if not contracts:
        return None
    weeklies = option_chain._weekly_expirations(contracts, count=3)
    return next((e for e in weeklies if e != from_expiration), None)
