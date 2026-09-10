"""Roll/defend auto-execute — recommendation_auto_execute's pure functions
(permission storage, ticket->payload, live-expiration resolution) and
recommendation_runner._check_roll_defend_auto_execute's wiring.

Offline, hand-built recs, mirroring test_circuit_breaker_auto_exit.py's shape
for the sibling EXIT-only permission set. The question here is the same one:
does the runner act ONLY on a granted trigger, reuse the exact ticket a human
would submit, refuse to guess a live expiration, and notify-not-raise on a
failure.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-autoroll-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402
import notifier  # noqa: E402
import recommendation_auto_execute as auto_exec  # noqa: E402
import recommendation_runner as runner  # noqa: E402
from rec_types import ActionType, TriggerRule  # noqa: E402

NOW = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_transmit", lambda: False)
    return tmp_path


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _roll_rec(rec_id, ticker, trigger_rule, *, from_strike=130.0, to_strike=135.0,
             from_expiration="2026-09-11", to_dte=4, contracts=5,
             roll_reason="scheduled", action_type=ActionType.ROLL_OUT,
             emission_dte=4):
    """A hand-built open ROLL_OUT/DEFEND rec with a roll_short ticket, shaped
    exactly like recommendation_engine._build_action_rec's output."""
    return {
        "rec_id": rec_id, "emitted_at": _iso(NOW - timedelta(hours=1)),
        "position_id": ticker, "ticker": ticker, "action_type": action_type,
        "trigger_rule": trigger_rule,
        "proposed_ticket": {
            "action": "roll_short", "roll_reason": roll_reason, "ticker": ticker,
            "contracts": contracts,
            "legs": [
                {"instruction": "BUY_TO_CLOSE", "role": "short", "strike": from_strike,
                 "expiration": from_expiration, "quantity": contracts},
                {"instruction": "SELL_TO_OPEN", "role": "short", "strike": to_strike,
                 "dte": to_dte, "quantity": contracts},
            ],
        },
        "input_snapshot": {
            "trigger_detail": {"dte": emission_dte},
            "shorts": [{"strike": from_strike, "dte": emission_dte,
                       "expiration": from_expiration}],
        },
        "valid_until": _iso(NOW + timedelta(hours=20)),
        "supersedes": None, "engine_version": 1,
    }


def _seed(recs):
    state = log.load_state()
    state["recommendations"] = recs
    log.save_state(state)


# ===========================================================================
# Permission storage.
# ===========================================================================
def test_permissions_default_all_off(store):
    assert auto_exec.get_permissions() == {
        TriggerRule.ROLL_SCHEDULED_WEEKLY: False, TriggerRule.ROLL_75PCT: False,
        TriggerRule.ROLL_EXTRINSIC_CAPTURED: False, TriggerRule.DEFEND_BELOW_STRIKE: False,
    }


def test_set_and_get_permission_round_trips(store):
    auto_exec.set_permission(TriggerRule.DEFEND_BELOW_STRIKE, True)
    perms = auto_exec.get_permissions()
    assert perms[TriggerRule.DEFEND_BELOW_STRIKE] is True
    assert perms[TriggerRule.ROLL_75PCT] is False
    auto_exec.set_permission(TriggerRule.DEFEND_BELOW_STRIKE, False)
    assert auto_exec.get_permissions()[TriggerRule.DEFEND_BELOW_STRIKE] is False


def test_set_permission_rejects_an_unknown_trigger(store):
    with pytest.raises(ValueError):
        auto_exec.set_permission(TriggerRule.CIRCUIT_BREAKER, True)
    with pytest.raises(ValueError):
        auto_exec.set_permission("not_a_trigger", True)


# ===========================================================================
# payload_from_ticket / emission_dte — pure.
# ===========================================================================
def test_payload_from_ticket_builds_the_execute_payload():
    rec = _roll_rec("rec_1", "KO", TriggerRule.DEFEND_BELOW_STRIKE, roll_reason="defend")
    payload = auto_exec.payload_from_ticket(rec)
    assert payload == {
        "action": "roll_short", "ticker": "KO", "contracts": 5,
        "from_strike": 130.0, "from_expiration": "2026-09-11",
        "to_strike": 135.0, "to_dte": 4, "roll_reason": "defend",
        "source_rec_id": "rec_1", "client_order_ref": "auto:rec_1",
    }


def test_payload_from_ticket_none_for_a_non_roll_ticket():
    rec = _roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT)
    rec["proposed_ticket"]["action"] = "sell_shares"
    assert auto_exec.payload_from_ticket(rec) is None


def test_payload_from_ticket_none_when_a_leg_is_missing():
    rec = _roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT)
    rec["proposed_ticket"]["legs"] = [rec["proposed_ticket"]["legs"][0]]  # no SELL_TO_OPEN
    assert auto_exec.payload_from_ticket(rec) is None


