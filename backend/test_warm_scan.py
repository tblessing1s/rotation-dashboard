"""Pre-open scan warm-up: priming the full-universe scan cache so the operator's
first Scan of the day loads warm instead of triggering a cold provider fetch +
indicator sweep on the request path."""
import pytest

import data_handler
import screening


@pytest.fixture(autouse=True)
def _clean_scan_cache():
    """warm_scan_cache() memoizes full-universe sweeps in screening._results; clear
    them around every test so this file can't pollute other tests' scan reads
    regardless of collection order."""
    screening.clear_cache()
    yield
    screening.clear_cache()


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


def test_warm_scan_guard_skips_when_disabled(monkeypatch):
    import alert_scheduler
    monkeypatch.setenv("CFM_WARM_SCAN", "0")
    called = {"n": 0}
    monkeypatch.setattr(screening, "warm_scan_cache",
                        lambda: called.__setitem__("n", called["n"] + 1) or {"ok": True})
    alert_scheduler._warm_scan()
    assert called["n"] == 0  # the guard short-circuits before touching screening
