"""Regression tests over the labeled synthetic parquet fixtures
(fixtures/regime/*.parquet, built by fixtures/regime/build_fixtures.py).

These stand in for known SPY market shapes and are read from disk — the tests
never fetch. Each asserts one whole-sequence regime behaviour:
  * a sustained confirmed uptrend holds GREEN with no flaps,
  * a distribution rollover degrades GREEN -> YELLOW -> RED in order, never
    jumping GREEN <-> RED day-over-day,
  * a boundary whipsaw's 1-day raw-green blip is absorbed by the yellow dwell
    (no yellow -> green -> yellow published round trip).
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-regime-"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import config  # noqa: E402
import regime_genius as rg  # noqa: E402

FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regime")


def _load(name):
    return pd.read_parquet(os.path.join(FIX_DIR, f"{name}.parquet"))


def _replay(df):
    """Published + raw regime for each bar past the slow-MA warm-up, accumulating
    the dwell over the trading-day sequence exactly as the live path does."""
    pub, raw = [], []
    for i in range(len(df)):
        if (i + 1) < config.GENIUS_SLOW_MA:
            continue
        tr = rg.compute_trace(df.iloc[: i + 1], None, None, pub)
        pub.append(tr["published_regime"])
        raw.append(tr["raw_condition"])
    return pub, raw


def _has_island(seq, label):
    return any(seq[j] == label and seq[j - 1] != label and seq[j + 1] != label
               and seq[j - 1] == seq[j + 1] for j in range(1, len(seq) - 1))


def test_sustained_green_holds_without_flaps():
    pub, _ = _replay(_load("sustained_green"))
    assert pub, "fixture produced no post-warmup samples"
    assert set(pub) == {"green"}                     # never leaves green
    transitions = sum(1 for a, b in zip(pub, pub[1:]) if a != b)
    assert transitions == 0                          # and never flaps


def test_distribution_rollover_degrades_in_order():
    pub, _ = _replay(_load("distribution_rollover"))
    # The published regime touches all three states...
    assert {"green", "yellow", "red"} <= set(pub)
    # ...in the order green -> yellow -> red (first occurrences)...
    assert pub.index("green") < pub.index("yellow") < pub.index("red")
    # ...and never jumps directly between green and red day-over-day — every
    # green<->red transition passes through yellow.
    for a, b in zip(pub, pub[1:]):
        assert {a, b} != {"green", "red"}, f"illegal direct {a}->{b} jump"


def test_v_bottom_whipsaw_dwell_suppresses_the_blip():
    df = _load("v_bottom_whipsaw")
    pub, raw = _replay(df)
    # The RAW vote does round-trip: a 1-day green island surrounded by yellow.
    assert _has_island(raw, "green"), "fixture no longer produces a raw green blip"
    # The PUBLISHED regime has NO 1-day island of any kind — the dwell smooths it.
    for label in ("green", "yellow", "red"):
        assert not _has_island(pub, label), f"published shows a 1-day {label} island"
    # Specifically, on every raw green-in-yellow blip the published regime stayed
    # yellow (the dwell held it), so there is no yellow->green->yellow round trip.
    for j in range(1, len(raw) - 1):
        if raw[j] == "green" and raw[j - 1] == "yellow" and raw[j + 1] == "yellow":
            assert pub[j] == "yellow"


@pytest.mark.parametrize("name", ["sustained_green", "distribution_rollover", "v_bottom_whipsaw"])
def test_fixtures_are_well_formed(name):
    df = _load(name)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.is_monotonic_increasing
    assert len(df) > config.GENIUS_SLOW_MA
