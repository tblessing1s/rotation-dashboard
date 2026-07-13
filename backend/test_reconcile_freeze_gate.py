"""Reconciliation freeze gate + minutes-based staleness + interval cadence (§5).

Offline. Covers: the global freeze verdict, the market-hours minutes staleness
degrade, recommendation generation blocked while frozen, and the intraday
reconcile+ingest cadence gate.

Run: python -m pytest backend/test_reconcile_freeze_gate.py -q
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import alert_scheduler  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402
import reconcile  # noqa: E402

ET = alert_scheduler.ET


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _now_utc(minus_min=0):
    return datetime.now(timezone.utc) - timedelta(minutes=minus_min)


# ---------------------------------------------------------------------------
# freeze_status
# ---------------------------------------------------------------------------
def test_freeze_status_clean_when_no_review(store):
    state = log.load_state()
    state["positions"].append({"ticker": "ABC", "status": "open"})
    fs = reconcile.freeze_status(state)
    assert fs["frozen"] is False and fs["tickers"] == []


def test_freeze_status_frozen_lists_tickers(store):
    state = log.load_state()
    state["positions"] += [
        {"ticker": "ABC", "status": "open", "needs_review": True,
         "review": {"summary": "qty mismatch"}},
        {"ticker": "XYZ", "status": "open"},
        {"ticker": "OLD", "status": "closed", "needs_review": True},  # closed never freezes
    ]
    fs = reconcile.freeze_status(state)
    assert fs["frozen"] is True
    assert fs["tickers"] == ["ABC"]
    assert "ABC" in fs["reason"]


# ---------------------------------------------------------------------------
# minutes-based staleness (a warning, not a freeze)
# ---------------------------------------------------------------------------
def test_stale_minutes_fresh_vs_stale(store):
    state = log.load_state()
    state["reconciliation"] = {"last_success": None}
    assert reconcile.is_reconcile_stale_minutes(state) is False  # never-run isn't stale

    fresh = _now_utc(minus_min=5).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["reconciliation"] = {"last_success": fresh}
    assert reconcile.is_reconcile_stale_minutes(state) is False

    old = _now_utc(minus_min=config.RECONCILE_STALE_MINUTES + 10).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["reconciliation"] = {"last_success": old}
    assert reconcile.is_reconcile_stale_minutes(state) is True


# ---------------------------------------------------------------------------
# recommendation generation blocked while frozen
# ---------------------------------------------------------------------------
def test_recommendation_run_skips_when_frozen(store, monkeypatch):
    import recommendation_runner as runner

    state = log.load_state()
    state["positions"].append({"ticker": "ABC", "status": "open", "needs_review": True,
                               "review": {"summary": "broker divergence"}})
    log.save_state(state)

    # If the gate fails to short-circuit, evaluate would run — make it explode so
    # the test fails loudly rather than silently passing.
    import recommendation_engine as engine
    monkeypatch.setattr(engine, "evaluate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("engine ran while frozen")))
    monkeypatch.setattr(runner, "release_pending", lambda **k: {"released": 0})

    summary = runner.run(notify=False)
    assert summary["reconcile_frozen"] is True
    assert summary["emitted"] == 0
    assert summary["frozen_tickers"] == ["ABC"]


# ---------------------------------------------------------------------------
# interval reconcile+ingest cadence gate
# ---------------------------------------------------------------------------
def test_interval_reconcile_respects_cadence(store, monkeypatch):
    import schwab_api
    import transaction_ingest

    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    calls = {"recon": 0, "ingest": 0}
    monkeypatch.setattr(reconcile, "run_reconciliation",
                        lambda *a, **k: (calls.__setitem__("recon", calls["recon"] + 1),
                                         {"status": "CLEAN", "diffs": []})[1])
    monkeypatch.setattr(transaction_ingest, "run_ingestion",
                        lambda *a, **k: (calls.__setitem__("ingest", calls["ingest"] + 1),
                                         {"matched": [], "proposals": []})[1])
    monkeypatch.setattr(alert_scheduler, "_last_interval_reconcile", None)

    t0 = datetime(2026, 7, 13, 10, 0, tzinfo=ET)   # Monday, market hours
    alert_scheduler._maybe_interval_reconcile(t0)
    assert calls == {"recon": 1, "ingest": 1}

    # 5 minutes later (< RECONCILE_INTERVAL_MINUTES) -> no second run
    alert_scheduler._maybe_interval_reconcile(t0 + timedelta(minutes=5))
    assert calls == {"recon": 1, "ingest": 1}

    # past the interval -> runs again
    alert_scheduler._maybe_interval_reconcile(
        t0 + timedelta(minutes=config.RECONCILE_INTERVAL_MINUTES + 1))
    assert calls == {"recon": 2, "ingest": 2}


def test_interval_reconcile_skips_outside_window(store, monkeypatch):
    import schwab_api
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    called = {"n": 0}
    monkeypatch.setattr(reconcile, "run_reconciliation",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(alert_scheduler, "_last_interval_reconcile", None)

    weekend = datetime(2026, 7, 11, 10, 0, tzinfo=ET)   # Saturday
    alert_scheduler._maybe_interval_reconcile(weekend)
    after_tail = datetime(2026, 7, 13, 17, 0, tzinfo=ET)  # Monday, past 16:30
    alert_scheduler._maybe_interval_reconcile(after_tail)
    assert called["n"] == 0
