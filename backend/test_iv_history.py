"""IV-history + IV-rank tests — daily-point recording (dedup + cap + junk
rejection) and the rank/percentile math, including the min-sample guard. Uses a
temp DATA_DIR so the on-disk store is isolated. Run: python -m pytest backend -q
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cfm-iv-test-")

import pytest  # noqa: E402

import iv_history  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    try:
        os.remove(iv_history.IV_HISTORY_PATH)
    except OSError:
        pass
    yield


def _seed(ticker, values, start_day=1):
    for i, v in enumerate(values):
        iv_history.record(ticker, v, day=f"2026-01-{start_day + i:02d}")


def test_record_one_point_per_day_last_write_wins():
    iv_history.record("NVDA", 40.0, day="2026-01-01")
    iv_history.record("NVDA", 45.0, day="2026-01-01")  # same day overwrites
    iv_history.record("NVDA", 50.0, day="2026-01-02")
    s = iv_history.series("NVDA")
    assert [r["iv"] for r in s] == [45.0, 50.0]


def test_record_rejects_junk():
    assert iv_history.record("NVDA", None) is False
    assert iv_history.record("NVDA", 0) is False        # non-positive
    assert iv_history.record("NVDA", 5000) is False      # absurd
    assert iv_history.record("", 40) is False            # no ticker
    assert iv_history.series("NVDA") == []


def test_series_is_capped_to_a_year():
    _seed("NVDA", [30.0 + i * 0.01 for i in range(300)])
    assert len(iv_history.series("NVDA")) == 260  # _MAX_POINTS, newest kept


def test_rank_needs_minimum_sample():
    _seed("NVDA", [40.0] * 10)  # < _MIN_POINTS
    r = iv_history.iv_rank("NVDA")
    assert r["iv_rank"] is None and r["days"] == 10


def test_rank_and_percentile_math():
    # 20 points spanning 20..58; current 40 sits mid-range.
    _seed("NVDA", [20.0 + 2 * i for i in range(20)])  # 20,22,...,58
    r = iv_history.iv_rank("NVDA", current_iv=40.0)
    assert r["iv_min"] == 20.0 and r["iv_max"] == 58.0
    # rank = (40-20)/(58-20)*100 = 52.6
    assert r["iv_rank"] == pytest.approx(52.6, abs=0.2)
    assert r["iv_now"] == 40.0
    # percentile: 40 already in the series; count of <=40 over 20 pts.
    assert 0 < r["iv_percentile"] <= 100


def test_current_iv_extends_the_series_for_a_fresh_reading():
    _seed("NVDA", [30.0] * 25)          # flat history at 30
    r = iv_history.iv_rank("NVDA", current_iv=60.0)  # a spike today
    assert r["iv_now"] == 60.0 and r["iv_max"] == 60.0
    assert r["iv_rank"] == 100.0        # new high vs its own year
