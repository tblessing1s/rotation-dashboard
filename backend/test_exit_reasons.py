"""Coded exit-reason tests — the enum lands on the derived cycle for every exit
path, the advisory evaluators map their trigger state to the right code, the
LEAP roll is coded, legacy cycles become LEGACY_UNRECORDED (never fabricated),
the v11->v12 migration seeds null snapshots, and the calibration loader yields
(entry_context, exit_reason, outcome) tuples skipping legacy-null cycles.

Offline, mocked frames/clocks. Run: python -m pytest backend/test_exit_reasons.py -q
"""
import json
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import calibration       # noqa: E402
import circuit_breaker   # noqa: E402
import config            # noqa: E402
import data_handler      # noqa: E402
import executor          # noqa: E402
import exit_reasons      # noqa: E402
import kill_switch       # noqa: E402
import logging_handler as log  # noqa: E402
import migrations        # noqa: E402
from exit_reasons import ExitReason  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: None)
    return tmp_path


def _run_cycle(exit_reason, exit_note=None, ticker="NVDA"):
    executor.execute({"action": "buy_leap", "ticker": ticker, "strike": 75,
                      "contracts": 5, "execution_price": 2400, "stock_price": 100,
                      "override_reason": "fixture"})
    executor.execute({"action": "close_leap", "ticker": ticker, "strike": 75,
                      "contracts": 5, "close_price": 2600, "stock_price": 100,
                      "exit_reason": exit_reason, "exit_note": exit_note})
    return log.load_state()["cycles"][0]


# ---------------------------------------------------------------------------
# R3 — every coded exit reason an operator can pick lands on the cycle verbatim.
# ---------------------------------------------------------------------------
_AUTO_CODES = sorted(exit_reasons.OPERATOR_SELECTABLE - {ExitReason.OPERATOR_DISCRETION})


@pytest.mark.parametrize("code", _AUTO_CODES)
def test_each_exit_code_lands_on_cycle(store, code):
    c = _run_cycle(code)
    assert c["exit_reason"] == code
    assert c["exit_note"] is None
    assert "exit_metrics" in c  # exit-time counterpart metrics captured


def test_operator_discretion_carries_note_onto_cycle(store):
    c = _run_cycle(ExitReason.OPERATOR_DISCRETION, exit_note="stepping aside")
    assert c["exit_reason"] == ExitReason.OPERATOR_DISCRETION
    assert c["exit_note"] == "stepping aside"


# ---------------------------------------------------------------------------
# Advisory evaluators map their trigger state to a coded reason (set AT the
# point the rule fires — the close then stamps that code).
# ---------------------------------------------------------------------------
def test_kill_switch_code_mapping():
    sector = {"status": "red", "rs3m_vs_spy": 1.0, "rs3m_vs_sector": -0.5}
    spy = {"status": "red", "rs3m_vs_spy": -0.5, "rs3m_vs_sector": 2.0}
    green = {"status": "green", "rs3m_vs_spy": 3.0, "rs3m_vs_sector": 2.0}
    assert kill_switch.exit_reason_code(sector) == ExitReason.KILL_SWITCH_SECTOR
    assert kill_switch.exit_reason_code(spy) == ExitReason.KILL_SWITCH_SPY
    assert kill_switch.exit_reason_code(green) is None


def test_circuit_breaker_code_mapping():
    for cid, code in (("drawdown", ExitReason.CB_DRAWDOWN_15),
                      ("ma_fast", ExitReason.CB_MA50_3CLOSE),
                      ("ma_slow", ExitReason.CB_MA200_CLOSE),
                      ("manual_line", ExitReason.CB_MANUAL_LINE)):
        assert circuit_breaker.exit_reason_code({"tripped_conditions": [cid]}) == code
    assert circuit_breaker.exit_reason_code({"tripped_conditions": []}) is None


def _frame_dropping(entry=100.0, last=80.0, n=60):
    """A frame that ends `last` (a >=15% drop from `entry`)."""
    idx = pd.bdate_range("2024-01-01", periods=n)
    prices = np.linspace(entry, last, n)
    c = pd.Series(prices, index=idx)
    return pd.DataFrame({"Open": c, "High": c + 0.5, "Low": c - 0.5, "Close": c,
                         "Volume": 1e6}, index=idx)


