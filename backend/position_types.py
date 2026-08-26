"""Position-type discriminator for the shares-primary migration (schema v20).

Every position carries a ``position_type``:

- ``LEAP_PMCC_LEGACY`` — the legacy diagonal (deep-ITM LEAP long + short call).
  READ ONLY: existing history renders and prices from the immutable log, but no
  new LEAP may be opened, rolled, or recommended.
- ``SHARES`` — the active base leg is real shares (delta == 1.0, zero extrinsic,
  no burn, no DTE). The short call covers the owned shares instead of a LEAP.
- ``CASH_SECURED_PUT`` — a short put with cash collateral and **no base leg at
  all** (schema v22). It holds no shares and no long option; its capital is
  ``strike x 100 x contracts`` of collateral. On assignment it becomes a SHARES
  position at the strike and hands off to the covered-call machinery unchanged.

Absence (a position with no discriminator — e.g. a half-built skeleton, or any
record that predates the migration and somehow wasn't backfilled) degrades to
LEGACY so the pre-migration behavior is preserved everywhere. Every non-legacy
path is opt-in by an explicit tag, never by omission — this is the load-bearing
rule that keeps burn/payback/coverage removal from ever leaking into a legacy
position.

WHY THE LEGACY DEFAULT IS DANGEROUS FOR A PUT, AND WHAT IS DONE ABOUT IT
-----------------------------------------------------------------------
The absence-degrades-to-LEGACY bias is right for shares and WRONG for a put: a
LEAP diagonal and a cash-secured put share no structure at all, so an untagged
put resolving to LEGACY would be read as a position holding a long call it does
not have. The protection is that ``of()`` never INFERS a put — a position is a
put only when it carries the explicit tag, which only ``executor._put_opened``
writes. There is deliberately no shape-sniffing fallback (e.g. "has short_puts
and no shares therefore a put"): a half-built skeleton must stay legacy-shaped
and visibly wrong rather than silently becoming a put.

The corollary that matters more: the SHARE-BASED readouts (coverage ratio, roll
drag, share exposure) are UNDEFINED for a put, not zero. ``covered_lots(0)``
would happily return ``coverable_lots: 0`` — a confident answer to a question
that should not have been asked. Callers must branch on :func:`is_put` and render
not-applicable; see ``position_manager.enrich_position``.
"""
from __future__ import annotations

LEAP_PMCC_LEGACY = "LEAP_PMCC_LEGACY"
SHARES = "SHARES"
CASH_SECURED_PUT = "CASH_SECURED_PUT"

ALL = frozenset({LEAP_PMCC_LEGACY, SHARES, CASH_SECURED_PUT})

# The types that hold, or will hold, real shares as their base leg. A put is NOT
# here: it holds collateral until assignment converts it into a SHARES position.
_EXPLICIT = {SHARES, CASH_SECURED_PUT}


def of(position) -> str:
    """The canonical position_type for a position dict.

    Anything that is not an EXPLICIT tag resolves to ``LEAP_PMCC_LEGACY`` (the
    safe default for pre-v20 records). A put is never inferred from shape — see
    the module docstring for why that matters."""
    if not isinstance(position, dict):
        return LEAP_PMCC_LEGACY
    tag = position.get("position_type")
    return tag if tag in _EXPLICIT else LEAP_PMCC_LEGACY


def is_shares(position) -> bool:
    """True only for a position explicitly tagged SHARES."""
    return of(position) == SHARES


def is_put(position) -> bool:
    """True only for a position explicitly tagged CASH_SECURED_PUT.

    Callers that compute a share-based metric MUST check this and render
    not-applicable rather than letting a zero share count answer for them."""
    return of(position) == CASH_SECURED_PUT


def is_legacy(position) -> bool:
    """True for a legacy LEAP diagonal (or any un-tagged/absent position)."""
    return of(position) == LEAP_PMCC_LEGACY


def holds_shares(position) -> bool:
    """True when the position's base leg is (or can be) owned shares — i.e. every
    share-based readout is DEFINED for it. False for a put, whose share metrics
    are undefined until assignment."""
    return of(position) in (SHARES, LEAP_PMCC_LEGACY)
