"""Pre-open scan warm-up: priming the full-universe scan cache so the operator's
first Scan of the day loads warm instead of triggering a cold provider fetch +
indicator sweep on the request path. Plus the detached background scan runner
(keeps running when the client tab is backgrounded/closed)."""
import threading
import time

import pytest

import data_handler
import screening


@pytest.fixture(autouse=True)
def _clean_scan_cache():
    """warm_scan_cache() memoizes full-universe sweeps in screening._results; clear
    them around every test so this file can't pollute other tests' scan reads
    regardless of collection order. Also reset the background-scan state."""
    screening.clear_cache()
    screening._scan_thread = None
    screening._scan_state.update(status="idle", started_at=None, finished_at=None, error=None)
    yield
    screening.clear_cache()
    screening._scan_thread = None
    screening._scan_state.update(status="idle", started_at=None, finished_at=None, error=None)


def _await_scan_done(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not screening.scan_status()["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("background scan did not finish in time")


def _fake_data(monkeypatch):
    """Point every data read at one synthetic frame so warm_scan_cache exercises
    the real sweeps (regime/sectors/stock-filter/scorecard) without providers."""
    n = 260
    df = _frame([100 + i * 0.1 for i in range(n)])
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "get_many", lambda syms, force=False: {s.upper(): df for s in syms})
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 120.0, "source": "test"})


def _frame(closes):
    import pandas as pd
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes,
        "Volume": [1_000_000] * len(closes),
    })


def test_warm_scan_cache_reports_ok_and_primes_the_memo(monkeypatch):
    _fake_data(monkeypatch)
    screening.clear_cache()

    result = screening.warm_scan_cache()
    assert result["ok"] is True

    # After warming, the memoized sweeps are present so the first real request is
    # a cache hit rather than a fresh full-universe computation.
    assert "stock_filter:ALL" in screening._results
    assert "scorecard:full" in screening._results
    assert "regime" in screening._results
    assert "sectors" in screening._results


def test_warm_scan_cache_swallows_failures(monkeypatch):
    # A warm-up must never raise — it runs off the scheduler tick, and a blow-up
    # there would take down alerting. A provider explosion returns ok=False.
    screening.clear_cache()
    monkeypatch.setattr(data_handler, "prefetch",
                        lambda syms, force=False: (_ for _ in ()).throw(RuntimeError("boom")))
    result = screening.warm_scan_cache()
    assert result["ok"] is False
    assert "boom" in result["error"]


def test_warm_scan_toggle_env(monkeypatch):
    import alert_scheduler
    monkeypatch.setenv("CFM_WARM_SCAN", "0")
    assert alert_scheduler.warm_scan_enabled() is False
    monkeypatch.setenv("CFM_WARM_SCAN", "1")
    assert alert_scheduler.warm_scan_enabled() is True
    monkeypatch.delenv("CFM_WARM_SCAN", raising=False)
    assert alert_scheduler.warm_scan_enabled() is True  # on by default


def test_background_scan_lifecycle_and_dedupe(monkeypatch):
    # A gated fake keeps the "scan" running until we release it, so we can observe
    # the running state and prove a concurrent start is deduped (one job at a time).
    gate = threading.Event()
    monkeypatch.setattr(screening, "warm_scan_cache",
                        lambda: (gate.wait(timeout=5), {"ok": True})[1])

    first = screening.start_background_scan()
    assert first["status"] == "running" and first["running"] is True
    running_thread = screening._scan_thread

    # Concurrent start while one is in flight: same thread, no second job.
    again = screening.start_background_scan()
    assert again["running"] is True
    assert screening._scan_thread is running_thread

    gate.set()
    _await_scan_done()
    assert screening.scan_status()["status"] == "done"


def test_background_scan_records_error(monkeypatch):
    monkeypatch.setattr(screening, "warm_scan_cache",
                        lambda: {"ok": False, "error": "provider down"})
    screening.start_background_scan()
    _await_scan_done()
    st = screening.scan_status()
    assert st["status"] == "error"
    assert st["error"] == "provider down"
    assert st["running"] is False


def test_scan_status_reports_freshness(monkeypatch):
    # fresh is driven by the memoized scorecard sweep being warm.
    screening.clear_cache()
    assert screening.scan_status()["fresh"] is False
    screening._results["scorecard:full"] = (time.time(), {"results": []})
    assert screening.scan_status()["fresh"] is True


def test_warm_scan_guard_skips_when_disabled(monkeypatch):
    import alert_scheduler
    monkeypatch.setenv("CFM_WARM_SCAN", "0")
    called = {"n": 0}
    monkeypatch.setattr(screening, "warm_scan_cache",
                        lambda: called.__setitem__("n", called["n"] + 1) or {"ok": True})
    alert_scheduler._warm_scan()
    assert called["n"] == 0  # the guard short-circuits before touching screening