def test_circuit_breaker_drawdown_drives_real_evaluation(monkeypatch):
    # A 20% drop from the stored entry trips the drawdown leg -> CB_DRAWDOWN_15.
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame_dropping(100, 80))
    position = {"ticker": "NVDA", "status": "active",
                "circuit_breaker": {"price": 50, "entry_price": 100}}
    ev = circuit_breaker.evaluate(position)
    assert ev["tripped"] and "drawdown" in ev["tripped_conditions"]
    assert circuit_breaker.exit_reason_code(ev) == ExitReason.CB_DRAWDOWN_15


# ---------------------------------------------------------------------------
# The mechanical LEAP roll is coded (LEAP_ROLL), note-free.
# ---------------------------------------------------------------------------
def test_leap_roll_close_is_coded(store, monkeypatch):
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame_dropping(100, 100))
    executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 75,
                      "contracts": 5, "execution_price": 2400, "stock_price": 100,
                      "override_reason": "fixture"})
    executor.execute({"action": "roll_leap", "ticker": "NVDA", "stock_price": 100,
                      "to_strike": 70, "to_dte": 400, "execution_price": 2600,
                      "close_price": 2500, "override_reason": "roll"})
    close = [e for e in log.load_state()["executions"]
             if e["action"] == "close_leap"][0]
    assert close["exit_reason"] == ExitReason.LEAP_ROLL
    assert close["exit_note"] is None


# ---------------------------------------------------------------------------
# R5 — legacy cycles become LEGACY_UNRECORDED; snapshots are never fabricated.
# ---------------------------------------------------------------------------
def _legacy_v11_state():
    return {
        "schema_version": 11,
        "metadata": {"last_updated": "2025-01-01T00:00:00Z"},
        "positions": [{"ticker": "OLD", "status": "closed", "leap": None,
                       "leap_legs": [], "short_calls": [], "shares": {"count": 0}}],
        "executions": [
            {"id": "exec_001", "action": "buy_leap", "ticker": "OLD", "strike": 75,
             "contracts": 5, "execution_total": 12000, "stock_price": 100,
             "date": "2025-01-01T00:00:00Z"},   # NO entry_context (pre-feature)
            {"id": "exec_002", "action": "close_leap", "ticker": "OLD", "strike": 75,
             "contracts": 5, "close_total": 13000, "realized_pnl": 1000,
             "date": "2025-01-10T00:00:00Z", "exit_reason": "kill switch"},  # free text
        ],
    }


def test_legacy_state_migrates_to_null_snapshot_and_legacy_reason(store):
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(_legacy_v11_state(), fh)

    state = log.load_state()  # migrates v11 -> v12 + recomputes cycles
    assert state["schema_version"] == migrations.CURRENT_VERSION == 12
    # Position gained a null snapshot (never fabricated from cached bars).
    assert state["positions"][0]["entry_context"] is None
    # The derived cycle carries LEGACY_UNRECORDED + a null entry_context.
    c = state["cycles"][0]
    assert c["exit_reason"] == ExitReason.LEGACY_UNRECORDED
    assert c["entry_context"] is None


# ---------------------------------------------------------------------------
# R6 — calibration loader: tuples for usable cycles, legacy skipped + counted.
# ---------------------------------------------------------------------------
def test_calibration_loader_yields_and_skips(store):
    # One usable cycle (fresh close), one legacy cycle injected by hand.
    _run_cycle(ExitReason.TARGET_REACHED, ticker="NVDA")
    state = log.load_state()
    state["cycles"].append({"id": "cycle_legacy", "ticker": "OLD",
                            "exit_reason": ExitReason.LEGACY_UNRECORDED,
                            "entry_context": None, "net_return_pct": 5.0})
    log.save_state(state)

    tuples, skipped = calibration.load_closed_cycles(log.load_state())
    assert skipped == 1                      # the legacy-null cycle
    assert len(tuples) == 1
    ec, reason, outcome = tuples[0]
    assert reason == ExitReason.TARGET_REACHED
    assert ec is not None and outcome["ticker"] == "NVDA"
    assert "exit_metrics" in outcome
