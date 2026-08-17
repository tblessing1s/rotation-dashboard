"""Shares-primary is PERMANENT — the LEAP entry path has no way back on.

test_shares_migration covers the guard's behavior when it is on. This file covers
the stronger claim the lock makes: production boots with the guard on and nothing
in the environment can turn it off. The constant survives only as a test/replay
seam, so these tests read the value production boots with rather than assuming it.

Run: python -m pytest backend/test_shares_locked.py -q
"""
import importlib.util
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402


def _boot_value() -> bool:
    """LEGACY_LEAP_READONLY as a fresh interpreter would compute it. Loaded into a
    throwaway module namespace so the live ``config`` other modules hold a
    reference to is untouched (conftest's autouse fixture relaxes the live
    attribute suite-wide, so the live one can't answer this question)."""
    spec = importlib.util.spec_from_file_location("_config_boot_probe", config.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.LEGACY_LEAP_READONLY


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)  # paper path
    return tmp_path


@pytest.fixture()
def production_default(monkeypatch):
    """Restore the guard to whatever production boots with (the suite defaults it
    off so pre-migration tests can seed legacy fixtures — see conftest)."""
    monkeypatch.setattr(config, "LEGACY_LEAP_READONLY", _boot_value())


def test_guard_is_on_by_default():
    assert _boot_value() is True


@pytest.mark.parametrize("env_value", ["0", "false", "", "no"])
def test_guard_is_not_env_overridable(monkeypatch, env_value):
    # The escape hatch is retired: no environment value re-enables LEAP entry.
    monkeypatch.setenv("LEGACY_LEAP_READONLY", env_value)
    assert _boot_value() is True


@pytest.mark.parametrize("payload", [
    {"action": "buy_leap", "ticker": "NVDA", "strike": 100, "contracts": 1,
     "execution_price": 5000, "stock_price": 120, "override_reason": "x"},
    {"action": "roll_leap", "ticker": "NVDA", "stock_price": 120},
    {"action": "open_position_atomic", "ticker": "NVDA", "contracts": 1,
     "stock_price": 120, "strike": 100, "short_strike": 118},
])
def test_leap_open_actions_blocked_at_production_default(store, production_default, payload):
    before = len(log.load_state().get("executions") or [])
    with pytest.raises(executor.LegacyLeapBlocked) as exc:
        executor.execute(payload)
    assert exc.value.action == payload["action"]
    # Refused up-front — no order path, no state mutation, no appended execution.
    assert len(log.load_state().get("executions") or []) == before


def test_block_message_offers_no_way_to_re_enable(production_default):
    # The message points at the replacement action, not at a flag to flip.
    with pytest.raises(executor.LegacyLeapBlocked) as exc:
        executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 100,
                          "contracts": 1, "execution_price": 5000, "stock_price": 120})
    msg = str(exc.value)
    assert "buy_shares" in msg
    assert "LEGACY_LEAP_READONLY" not in msg
