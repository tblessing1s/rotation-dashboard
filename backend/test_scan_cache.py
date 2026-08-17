"""Day-scoped scan cache: the full-universe sweep runs once per data epoch and is
replayed from disk, instead of being recomputed every few minutes and lost on
every restart.

Run: python -m pytest backend/test_scan_cache.py -q
"""
import os
import tempfile
from datetime import datetime

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import config  # noqa: E402
import scan_cache  # noqa: E402
import screening  # noqa: E402
from metrics import scorecard as scorecard_metrics  # noqa: E402


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Point the cache at a scratch dir and drop both cache layers around the test."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(config, "active_cache_dir", lambda: str(tmp_path / "cache"))
    scan_cache.clear()
    screening.clear_cache()
    yield tmp_path
    scan_cache.clear()
    screening.clear_cache()


def _result(n=2):
    return {"as_of": "2026-08-17T12:00:00Z",
            "results": [{"ticker": f"T{i}", "verdict": "WATCH"} for i in range(n)]}


NAMES = ["AAPL", "MSFT"]


# ---- The epoch key -----------------------------------------------------------
def test_epoch_rolls_once_after_the_close():
    et = scan_cache.ET
    pre = datetime(2026, 8, 17, 9, 45, tzinfo=et)
    late = datetime(2026, 8, 17, 15, 59, tzinfo=et)
    post = datetime(2026, 8, 17, 16, 30, tzinfo=et)
    # Everything before the roll is ONE epoch — the morning and the afternoon
    # share a sweep, which is the whole point.
    assert scan_cache.epoch(pre) == scan_cache.epoch(late)
    # ...and the evening is a new one, because the session's bar has landed.
    assert scan_cache.epoch(post) != scan_cache.epoch(pre)
    # A new day is always a new epoch.
    assert scan_cache.epoch(datetime(2026, 8, 18, 9, 45, tzinfo=et)) != scan_cache.epoch(pre)


def test_epoch_is_evaluated_in_market_time_not_utc():
    from zoneinfo import ZoneInfo
    # 00:30 UTC on the 18th is 20:30 ET on the 17th — still the 17th's post epoch.
    utc = datetime(2026, 8, 18, 0, 30, tzinfo=ZoneInfo("UTC"))
    assert scan_cache.epoch(utc) == "2026-08-17/post"


# ---- Store / load round trip -------------------------------------------------
def test_load_replays_the_stored_sweep_within_the_epoch(cache_dir):
    now = datetime(2026, 8, 17, 10, 0, tzinfo=scan_cache.ET)
    scan_cache.store(NAMES, "green", _result(), now=now)
    hit = scan_cache.load(NAMES, "green", now=datetime(2026, 8, 17, 15, 0, tzinfo=scan_cache.ET))
    assert hit is not None
    assert [r["ticker"] for r in hit["results"]] == ["T0", "T1"]
    # Provenance: the UI can tell a replay from a fresh sweep, and when it ran.
    assert hit["cached"] is True and hit["scanned_at"].startswith("2026-08-17T10:00")


def test_load_misses_in_a_later_epoch(cache_dir):
    scan_cache.store(NAMES, "green", _result(),
                     now=datetime(2026, 8, 17, 10, 0, tzinfo=scan_cache.ET))
    assert scan_cache.load(NAMES, "green",
                           now=datetime(2026, 8, 17, 17, 0, tzinfo=scan_cache.ET)) is None
    assert scan_cache.load(NAMES, "green",
                           now=datetime(2026, 8, 18, 10, 0, tzinfo=scan_cache.ET)) is None


@pytest.mark.parametrize("names,regime", [
    (["AAPL", "MSFT", "NVDA"], "green"),   # universe changed
    (["AAPL", "MSFT"], "red"),             # regime flipped
    (["AAPL"], "green"),                   # a name was removed
])
def test_load_misses_when_an_input_would_make_the_answer_wrong(cache_dir, names, regime):
    now = datetime(2026, 8, 17, 10, 0, tzinfo=scan_cache.ET)
    scan_cache.store(NAMES, "green", _result(), now=now)
    assert scan_cache.load(names, regime, now=now) is None


def test_clear_drops_the_persisted_sweep(cache_dir):
    now = datetime(2026, 8, 17, 10, 0, tzinfo=scan_cache.ET)
    scan_cache.store(NAMES, "green", _result(), now=now)
    assert scan_cache.load(NAMES, "green", now=now) is not None
    scan_cache.clear()
    assert scan_cache.load(NAMES, "green", now=now) is None


