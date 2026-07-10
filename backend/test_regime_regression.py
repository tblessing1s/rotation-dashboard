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
import indicators as ind  # noqa: E402
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


# ---------------------------------------------------------------------------
# SAR causality (R6): the property the regime backfill relies on.
# ---------------------------------------------------------------------------
def test_sar_is_prefix_causal_equals_full_history():
    """R6: Parabolic SAR is forward-causal — the value computed on history
    TRUNCATED at date D equals the value at D from the full-history run, for EVERY
    D. The regime backfill computes each historical day from a prefix of the
    cached SPY frame (regime_history.backfill: spy.iloc[:i+1]); this is exactly
    the invariant that makes that legitimate. Over >= 1 trading year of bars."""
    df = _load("distribution_rollover")
    assert len(df) >= 252
    full = ind.parabolic_sar(df)
    for i in range(1, len(df)):
        assert ind.parabolic_sar_last(df.iloc[: i + 1]) == full[i], f"SAR diverged at bar {i}"


def test_four_light_regime_prefix_equals_full_history():
    """R6: the WHOLE four-light published regime (SAR + slow/fast MA + momentum +
    the yellow dwell) computed on history truncated at D equals the value at D from
    the full-history replay — for every sampled D. Determinism of the published
    regime history is the requirement; this pins it (the dwell is path-dependent
    but forward-causal, so a prefix reproduces the full run's value at its end)."""
    df = _load("distribution_rollover")
    pub_full, _ = _replay(df)
    warm = config.GENIUS_SLOW_MA - 1
    # Sample across the range (the nested replay is O(n^2); a stride keeps it fast
    # while still covering green/yellow/red and the transitions between them).
    for i in range(warm, len(df), 4):
        pub_trunc, _ = _replay(df.iloc[: i + 1])
        assert pub_trunc[-1] == pub_full[i - warm], f"published regime diverged at bar {i}"


def test_sar_shifted_start_diverges_documents_the_boundary():
    """R6 (boundary): the causality guarantee holds ONLY for prefixes sharing the
    SAME first bar. A window that starts LATER (a different bar 0 — e.g. a rolling
    cache that dropped old bars over time) re-seeds SAR from its own first two bars
    and diverges for the bars right after the shift. This is WHY the backfill must
    always recompute from the EARLIEST cached bar, never a rolling sub-window —
    the determinism above is a property of that slicing discipline, not of SAR
    alone."""
    df = _load("distribution_rollover")
    full = ind.parabolic_sar(df)
    k = 120
    shifted = ind.parabolic_sar(df.iloc[k:])
    # Right after the shifted start the fresh two-bar seed dominates -> a visible
    # (> 1 point) divergence from the full-history SAR at the same absolute bar.
    assert abs(shifted[2] - full[k + 2]) > 0.5
