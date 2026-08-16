"""Canonical per-contract <-> per-share unit conversion for option legs.

This module is the ONE named home for the CFM units convention, so the ×100 /
÷100 factor can't drift across the ~200 arithmetic sites that touch it.

The convention (see CLAUDE.md "Units"):

  * A LEAP leg stores dollars **per contract** — ``execution_price``,
    ``close_price``, ``cost_basis``/``current_bid`` (per-contract total),
    ``extrinsic_captured`` (per-contract total).
  * A short leg stores dollars **per share** — ``premium_per_share``,
    ``close_price_per_share``, ``entry_extrinsic_per_share``.
  * Display and order limits are per-share. One contract = 100 shares.

These helpers are pure unit conversions and do NOT round: a storage boundary
that wants cents rounds explicitly at the call site (``round(leap_per_contract(x),
2)``), exactly as the hand-written code did, so routing a site through here is a
de-duplication, not a behavior change. The frontend mirror is
``frontend/src/units.js``.
"""
from __future__ import annotations

SHARES_PER_CONTRACT = 100


def leap_per_contract(per_share) -> float:
    """Per-share LEAP price/mark -> per-contract dollars (× shares/contract)."""
    return float(per_share) * SHARES_PER_CONTRACT


def leap_per_share(per_contract) -> float:
    """Per-contract LEAP dollars -> per-share (÷ shares/contract), for display."""
    return float(per_contract) / SHARES_PER_CONTRACT


def leap_extrinsic_per_share(per_contract_total, contracts) -> float:
    """Per-contract *total* extrinsic (across ``contracts``) -> per-share.

    ``extrinsic_captured`` is stored as the whole position's dollar extrinsic, so
    the per-share figure divides by both shares/contract and the contract count.
    ``contracts`` falls back to 1 when missing/zero (a single-contract leg).
    """
    c = int(contracts or 1) or 1
    return float(per_contract_total) / (SHARES_PER_CONTRACT * c)
