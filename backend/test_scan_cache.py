"""Day-scoped scan cache: the full-universe sweep runs ONCE per trading day,
outside trading hours, and is replayed from disk — instead of being recomputed
every few minutes and lost on every restart. Individually refreshed rows are
written back so they survive a reload; the next daily sweep overwrites them.

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


# ---- The scan-day key -------------------------------------------------------
def test_scan_day_rolls_once_after_the_close():
    et = scan_cache.ET
    # Monday 2026-08-17. Before the close, the last COMPLETED session is Friday.
    assert scan_cache.scan_day(datetime(2026, 8, 17, 9, 45, tzinfo=et)) == "2026-08-14"
    assert scan_cache.scan_day(datetime(2026, 8, 17, 15, 59, tzinfo=et)) == "2026-08-14"
    # Once the close has passed, today's own bars are final — one roll, outside
    # trading hours, and it holds for the rest of the day.
    assert scan_cache.scan_day(datetime(2026, 8, 17, 16, 30, tzinfo=et)) == "2026-08-17"
    assert scan_cache.scan_day(datetime(2026, 8, 17, 23, 59, tzinfo=et)) == "2026-08-17"


def test_scan_day_holds_across_the_weekend():
    et = scan_cache.ET
    # Friday evening through Monday's close all replay Friday's sweep — no
    # identical weekend re-scan.
    for when in (datetime(2026, 8, 14, 17, 0, tzinfo=et),   # Fri after the close
                 datetime(2026, 8, 15, 12, 0, tzinfo=et),   # Sat
                 datetime(2026, 8, 16, 21, 0, tzinfo=et),   # Sun
                 datetime(2026, 8, 17, 9, 30, tzinfo=et)):  # Mon pre-close
        assert scan_cache.scan_day(when) == "2026-08-14", when


def test_scan_day_skips_holidays():
    et = scan_cache.ET
    # Fri 2026-07-03 is the observed Independence Day holiday, so the last
    # completed session before Monday's close is Thursday the 2nd.
    assert scan_cache.scan_day(datetime(2026, 7, 6, 9, 30, tzinfo=et)) == "2026-07-02"


def test_scan_day_is_evaluated_in_market_time_not_utc():
    from zoneinfo import ZoneInfo
    # 00:30 UTC on the 18th is 20:30 ET on the 17th — still the 17th's scan day.
    utc = datetime(2026, 8, 18, 0, 30, tzinfo=ZoneInfo("UTC"))
    assert scan_cache.scan_day(utc) == "2026-08-17"


# ---- Store / load round trip -------------------------------------------------
def test_load_replays_the_stored_sweep_for_the_whole_scan_day(cache_dir):
    swept = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)   # the post-close sweep
    scan_cache.store(NAMES, "green", _result(), now=swept)
    # ...still served through the evening AND the whole of the next session.
    for when in (datetime(2026, 8, 17, 21, 0, tzinfo=scan_cache.ET),
                 datetime(2026, 8, 18, 9, 45, tzinfo=scan_cache.ET),
                 datetime(2026, 8, 18, 15, 59, tzinfo=scan_cache.ET)):
        hit = scan_cache.load(NAMES, "green", now=when)
        assert hit is not None, when
        assert [r["ticker"] for r in hit["results"]] == ["T0", "T1"]
        # Provenance: the UI can tell a replay from a fresh sweep, and when it ran.
        assert hit["cached"] is True and hit["scanned_at"].startswith("2026-08-17T16:30")


def test_load_misses_once_the_next_session_closes(cache_dir):
    scan_cache.store(NAMES, "green", _result(),
                     now=datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET))
    # Past the NEXT close there are newer final bars — a fresh sweep is due.
    assert scan_cache.load(NAMES, "green",
                           now=datetime(2026, 8, 18, 16, 30, tzinfo=scan_cache.ET)) is None


@pytest.mark.parametrize("names,regime", [
    (["AAPL", "MSFT", "NVDA"], "green"),   # universe changed
    (["AAPL", "MSFT"], "red"),             # regime flipped
    (["AAPL"], "green"),                   # a name was removed
])
def test_load_misses_when_an_input_would_make_the_answer_wrong(cache_dir, names, regime):
    now = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)
    scan_cache.store(NAMES, "green", _result(), now=now)
    assert scan_cache.load(names, regime, now=now) is None


def test_clear_drops_the_persisted_sweep(cache_dir):
    now = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)
    scan_cache.store(NAMES, "green", _result(), now=now)
    assert scan_cache.load(NAMES, "green", now=now) is not None
    scan_cache.clear()
    assert scan_cache.load(NAMES, "green", now=now) is None


def test_an_empty_sweep_is_never_pinned_for_the_day(cache_dir):
    # A failed/empty sweep must not be cached, or one bad run blanks the Scan for
    # the rest of the day.
    now = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)
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
def test_full_sweep_computes_once_per_day_then_replays(cache_dir, monkeypatch):
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
                        lambda: {"warm": True, "scan_day": "2026-08-17",
                                 "scanned_at": "2026-08-17T16:31:00-04:00"})
    st = screening.scan_status()
    # fresh must consider the DAY cache, not just the 5-minute memo — the Scan tab
    # auto-forces a rescan whenever this reads false.
    assert st["fresh"] is True
    assert st["scanned_at"] == "2026-08-17T16:31:00-04:00"
    assert st["scan_day"] == "2026-08-17"


# ---- Per-stock refreshes are independent and persist -------------------------
def _stored(cache_dir, now):
    scan_cache.store(NAMES, "green",
                     {"as_of": "2026-08-17T20:30:00Z",
                      "results": [{"ticker": "T0", "verdict": "WATCH", "price": 10},
                                  {"ticker": "T1", "verdict": "WATCH", "price": 20}]},
                     now=now)


def test_a_refreshed_row_is_written_back_and_leaves_the_others_alone(cache_dir):
    now = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)
    _stored(cache_dir, now)
    n = scan_cache.patch_rows(NAMES, "green",
                              [{"ticker": "T1", "verdict": "READY", "price": 22,
                                "price_source": "schwab"}], now=now)
    assert n == 1
    rows = {r["ticker"]: r for r in scan_cache.load(NAMES, "green", now=now)["results"]}
    # The refreshed stock now carries its live numbers, and its own stamp so the
    # UI can show it as fresher than the sweep around it.
    assert rows["T1"]["verdict"] == "READY" and rows["T1"]["price"] == 22
    assert rows["T1"]["price_source"] == "schwab"
    assert rows["T1"]["refreshed_at"].startswith("2026-08-17T16:30")
    # Every other stock is exactly as the daily sweep left it — rows are independent.
    assert rows["T0"] == {"ticker": "T0", "verdict": "WATCH", "price": 10}


def test_the_daily_sweep_overwrites_refreshed_rows(cache_dir):
    day1 = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)
    _stored(cache_dir, day1)
    scan_cache.patch_rows(NAMES, "green",
                          [{"ticker": "T1", "verdict": "READY", "price": 22}], now=day1)
    assert scan_cache.load(NAMES, "green", now=day1)["results"][1]["verdict"] == "READY"

    # The next day's full sweep re-scans EVERY name, refreshed ones included.
    day2 = datetime(2026, 8, 18, 16, 30, tzinfo=scan_cache.ET)
    _stored(cache_dir, day2)
    rows = {r["ticker"]: r for r in scan_cache.load(NAMES, "green", now=day2)["results"]}
    assert rows["T1"]["verdict"] == "WATCH" and rows["T1"]["price"] == 20
    assert "refreshed_at" not in rows["T1"]


def test_patch_is_a_no_op_when_there_is_no_sweep_to_patch(cache_dir):
    now = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)
    assert scan_cache.patch_rows(NAMES, "green", [{"ticker": "T1"}], now=now) == 0
    # A stale sweep (yesterday's) must not absorb today's refreshed row either.
    _stored(cache_dir, datetime(2026, 8, 14, 16, 30, tzinfo=scan_cache.ET))
    assert scan_cache.patch_rows(NAMES, "green", [{"ticker": "T1"}], now=now) == 0


def test_patch_never_grows_the_cached_universe(cache_dir):
    # A sector refresh also pulls the sector ETF, which is not a scan row. Adding
    # it would break the payload's agreement with the fingerprint it was stored
    # under, so unknown tickers are ignored.
    now = datetime(2026, 8, 17, 16, 30, tzinfo=scan_cache.ET)
    _stored(cache_dir, now)
    n = scan_cache.patch_rows(NAMES, "green", [{"ticker": "XLK", "verdict": "READY"}], now=now)
    assert n == 0
    tickers = [r["ticker"] for r in scan_cache.load(NAMES, "green", now=now)["results"]]
    assert tickers == ["T0", "T1"]


def test_refresh_tickers_persists_into_the_days_sweep(cache_dir, monkeypatch):
    import refresh_policy
    # Stored and patched under the REAL clock: refresh_tickers patches whatever
    # the CURRENT scan day is, and a patch aimed at any other day is a no-op (see
    # test_patch_is_a_no_op_when_there_is_no_sweep_to_patch).
    _stored(cache_dir, None)
    monkeypatch.setattr(scorecard_metrics, "_current_regime_color", lambda: "green")
    monkeypatch.setattr(scorecard_metrics.sector_data, "all_tickers", lambda: list(NAMES))
    monkeypatch.setattr(refresh_policy.data_handler, "prefetch", lambda syms, force=False: None)
    monkeypatch.setattr(refresh_policy.data_handler, "live_prices",
                        lambda names: {"T1": {"price": 22.0, "source": "schwab"}})
    monkeypatch.setattr(scorecard_metrics, "scorecard",
                        lambda names, price_overrides=None, force=False: {
                            "as_of": "now",
                            "results": [{"ticker": "T1", "verdict": "READY", "price": 22.0}]})

    out = refresh_policy.refresh_tickers(["T1"])
    assert out["cached_rows_updated"] == 1
    rows = {r["ticker"]: r for r in scan_cache.load(NAMES, "green")["results"]}
    assert rows["T1"]["verdict"] == "READY"          # persisted, survives a reload
    assert rows["T1"]["price_source"] == "schwab"
    assert rows["T0"]["verdict"] == "WATCH"          # untouched
