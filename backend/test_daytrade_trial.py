"""Paper-trading trial tracker (daytrade/trial.py). Offline: a tmp_path
store with hand-written trade-log files, no signal-engine replay needed."""
from __future__ import annotations

import pytest

import config
from daytrade import store, trial


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    return tmp_path


def _trade(trade_id, status="closed", realized_r=1.0, realized_pnl=25.0, closed_at="x"):
    return {"trade_id": trade_id, "status": status, "realized_r": realized_r,
            "realized_pnl": realized_pnl, "closed_at": closed_at}


def test_no_trades_yet_is_running_with_nothing_counted(tmp_store):
    status = trial.trial_status("primary")
    assert status == {"target_trades": config.DAYTRADE_TRIAL_TRADES, "completed_trades": 0,
                      "status": "running", "verdict": None, "net_r": 0, "net_pnl": 0,
                      "win_rate": None}


def test_open_trades_are_excluded_from_the_count(tmp_store):
    store.save_trades("2026-09-14", "primary", {"t1": _trade("t1", status="open", closed_at=None)})

    status = trial.trial_status("primary")

    assert status["completed_trades"] == 0
    assert status["status"] == "running"


def test_aggregates_closed_trades_across_multiple_days(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 3)
    store.save_trades("2026-09-14", "primary", {
        "t1": _trade("t1", realized_r=1.5, realized_pnl=30.0, closed_at="2026-09-14T10:00:00"),
        "t2": _trade("t2", realized_r=-1.0, realized_pnl=-20.0, closed_at="2026-09-14T10:30:00"),
    })
    store.save_trades("2026-09-15", "primary", {
        "t3": _trade("t3", realized_r=1.0, realized_pnl=15.0, closed_at="2026-09-15T09:45:00"),
    })

    status = trial.trial_status("primary")

    assert status["completed_trades"] == 3
    assert status["status"] == "complete"
    assert status["net_r"] == pytest.approx(1.5)
    assert status["net_pnl"] == pytest.approx(25.0)
    assert status["win_rate"] == pytest.approx(200 / 3, rel=1e-3)  # 2 of 3 winners


def test_caps_at_the_target_and_ignores_trades_past_it(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 2)
    store.save_trades("2026-09-14", "primary", {
        "t1": _trade("t1", realized_pnl=10.0, closed_at="2026-09-14T10:00:00"),
        "t2": _trade("t2", realized_pnl=10.0, closed_at="2026-09-14T10:30:00"),
        "t3": _trade("t3", realized_pnl=1000.0, closed_at="2026-09-14T11:00:00"),  # past the cap
    })

    status = trial.trial_status("primary")

    assert status["completed_trades"] == 2
    assert status["net_pnl"] == pytest.approx(20.0)  # t3 not counted


def test_verdict_is_win_loss_or_flat_only_once_complete(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 1)

    store.save_trades("2026-09-14", "primary", {"t1": _trade("t1", realized_pnl=50.0, closed_at="a")})
    assert trial.trial_status("primary")["verdict"] == "win"

    store.save_trades("2026-09-15", "primary", {"t2": _trade("t2", realized_pnl=-50.0, closed_at="b")})
    # Still capped at 1 (the target) — the FIRST trade decides the verdict.
    assert trial.trial_status("primary")["verdict"] == "win"


def test_a_losing_trial_verdicts_loss(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 1)
    store.save_trades("2026-09-14", "primary", {"t1": _trade("t1", realized_pnl=-50.0, closed_at="a")})

    assert trial.trial_status("primary")["verdict"] == "loss"


def test_a_breakeven_trial_verdicts_flat(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 1)
    store.save_trades("2026-09-14", "primary", {"t1": _trade("t1", realized_pnl=0.0, closed_at="a")})

    assert trial.trial_status("primary")["verdict"] == "flat"


def test_incomplete_trial_has_no_verdict(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 5)
    store.save_trades("2026-09-14", "primary", {"t1": _trade("t1", realized_pnl=50.0, closed_at="a")})

    status = trial.trial_status("primary")
    assert status["status"] == "running"
    assert status["verdict"] is None


def test_trials_are_isolated_per_account(tmp_store, monkeypatch):
    monkeypatch.setattr(config, "DAYTRADE_TRIAL_TRADES", 1)
    store.save_trades("2026-09-14", "primary", {"t1": _trade("t1", realized_pnl=50.0, closed_at="a")})

    primary_status = trial.trial_status("primary")
    ira_status = trial.trial_status("ira")

    assert primary_status["completed_trades"] == 1
    assert primary_status["verdict"] == "win"
    assert ira_status["completed_trades"] == 0
    assert ira_status["status"] == "running"
