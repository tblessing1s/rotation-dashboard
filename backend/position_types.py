"""Position-type discriminator for the shares-primary migration (schema v20).

Every position carries a ``position_type``:

- ``LEAP_PMCC_LEGACY`` — the legacy diagonal (deep-ITM LEAP long + short call).
  READ ONLY: existing history renders and prices from the immutable log, but no
  new LEAP may be opened, rolled, or recommended.
- ``SHARES`` — the active base leg is real shares (delta == 1.0, zero extrinsic,
  no burn, no DTE). The short call covers the owned shares instead of a LEAP.

Absence (a position with no discriminator — e.g. a half-built skeleton, or any
record that predates the migration and somehow wasn't backfilled) degrades to
LEGACY so the pre-migration behavior is preserved everywhere. The SHARES path is
opt-in by an explicit tag, never by omission — this is the load-bearing rule that
keeps burn/payback/coverage removal from ever leaking into a legacy position.
"""
from __future__ import annotations

LEAP_PMCC_LEGACY = "LEAP_PMCC_LEGACY"
SHARES = "SHARES"

ALL = frozenset({LEAP_PMCC_LEGACY, SHARES})


def of(position) -> str:
    """The canonical position_type for a position dict. Anything that is not an
    explicit ``SHARES`` tag resolves to ``LEAP_PMCC_LEGACY`` (the safe default)."""
    if not isinstance(position, dict):
        return LEAP_PMCC_LEGACY
    return SHARES if position.get("position_type") == SHARES else LEAP_PMCC_LEGACY


def is_shares(position) -> bool:
    """True only for a position explicitly tagged SHARES."""
    return of(position) == SHARES


def is_legacy(position) -> bool:
    """True for a legacy LEAP diagonal (or any un-tagged/absent position)."""
    return of(position) == LEAP_PMCC_LEGACY