def test_emission_dte_matches_by_strike_and_expiration():
    rec = _roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT, from_strike=130.0,
                    from_expiration="2026-09-11", emission_dte=6)
    assert auto_exec.emission_dte(rec, 130.0, "2026-09-11") == 6
    assert auto_exec.emission_dte(rec, 130.0, "2026-09-18") is None
    assert auto_exec.emission_dte(rec, 999.0, "2026-09-11") is None


# ===========================================================================
# resolve_live_expiration — pure given the chain fetch is mocked.
# ===========================================================================
def test_resolve_live_expiration_same_week_never_touches_the_chain(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("same-week resolution must not fetch the chain")
    import option_chain
    monkeypatch.setattr(option_chain, "_fetch_chain", _boom)
    assert auto_exec.resolve_live_expiration("KO", "2026-09-11", True) == "2026-09-11"


def test_resolve_live_expiration_next_week_picks_the_next_real_weekly(monkeypatch):
    import option_chain
    import schwab_api
    monkeypatch.setattr(option_chain, "_fetch_chain", lambda t: {"raw": True})
    monkeypatch.setattr(schwab_api, "parse_call_chain", lambda p: (100.0, [{"strike": 130}]))
    monkeypatch.setattr(option_chain, "_weekly_expirations",
                        lambda contracts, count=3: ["2026-09-11", "2026-09-18", "2026-09-25"])
    out = auto_exec.resolve_live_expiration("KO", "2026-09-11", False)
    assert out == "2026-09-18"


def test_resolve_live_expiration_none_when_the_chain_has_no_contracts(monkeypatch):
    import option_chain
    import schwab_api
    monkeypatch.setattr(option_chain, "_fetch_chain", lambda t: {"raw": True})
    monkeypatch.setattr(schwab_api, "parse_call_chain", lambda p: (100.0, []))
    assert auto_exec.resolve_live_expiration("KO", "2026-09-11", False) is None


# ===========================================================================
# _check_roll_defend_auto_execute — the wiring, paper mode (default fixture).
# ===========================================================================
def test_no_permissions_granted_never_calls_execute(store, monkeypatch):
    _seed([_roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT)])
    called = []
    monkeypatch.setattr(executor, "execute", lambda *a, **k: called.append(a) or {"success": True})
    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)
    assert results == [] and called == []


def test_granted_trigger_submits_the_tickets_own_payload(store, monkeypatch):
    auto_exec.set_permission(TriggerRule.ROLL_75PCT, True)
    _seed([_roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT, roll_reason="75%-rule")])
    calls = []

    def _fake_execute(payload, now=None):
        calls.append(payload)
        return {"success": True, "status": "filled"}
    monkeypatch.setattr(executor, "execute", _fake_execute)

    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)

    assert len(calls) == 1
    assert calls[0]["action"] == "roll_short" and calls[0]["ticker"] == "KO"
    assert calls[0]["roll_reason"] == "75%-rule"
    assert calls[0]["source_rec_id"] == "rec_1"
    assert "to_expiration" not in calls[0]  # paper mode never needs a resolved date
    assert results[0]["rec_id"] == "rec_1" and results[0]["success"] is True


def test_ungranted_trigger_is_never_acted_on(store, monkeypatch):
    auto_exec.set_permission(TriggerRule.ROLL_75PCT, True)
    _seed([_roll_rec("rec_1", "KO", TriggerRule.ROLL_SCHEDULED_WEEKLY)])
    called = []
    monkeypatch.setattr(executor, "execute", lambda *a, **k: called.append(1) or {"success": True})
    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)
    assert results == [] and called == []


def test_non_eligible_trigger_is_ignored_even_when_something_else_is_granted(store, monkeypatch):
    auto_exec.set_permission(TriggerRule.ROLL_75PCT, True)
    rec = _roll_rec("rec_1", "KO", TriggerRule.KILL_RS_SPY_CONFIRMED)
    _seed([rec])
    called = []
    monkeypatch.setattr(executor, "execute", lambda *a, **k: called.append(1) or {"success": True})
    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)
    assert results == [] and called == []


def test_multiple_open_recs_are_each_considered(store, monkeypatch):
    auto_exec.set_permission(TriggerRule.ROLL_75PCT, True)
    _seed([_roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT),
          _roll_rec("rec_2", "PEP", TriggerRule.ROLL_SCHEDULED_WEEKLY)])
    calls = []
    monkeypatch.setattr(executor, "execute",
                        lambda payload, now=None: calls.append(payload["ticker"]) or {"success": True})
    runner._check_roll_defend_auto_execute(NOW, dry_run=True)
    # Only KO's trigger (ROLL_75PCT) is granted; PEP's (ROLL_SCHEDULED_WEEKLY) is not.
    assert calls == ["KO"]


