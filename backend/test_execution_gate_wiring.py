"""Executor wiring for the market-settle gate (offline, injected clock).

Verifies the gate is enforced in the shared executor.execute path: blocked in the
settle window, allowed midday, dormant when enforcement is off, the DEFENSE-vs-
routine-roll routing, emergency tagging onto the immutable record, and the
spread-quality acknowledge. Enforcement is behind config.market_settle_gate_enabled
(off by default), so these tests turn it on explicitly.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
import execution_gate as eg
import executor
import logging_handler as log
import spread_monitor

ET = ZoneInfo("America/New_York")


@pytest.fixture()
def paper(tmp_path, monkeypatch):
    """A paper session with an isolated state file and the gate ENFORCING."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "active_state_path", lambda: str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(config, "market_settle_gate_enabled", lambda: True)
    return monkeypatch


def _roll(**over):
    p = {"action": "roll_short", "ticker": "XLK", "stock_price": 180.0,
         "contracts": 5, "roll_reason": "scheduled"}
    p.update(over)
    return p


# ---- enforcement in execute() ------------------------------------------------

def test_execute_blocks_routine_roll_in_settle_window(paper):
    now = datetime(2026, 7, 13, 9, 40, tzinfo=ET)   # 10 min after open
    with pytest.raises(executor.ExecutionWindowError) as ei:
        executor.execute(_roll(), now=now)
    assert ei.value.reason == eg.WindowReason.SETTLE_WINDOW
    assert ei.value.executable_at == datetime(2026, 7, 13, 10, 0, tzinfo=ET)


def test_execute_blocks_when_market_closed(paper):
    now = datetime(2026, 7, 13, 8, 0, tzinfo=ET)    # pre-open
    with pytest.raises(executor.ExecutionWindowError) as ei:
        executor.execute(_roll(), now=now)
    assert ei.value.reason == eg.WindowReason.MARKET_CLOSED


def test_execute_gate_dormant_when_enforcement_off(paper):
    # Enforcement OFF -> the window gate must not raise (a later dispatch step may,
    # but never the gate). Verified at the enforcement boundary.
    paper.setattr(config, "market_settle_gate_enabled", lambda: False)
    now = datetime(2026, 7, 13, 9, 40, tzinfo=ET)
    verdict = executor._enforce_execution_window("roll_short", "XLK", _roll(), now)
    assert verdict is not None and not verdict.allowed   # computed but not enforced


def test_cancel_and_adjustment_are_never_gated(paper):
    now = datetime(2026, 7, 13, 9, 40, tzinfo=ET)
    assert executor._enforce_execution_window("adjustment", "XLK", {}, now) is None
    # CANCEL never reaches execute(); classify maps it, gate returns allowed.
    assert executor._enforce_execution_window("cancel_order", "XLK", {}, now) is None


# ---- DEFENSE vs routine roll routing + emergency tagging ----------------------

def test_defense_routes_and_emergency_tags_payload(paper, monkeypatch):
    now = datetime(2026, 7, 13, 9, 36, tzinfo=ET)   # settle window
    # Force a qualifying gap-emergency context (data layer is mocked out).
    monkeypatch.setattr(executor, "_build_gap_context", lambda *a, **k: eg.GapContext(
        adverse_gap_atr=2.5, two_sided_print_minutes=6.0, is_limit_order=True))
    payload = _roll(roll_reason="defend")
    verdict = executor._enforce_execution_window("roll_short", "XLK", payload, now)
    assert verdict.allowed and verdict.emergency_path
    assert payload.get("emergency_path") is True         # tagged for the record
    assert payload.get("gate_reason") == eg.WindowReason.GAP_EMERGENCY_UNLOCK


def test_routine_roll_never_takes_emergency_path(paper, monkeypatch):
    now = datetime(2026, 7, 13, 9, 36, tzinfo=ET)
    monkeypatch.setattr(executor, "_build_gap_context", lambda *a, **k: eg.GapContext(
        adverse_gap_atr=2.5, two_sided_print_minutes=6.0, is_limit_order=True))
    payload = _roll(roll_reason="scheduled")   # routine
    with pytest.raises(executor.ExecutionWindowError):
        executor._enforce_execution_window("roll_short", "XLK", payload, now)
    assert "emergency_path" not in payload


# ---- spread-quality acknowledge ---------------------------------------------

def _seed_baseline(symbol="XLK_SHORT", spread=0.10, n=6):
    state = log.load_state()
    for _ in range(n):
        spread_monitor.record(state, symbol, bid=1.0, ask=1.0 + spread)
    log.save_state(state)


def test_wide_spread_requires_ack(paper):
    _seed_baseline()   # baseline spread 0.10 over 6 samples
    payload = _roll(action="close_short", option_symbol="XLK_SHORT",
                    current_spread=0.40, roll_reason=None)
    verdict = executor._enforce_execution_window(
        "close_short", "XLK", payload, datetime(2026, 7, 13, 12, 0, tzinfo=ET))
    with pytest.raises(executor.SpreadAckRequiredError) as ei:
        executor._enforce_spread_quality("XLK", payload, verdict)
    assert ei.value.est_excess_slippage_usd is not None


def test_wide_spread_with_ack_passes(paper):
    _seed_baseline()
    payload = _roll(action="close_short", option_symbol="XLK_SHORT",
                    current_spread=0.40, spread_ack=True)
    verdict = executor._enforce_execution_window(
        "close_short", "XLK", payload, datetime(2026, 7, 13, 12, 0, tzinfo=ET))
    executor._enforce_spread_quality("XLK", payload, verdict)   # no raise
    assert payload.get("spread_warning") == eg.SpreadWarning.WIDE_SPREAD


def test_no_baseline_never_blocks(paper):
    payload = _roll(action="close_short", option_symbol="XLK_NEW",
                    current_spread=0.40)
    verdict = executor._enforce_execution_window(
        "close_short", "XLK", payload, datetime(2026, 7, 13, 12, 0, tzinfo=ET))
    executor._enforce_spread_quality("XLK", payload, verdict)   # no raise: no baseline


def test_emergency_wide_spread_shown_but_not_blocked(paper, monkeypatch):
    _seed_baseline("XLK_SHORT")
    now = datetime(2026, 7, 13, 9, 36, tzinfo=ET)
    monkeypatch.setattr(executor, "_build_gap_context", lambda *a, **k: eg.GapContext(
        adverse_gap_atr=2.5, two_sided_print_minutes=6.0, is_limit_order=True))
    payload = _roll(action="close_position_atomic", option_symbol="XLK_SHORT",
                    current_spread=0.40)
    verdict = executor._enforce_execution_window("close_position_atomic", "XLK", payload, now)
    assert verdict.emergency_path
    executor._enforce_spread_quality("XLK", payload, verdict)   # no raise on emergency
    assert payload.get("spread_warning") == eg.SpreadWarning.WIDE_SPREAD
