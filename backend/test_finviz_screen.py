"""Finviz screener filter-mapping tests.

The screener crashed in production with "Invalid filter option '5M to 10M'"
because the average-volume mapping emitted Finviz dropdown labels that don't
exist. These tests pin the mapping to the *exact* set of labels Finviz accepts
and assert the minimum-volume semantics (a floor, never a range).
"""
import pytest

from providers import finviz_screen


# The exact average-volume options Finviz accepts. Kept in sync with
# finvizfinance.constants.filter_dict["Average Volume"] when that package is
# installed (asserted below), but hard-coded here so the test is meaningful
# even when the optional dependency is absent.
VALID_FINVIZ_AVG_VOL_OPTIONS = {
    "Any",
    "Under 50K", "Under 100K", "Under 500K", "Under 750K", "Under 1M",
    "Over 50K", "Over 100K", "Over 200K", "Over 300K", "Over 400K",
    "Over 500K", "Over 750K", "Over 1M", "Over 2M",
    "100K to 500K", "100K to 1M", "500K to 1M", "500K to 10M",
}


@pytest.mark.parametrize("vol_min_shares", [
    0, 1, 49_999, 50_000, 99_999, 100_000, 250_000, 500_000, 750_000,
    1_000_000, 1_999_999, 2_000_000, 5_000_000,
    10_000_000,  # the production default that triggered the crash
    50_000_000, 1e12,
])
def test_vol_filter_always_returns_a_valid_finviz_option(vol_min_shares):
    assert finviz_screen.vol_filter(vol_min_shares) in VALID_FINVIZ_AVG_VOL_OPTIONS


def test_vol_filter_is_a_floor_not_a_range():
    # A minimum filter must map to an "Over X" (or "Any"), never an "A to B"
    # range that would exclude the most-liquid names.
    for shares in (100_000, 1_000_000, 10_000_000):
        label = finviz_screen.vol_filter(shares)
        assert label == "Any" or label.startswith("Over "), label


def test_vol_filter_picks_highest_floor_not_exceeding_request():
    assert finviz_screen.vol_filter(10_000_000) == "Over 2M"   # capped at Finviz max
    assert finviz_screen.vol_filter(2_000_000) == "Over 2M"
    assert finviz_screen.vol_filter(1_500_000) == "Over 1M"
    assert finviz_screen.vol_filter(1_000_000) == "Over 1M"
    assert finviz_screen.vol_filter(750_000) == "Over 750K"
    assert finviz_screen.vol_filter(600_000) == "Over 500K"
    assert finviz_screen.vol_filter(50_000) == "Over 50K"


def test_vol_filter_below_smallest_floor_applies_no_filter():
    assert finviz_screen.vol_filter(49_999) == "Any"
    assert finviz_screen.vol_filter(0) == "Any"


def test_mapping_matches_installed_finvizfinance_options_if_present():
    finvizfinance = pytest.importorskip("finvizfinance.constants")
    real_options = set(finvizfinance.filter_dict["Average Volume"]["option"].keys())
    # Our hard-coded reference set must not drift from the real package.
    assert VALID_FINVIZ_AVG_VOL_OPTIONS == real_options
    # And every label our mapping can emit must be accepted by Finviz.
    emitted = {finviz_screen.vol_filter(s) for s in
               (0, 50_000, 100_000, 750_000, 1_000_000, 2_000_000, 99_000_000)}
    assert emitted <= real_options
