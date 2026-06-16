"""Finviz screener filter-mapping tests.

The screener crashed in production with "Invalid filter option '5M to 10M'"
because the average-volume mapping emitted Finviz dropdown labels that don't
exist. These tests pin the mapping to the *exact* set of labels Finviz accepts
and assert the minimum-volume semantics (a floor, never a range).
"""
import pandas as pd
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


# ---------------------------------------------------------------------------
# run() — merge of the Technical (ATR) and Custom (avg/rel volume) views.
# ---------------------------------------------------------------------------
class _FakeView:
    def __init__(self, df=None, raises=None):
        self._df, self._raises = df, raises

    def set_filter(self, *a, **k):
        pass

    def screener_view(self, *a, **k):
        if self._raises:
            raise self._raises
        return self._df


_TECH_DF = pd.DataFrame([
    # "Change" is a fraction as Finviz reports it (0.0234 == +2.34%).
    {"Ticker": "AAA", "ATR": 3.0, "Price": 50.0, "Change": 0.0234},   # atrPct 6.0, in band
    {"Ticker": "BBB", "ATR": 3.5, "Price": 50.0, "Change": -0.01},    # atrPct 7.0, in band
    {"Ticker": "CCC", "ATR": 1.5, "Price": 30.0, "Change": 0.05},     # atrPct 5.0, in band
    {"Ticker": "DDD", "ATR": 10.0, "Price": 200.0, "Change": 0.0},    # price out of $20-100 band
    {"Ticker": "EEE", "ATR": 0.5, "Price": 50.0, "Change": 0.0},      # atrPct 1.0, below 4
])

_CUSTOM_DF = pd.DataFrame([
    {"Ticker": "AAA", "Sector": "Tech", "Avg Volume": 15_000_000.0, "Rel Volume": 1.5, "Price": 50.0},
    {"Ticker": "BBB", "Sector": "Energy", "Avg Volume": 3_000_000.0, "Rel Volume": 2.0, "Price": 50.0},
    {"Ticker": "CCC", "Sector": "Health", "Avg Volume": 12_000_000.0, "Rel Volume": 1.1, "Price": 30.0},
    {"Ticker": "EEE", "Sector": "Tech", "Avg Volume": 20_000_000.0, "Rel Volume": 0.9, "Price": 50.0},
])


def _patch_views(monkeypatch, tech, custom):
    import finvizfinance.screener.technical as t
    import finvizfinance.screener.custom as c
    monkeypatch.setattr(t, "Technical", lambda: tech)
    monkeypatch.setattr(c, "Custom", lambda: custom)


def test_run_enforces_exact_volume_floor_and_attaches_rvol(monkeypatch):
    _patch_views(monkeypatch, _FakeView(_TECH_DF), _FakeView(_CUSTOM_DF))
    out = finviz_screen.run(price_min=20, price_max=100, vol_min_shares=10_000_000,
                            atr_min=4, atr_max=9)

    assert out["volPrecise"] is True
    # BBB (3M avg vol) is below the 10M floor and must be dropped even though it
    # has the highest ATR%; DDD/EEE fail price/ATR%. Sorted by ATR% desc.
    assert [r["symbol"] for r in out["results"]] == ["AAA", "CCC"]
    aaa = out["results"][0]
    assert aaa["avgVol"] == 15_000_000 and aaa["rvol"] == 1.5
    assert aaa["sector"] == "Tech"
    assert aaa["changePct"] == 2.34  # Finviz fraction 0.0234 -> percent


def test_run_falls_back_to_technical_only_when_enrichment_fails(monkeypatch):
    _patch_views(monkeypatch, _FakeView(_TECH_DF),
                 _FakeView(raises=RuntimeError("Finviz 403")))
    out = finviz_screen.run(price_min=20, price_max=100, vol_min_shares=10_000_000,
                            atr_min=4, atr_max=9)

    assert out["volPrecise"] is False
    # No exact floor applied: all price/ATR%-passing names survive, ATR% desc.
    assert [r["symbol"] for r in out["results"]] == ["BBB", "AAA", "CCC"]
    assert all(r["avgVol"] is None and r["rvol"] is None for r in out["results"])


def test_run_tolerates_alternate_custom_header_names(monkeypatch):
    # finvizfinance has shipped both "Avg Volume" and "Average Volume" headers.
    alt = _CUSTOM_DF.rename(columns={
        "Avg Volume": "Average Volume", "Rel Volume": "Relative Volume"})
    _patch_views(monkeypatch, _FakeView(_TECH_DF), _FakeView(alt))
    out = finviz_screen.run(price_min=20, price_max=100, vol_min_shares=10_000_000,
                            atr_min=4, atr_max=9)
    assert out["volPrecise"] is True
    assert [r["symbol"] for r in out["results"]] == ["AAA", "CCC"]
    assert out["results"][0]["rvol"] == 1.5


def test_run_degrades_when_custom_view_lacks_volume_column(monkeypatch):
    no_vol = _CUSTOM_DF.drop(columns=["Avg Volume"])
    _patch_views(monkeypatch, _FakeView(_TECH_DF), _FakeView(no_vol))
    out = finviz_screen.run(price_min=20, price_max=100, vol_min_shares=10_000_000,
                            atr_min=4, atr_max=9)
    # No usable avg-volume column => treat as enrichment-unavailable, keep all.
    assert out["volPrecise"] is False
    assert [r["symbol"] for r in out["results"]] == ["BBB", "AAA", "CCC"]


def test_run_respects_limit(monkeypatch):
    _patch_views(monkeypatch, _FakeView(_TECH_DF), _FakeView(_CUSTOM_DF))
    out = finviz_screen.run(price_min=20, price_max=100, vol_min_shares=10_000_000,
                            atr_min=4, atr_max=9, limit=1)
    assert [r["symbol"] for r in out["results"]] == ["AAA"]


def test_mapping_matches_installed_finvizfinance_options_if_present():
    finvizfinance = pytest.importorskip("finvizfinance.constants")
    real_options = set(finvizfinance.filter_dict["Average Volume"]["option"].keys())
    # Our hard-coded reference set must not drift from the real package.
    assert VALID_FINVIZ_AVG_VOL_OPTIONS == real_options
    # And every label our mapping can emit must be accepted by Finviz.
    emitted = {finviz_screen.vol_filter(s) for s in
               (0, 50_000, 100_000, 750_000, 1_000_000, 2_000_000, 99_000_000)}
    assert emitted <= real_options
