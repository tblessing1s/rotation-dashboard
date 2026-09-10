"""Circuit-breaker auto-exit wiring — recommendation_runner's
_check_circuit_breaker_auto_exit and circuit_breaker.get/set_auto_exit_permission.

Offline, hand-built recs. executor.exit_position's own correctness (leg
sequencing, fill confirmation, safe partial-failure) is covered by
test_exit_position.py; the question here is purely "does the runner act ONLY
on nearest_trigger (whichever level is closest to price), only when that
specific condition is granted, and never for anything else — and does a
failure notify without ever raising out of the pass."
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-autoexit-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import circuit_breaker  # noqa: E402
import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402
import notifier  # noqa: E402
import recommendation_runner as runner  # noqa: E402
from rec_types import ActionType, TriggerRule  # noqa: E402

NOW = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)

_CODE_FOR = {"drawdown": "CB_DRAWDOWN_15", "ma_fast": "CB_MA50_3CLOSE",
             "ma_slow": "CB_MA200_CLOSE", "manual_line": "CB_MANUAL_LINE"}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cb_rec(rec_id, ticker, nearest_condition, nearest_price=100.0,
           tripped_conditions=None, levels=None):
    """A hand-built open CIRCUIT_BREAKER EXIT rec. ``nearest_condition`` is
    what nearest_trigger names — the level closest to price, which is what
    the runner acts on. ``tripped_conditions`` defaults to just the nearest
    one; pass more to simulate a multi-condition breach day."""
    tripped_conditions = tripped_conditions or [nearest_condition]
    levels = levels or {nearest_condition: nearest_price}
    return {
        "rec_id": rec_id, "emitted_at": _iso(NOW - timedelta(hours=1)),
        "position_id": ticker, "ticker": ticker, "action_type": ActionType.EXIT,
        "trigger_rule": TriggerRule.CIRCUIT_BREAKER,
        "proposed_ticket": {"action": "sell_shares", "legs": []},
        "input_snapshot": {"trigger_detail": {
            "circuit_breaker": {
                "tripped_conditions": tripped_conditions,
                "levels": levels,
                "nearest_trigger": {"condition": nearest_condition, "price": nearest_price,
                                    "label": nearest_condition},
            },
            "exit_reason_code": _CODE_FOR[nearest_condition],
        }},
        "valid_until": _iso(NOW + timedelta(hours=20)),
        "supersedes": None, "engine_version": 1,
    }


def _seed(recs):
    state = log.load_state()
    state["recommendations"] = recs
    log.save_state(state)


# ===========================================================================
# _check_circuit_breaker_auto_exit — the wiring.
# ===========================================================================
def test_no_permissions_granted_never_calls_exit_position(store, monkeypatch):
    _seed([_cb_rec("rec_1", "KO", "drawdown")])
    called = []
    monkeypatch.setattr(executor, "exit_position",
                        lambda *a, **k: called.append((a, k)) or {"ok": True})
    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)
    assert results == [] and called == []


def test_granted_nearest_condition_triggers_exit_position_with_its_own_reason(store, monkeypatch):
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    _seed([_cb_rec("rec_1", "KO", "drawdown", nearest_price=85.0)])
    calls = []

    def _fake_exit(ticker, exit_reason, exit_note=None, source_rec_id=None, now=None):
        calls.append({"ticker": ticker, "exit_reason": exit_reason,
                      "source_rec_id": source_rec_id})
        return {"ok": True, "position_closed": True, "steps": []}
    monkeypatch.setattr(executor, "exit_position", _fake_exit)

    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)

    assert len(calls) == 1
    assert calls[0] == {"ticker": "KO", "exit_reason": "CB_DRAWDOWN_15",
                        "source_rec_id": "rec_1"}
    assert results[0]["ok"] is True and results[0]["condition"] == "drawdown"


def test_ungranted_nearest_condition_is_never_acted_on(store, monkeypatch):
    # ma_fast is granted, but the NEAREST level on this rec is ma_slow — not
    # granted, so nothing fires even though ma_fast is also tripped.
    circuit_breaker.set_auto_exit_permission("ma_fast", True)
    _seed([_cb_rec("rec_1", "KO", "ma_slow", nearest_price=90.0,
                  tripped_conditions=["ma_slow", "ma_fast"],
                  levels={"ma_slow": 90.0, "ma_fast": 80.0})])
    called = []
    monkeypatch.setattr(executor, "exit_position",
                        lambda *a, **k: called.append(1) or {"ok": True})
    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)
    assert results == [] and called == []


def test_multi_condition_breach_acts_on_the_nearest_one_even_when_a_farther_one_is_granted(
        store, monkeypatch):
    # Both ma_slow (nearest, granted) and drawdown (farther, also granted)
    # tripped on the same day. Only the nearest is acted on, with its own
    # coded reason — not drawdown's, even though drawdown is granted too.
    circuit_breaker.set_auto_exit_permission("ma_slow", True)
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    _seed([_cb_rec("rec_1", "KO", "ma_slow", nearest_price=90.0,
                  tripped_conditions=["ma_slow", "drawdown"],
                  levels={"ma_slow": 90.0, "drawdown": 80.0})])
    calls = []

    def _fake_exit(ticker, exit_reason, **k):
        calls.append(exit_reason)
        return {"ok": True, "position_closed": True, "steps": []}
    monkeypatch.setattr(executor, "exit_position", _fake_exit)

    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)

    assert calls == ["CB_MA200_CLOSE"]
    assert results[0]["condition"] == "ma_slow"


def test_manual_line_as_nearest_blocks_execution_even_with_other_conditions_granted(
        store, monkeypatch):
    # The manual line can never itself be granted, and when it is the
    # NEAREST level, it blocks auto-exit entirely on that rec — even though
    # a farther condition is granted.
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    _seed([_cb_rec("rec_1", "KO", "manual_line", nearest_price=95.0,
                  tripped_conditions=["manual_line", "drawdown"],
                  levels={"manual_line": 95.0, "drawdown": 80.0})])
    called = []
    monkeypatch.setattr(executor, "exit_position",
                        lambda *a, **k: called.append(1) or {"ok": True})
    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)
    assert results == [] and called == []


def test_non_circuit_breaker_exit_recs_are_ignored(store, monkeypatch):
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    circuit_breaker.set_auto_exit_permission("ma_fast", True)
    circuit_breaker.set_auto_exit_permission("ma_slow", True)
    rec = _cb_rec("rec_1", "KO", "drawdown")
    rec["trigger_rule"] = TriggerRule.KILL_RS_SPY_CONFIRMED
    _seed([rec])
    called = []
    monkeypatch.setattr(executor, "exit_position",
                        lambda *a, **k: called.append(1) or {"ok": True})
    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)
    assert results == [] and called == []


def test_a_no_action_rec_is_ignored_even_if_it_somehow_named_the_rule(store, monkeypatch):
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    rec = _cb_rec("rec_1", "KO", "drawdown")
    rec["action_type"] = ActionType.NO_ACTION
    _seed([rec])
    called = []
    monkeypatch.setattr(executor, "exit_position",
                        lambda *a, **k: called.append(1) or {"ok": True})
    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)
    assert results == [] and called == []


def test_a_failed_auto_exit_notifies_and_does_not_raise(store, monkeypatch):
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    _seed([_cb_rec("rec_1", "KO", "drawdown")])
    monkeypatch.setattr(executor, "exit_position",
                        lambda *a, **k: {"ok": False, "position_closed": False,
                                        "error": "no price available", "steps": []})
    notified = []
    monkeypatch.setattr(notifier, "dispatch",
                        lambda batch, settings, dry_run=None: notified.append(batch))

    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)

    assert results[0]["ok"] is False
    assert len(notified) == 1 and notified[0][0]["severity"] == "CRITICAL"
    assert notified[0][0]["ticker"] == "KO"


def test_an_already_closed_result_does_not_notify(store, monkeypatch):
    # A harmless re-check racing resolution matching (the position was already
    # fully closed by an earlier pass) is not a failure worth paging on.
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    _seed([_cb_rec("rec_1", "KO", "drawdown")])
    monkeypatch.setattr(executor, "exit_position",
                        lambda *a, **k: {"ok": False, "position_closed": True,
                                        "error": "no open position", "steps": []})
    notified = []
    monkeypatch.setattr(notifier, "dispatch",
                        lambda batch, settings, dry_run=None: notified.append(batch))

    runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)

    assert notified == []


def test_an_exception_from_exit_position_is_caught_and_notified(store, monkeypatch):
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    _seed([_cb_rec("rec_1", "KO", "drawdown")])

    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(executor, "exit_position", _boom)
    notified = []
    monkeypatch.setattr(notifier, "dispatch",
                        lambda batch, settings, dry_run=None: notified.append(batch))

    results = runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)

    assert results[0]["ok"] is False and "boom" in results[0]["error"]
    assert len(notified) == 1


def test_multiple_open_circuit_breaker_recs_are_each_considered(store, monkeypatch):
    circuit_breaker.set_auto_exit_permission("drawdown", True)
    _seed([_cb_rec("rec_1", "KO", "drawdown"),
          _cb_rec("rec_2", "PEP", "ma_slow")])
    calls = []
    monkeypatch.setattr(executor, "exit_position",
                        lambda ticker, **k: calls.append(ticker) or
                        {"ok": True, "position_closed": True, "steps": []})
    runner._check_circuit_breaker_auto_exit(NOW, dry_run=True)
    # Only KO's nearest condition (drawdown) is granted; PEP's (ma_slow) is not.
    assert calls == ["KO"]


# ===========================================================================
# Permission storage.
# ===========================================================================
def test_permissions_default_all_off(store):
    assert circuit_breaker.get_auto_exit_permissions() == {
        "drawdown": False, "ma_fast": False, "ma_slow": False}


def test_set_and_get_permission_round_trips(store):
    circuit_breaker.set_auto_exit_permission("ma_slow", True)
    perms = circuit_breaker.get_auto_exit_permissions()
    assert perms == {"drawdown": False, "ma_fast": False, "ma_slow": True}
    circuit_breaker.set_auto_exit_permission("ma_slow", False)
    assert circuit_breaker.get_auto_exit_permissions()["ma_slow"] is False


def test_set_permission_rejects_an_unknown_condition(store):
    with pytest.raises(ValueError):
        circuit_breaker.set_auto_exit_permission("manual_line", True)
    with pytest.raises(ValueError):
        circuit_breaker.set_auto_exit_permission("not_a_condition", True)


# ===========================================================================
# exit_reason_code_for — the per-condition lookup the auto-exit path uses.
# ===========================================================================
def test_exit_reason_code_for_known_conditions():
    assert circuit_breaker.exit_reason_code_for("drawdown") == "CB_DRAWDOWN_15"
    assert circuit_breaker.exit_reason_code_for("ma_fast") == "CB_MA50_3CLOSE"
    assert circuit_breaker.exit_reason_code_for("ma_slow") == "CB_MA200_CLOSE"
    assert circuit_breaker.exit_reason_code_for("manual_line") == "CB_MANUAL_LINE"


def test_exit_reason_code_for_unknown_condition_is_none():
    assert circuit_breaker.exit_reason_code_for("not_a_condition") is None
    assert circuit_breaker.exit_reason_code_for(None) is None