def test_an_empty_sweep_is_never_pinned_for_an_epoch(cache_dir):
    # A failed/empty sweep must not be cached, or one bad run blanks the Scan for
    # the rest of the epoch.
    now = datetime(2026, 8, 17, 10, 0, tzinfo=scan_cache.ET)
    scan_cache.store(NAMES, "green", {"as_of": "x", "results": []}, now=now)
    assert scan_cache.load(NAMES, "green", now=now) is None


def test_corrupt_cache_is_a_miss_not_a_crash(cache_dir):
    os.makedirs(config.active_cache_dir(), exist_ok=True)
    with open(os.path.join(config.active_cache_dir(), "scan_scorecard.json"), "w") as fh:
        fh.write("{not json")
    assert scan_cache.load(NAMES, "green") is None


def test_store_survives_an_unwritable_cache_dir(cache_dir, monkeypatch):
    # A full/read-only volume degrades to "no caching", never a failed scan.
    monkeypatch.setattr(config, "active_cache_dir", lambda: "/proc/nonexistent/nope")
    scan_cache.store(NAMES, "green", _result())  # must not raise
    assert scan_cache.load(NAMES, "green") is None


# ---- The sweep actually uses it ---------------------------------------------
def test_full_sweep_computes_once_per_epoch_then_replays(cache_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(scorecard_metrics, "_current_regime_color", lambda: "green")
    monkeypatch.setattr(scorecard_metrics.sector_data, "all_tickers", lambda: list(NAMES))
    monkeypatch.setattr(scorecard_metrics, "_compute_scorecard",
                        lambda names, price_overrides=None, regime_color=None:
                            (calls.append(names), _result())[1])

    first = scorecard_metrics.scorecard(None)
    assert len(calls) == 1 and first.get("cached") is False

    # The in-process memo would hide the disk layer; drop ONLY the memo (not
    # clear_cache(), which also wipes the disk) so this proves the DISK cache is
    # what serves the replay — the restarted-machine case.
    screening._results.clear()
    second = scorecard_metrics.scorecard(None)
    assert len(calls) == 1, "the sweep recomputed instead of replaying from disk"
    assert second["cached"] is True
    assert [r["ticker"] for r in second["results"]] == ["T0", "T1"]


def test_force_bypasses_the_day_cache(cache_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(scorecard_metrics, "_current_regime_color", lambda: "green")
    monkeypatch.setattr(scorecard_metrics.sector_data, "all_tickers", lambda: list(NAMES))
    monkeypatch.setattr(scorecard_metrics, "_compute_scorecard",
                        lambda names, price_overrides=None, regime_color=None:
                            (calls.append(names), _result())[1])

    scorecard_metrics.scorecard(None)
    scorecard_metrics.scorecard(None)          # replayed
    assert len(calls) == 1
    forced = scorecard_metrics.scorecard(None, force=True)   # the Rescan button
    assert len(calls) == 2 and forced["cached"] is False
    # A forced sweep must also refresh the memo, or the next read serves back the
    # very copy the force was meant to replace.
    assert screening.peek_cached("scorecard:full") is not None


def test_a_ticker_subset_never_touches_the_day_cache(cache_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(scorecard_metrics, "_compute_scorecard",
                        lambda names, price_overrides=None, regime_color=None:
                            (calls.append(names), _result())[1])
    scorecard_metrics.scorecard(["AAPL"])
    scorecard_metrics.scorecard(["AAPL"])
    # An entry snapshot at trade time must be live every single time.
    assert len(calls) == 2
    assert scan_cache.load(["AAPL"], None) is None


def test_scan_status_reports_when_the_universe_was_swept(cache_dir, monkeypatch):
    monkeypatch.setattr(screening, "_day_cache_status",
                        lambda: {"warm": True, "epoch": "2026-08-17/pre",
                                 "scanned_at": "2026-08-17T09:31:00-04:00"})
    st = screening.scan_status()
    # fresh must consider the DAY cache, not just the 5-minute memo — the Scan tab
    # auto-forces a rescan whenever this reads false.
    assert st["fresh"] is True
    assert st["scanned_at"] == "2026-08-17T09:31:00-04:00"
    assert st["epoch"] == "2026-08-17/pre"
