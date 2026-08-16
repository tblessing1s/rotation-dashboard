"""Canonical per-contract <-> per-share unit conversion (backend/units.py).

Pins the convention so the ×100 / ÷100 factor can't drift, and guards the
round-trip the executor boundary sites rely on.
"""
import pytest

import units


def test_shares_per_contract_is_100():
    assert units.SHARES_PER_CONTRACT == 100


@pytest.mark.parametrize("per_share", [0.0, 1.0, 2.55, 12.34, 187.5, 0.01])
def test_per_contract_round_trip(per_share):
    assert units.leap_per_share(units.leap_per_contract(per_share)) == pytest.approx(per_share)


def test_leap_per_contract_scales_by_hundred():
    assert units.leap_per_contract(2.55) == pytest.approx(255.0)
    assert units.leap_per_share(255.0) == pytest.approx(2.55)


def test_leap_per_contract_is_unrounded():
    # Pure conversion — storage sites round explicitly, so the helper itself must
    # not silently drop precision (a third decimal survives).
    assert units.leap_per_contract(1.234) == pytest.approx(123.4)


def test_storage_boundary_matches_legacy_expression():
    # The executor stored `round(price * 100, 2)`; routing through the helper must
    # produce the identical number.
    price = 12.345
    assert round(units.leap_per_contract(price), 2) == round(price * 100, 2)


@pytest.mark.parametrize("total,contracts,expected", [
    (255.0, 1, 2.55),      # one contract: /100
    (510.0, 2, 2.55),      # two contracts: /(100*2)
    (0.0, 3, 0.0),
])
def test_leap_extrinsic_per_share(total, contracts, expected):
    assert units.leap_extrinsic_per_share(total, contracts) == pytest.approx(expected)


def test_leap_extrinsic_per_share_defaults_contracts_to_one():
    assert units.leap_extrinsic_per_share(255.0, 0) == pytest.approx(2.55)
    assert units.leap_extrinsic_per_share(255.0, None) == pytest.approx(2.55)