def test_a_failed_auto_roll_notifies_and_does_not_raise(store, monkeypatch):
    auto_exec.set_permission(TriggerRule.DEFEND_BELOW_STRIKE, True)
    _seed([_roll_rec("rec_1", "KO", TriggerRule.DEFEND_BELOW_STRIKE, action_type=ActionType.DEFEND)])
    monkeypatch.setattr(executor, "execute",
                        lambda *a, **k: {"success": False, "error": "bad quote"})
    notified = []
    monkeypatch.setattr(notifier, "dispatch",
                        lambda batch, settings, dry_run=None: notified.append(batch))

    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)

    assert results[0]["success"] is False
    assert len(notified) == 1 and notified[0][0]["severity"] == "CRITICAL"
    assert notified[0][0]["ticker"] == "KO"


def test_an_exception_from_execute_is_caught_and_notified(store, monkeypatch):
    auto_exec.set_permission(TriggerRule.ROLL_75PCT, True)
    _seed([_roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT)])

    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(executor, "execute", _boom)
    notified = []
    monkeypatch.setattr(notifier, "dispatch",
                        lambda batch, settings, dry_run=None: notified.append(batch))

    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)

    assert results[0]["success"] is False and "boom" in results[0]["error"]
    assert len(notified) == 1


# ===========================================================================
# _check_roll_defend_auto_execute — live mode: never guesses an expiration.
# ===========================================================================
def test_live_mode_resolves_a_real_expiration_before_submitting(store, monkeypatch):
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    auto_exec.set_permission(TriggerRule.ROLL_SCHEDULED_WEEKLY, True)
    # to_dte (11) = emission_dte (4) + 7 -> a roll OUT, same_week False.
    _seed([_roll_rec("rec_1", "KO", TriggerRule.ROLL_SCHEDULED_WEEKLY,
                     from_expiration="2026-09-11", to_dte=11, emission_dte=4)])
    resolve_calls = []

    def _fake_resolve(ticker, from_expiration, same_week):
        resolve_calls.append((ticker, from_expiration, same_week))
        return "2026-09-18"
    monkeypatch.setattr(auto_exec, "resolve_live_expiration", _fake_resolve)

    calls = []
    monkeypatch.setattr(executor, "execute",
                        lambda payload, now=None: calls.append(payload) or {"success": True})

    runner._check_roll_defend_auto_execute(NOW, dry_run=True)

    assert resolve_calls == [("KO", "2026-09-11", False)]
    assert calls[0]["to_expiration"] == "2026-09-18"


def test_live_mode_same_week_roll_up_resolves_same_week_true(store, monkeypatch):
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    auto_exec.set_permission(TriggerRule.DEFEND_BELOW_STRIKE, True)
    # to_dte (4) == emission_dte (4) -> same_week True.
    _seed([_roll_rec("rec_1", "KO", TriggerRule.DEFEND_BELOW_STRIKE, action_type=ActionType.DEFEND,
                     from_expiration="2026-09-11", to_dte=4, emission_dte=4)])
    resolve_calls = []

    def _fake_resolve(ticker, from_expiration, same_week):
        resolve_calls.append((ticker, from_expiration, same_week))
        return from_expiration
    monkeypatch.setattr(auto_exec, "resolve_live_expiration", _fake_resolve)
    monkeypatch.setattr(executor, "execute", lambda payload, now=None: {"success": True})

    runner._check_roll_defend_auto_execute(NOW, dry_run=True)

    assert resolve_calls == [("KO", "2026-09-11", True)]


def test_live_mode_never_guesses_when_emission_dte_is_unresolvable(store, monkeypatch):
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    auto_exec.set_permission(TriggerRule.ROLL_75PCT, True)
    rec = _roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT)
    rec["input_snapshot"]["shorts"] = []  # no match -> emission_dte is None
    _seed([rec])
    called = []
    monkeypatch.setattr(executor, "execute", lambda *a, **k: called.append(1) or {"success": True})
    resolve_called = []
    monkeypatch.setattr(auto_exec, "resolve_live_expiration",
                        lambda *a, **k: resolve_called.append(1) or "2026-09-18")

    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)

    assert results == [] and called == [] and resolve_called == []


def test_live_mode_skips_when_the_chain_has_no_matching_expiration(store, monkeypatch):
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    auto_exec.set_permission(TriggerRule.ROLL_75PCT, True)
    _seed([_roll_rec("rec_1", "KO", TriggerRule.ROLL_75PCT, to_dte=11, emission_dte=4)])
    monkeypatch.setattr(auto_exec, "resolve_live_expiration", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(executor, "execute", lambda *a, **k: called.append(1) or {"success": True})

    results = runner._check_roll_defend_auto_execute(NOW, dry_run=True)

    assert results == [] and called == []
