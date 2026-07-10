"""Trust-derivation tests — resolution matching, coverage misses, precision,
timeliness, graduation math, migration, and crash recovery. Fully offline;
everything derives from hand-built immutable records + an injected clock."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-trust-test-"))

import config  # noqa: E402
import migrations  # noqa: E402
import trust_derive  # noqa: E402
from rec_types import ActionType, Resolution, TriggerRule  # noqa: E402

NOW = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
SINCE = "2026-01-01T00:00:00Z"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(recs=(), overrides=(), execs=(), events=(), receipts=(), fidelity=None):
    return {
        "schema_version": migrations.CURRENT_VERSION,
        "metadata": {"trust_layer_since": SINCE},
        "positions": [], "executions": list(execs),
        "recommendations": list(recs),
        "recommendation_overrides": list(overrides),
        "order_events": list(events), "order_receipts": list(receipts),
        "order_fidelity": dict(fidelity or {}),
        "roll_ledger": {"rolls": []},
    }


def _rec(rec_id="rec_00001", action=ActionType.EXIT, ticker="AAPL",
         emitted=NOW - timedelta(hours=6), valid_hours=72, supersedes=None,
         trigger=TriggerRule.KILL_RS_SECTOR, first_true=None, strike=None):
    ticket = None
    if action != ActionType.NO_ACTION:
        legs = ([{"instruction": "SELL_TO_OPEN", "role": "short",
                  "strike": strike, "quantity": 1}] if strike is not None else [])
        ticket = {"action": "roll_short" if action in (ActionType.ROLL_OUT, ActionType.DEFEND)
                  else "close_position", "legs": legs,
                  "min_acceptable_net_credit": 1.00,
                  "max_slippage_pct_of_mid": 0.05}
    return {"rec_id": rec_id, "emitted_at": _iso(emitted),
            "position_id": ticker if action != ActionType.ENTER else None,
            "ticker": ticker, "action_type": action, "trigger_rule": trigger,
            "proposed_ticket": ticket,
            "input_snapshot": {"condition_first_true_at": first_true},
            "valid_until": _iso(emitted + timedelta(hours=valid_hours)),
            "supersedes": supersedes, "engine_version": 1}


def _exit_exec(eid="exec_001", ticker="AAPL", at=NOW - timedelta(hours=2),
               live=True, src=None):
    e = {"id": eid, "ticker": ticker, "action": "close_leap", "date": _iso(at),
         "strike": 130.0, "contracts": 1, "live_transmitted": live,
         "exit_reason": "KILL_SWITCH_SECTOR"}
    if src:
        e["source_rec_id"] = src
    return e


def _roll_pair(gid="roll1", ticker="AAPL", at=NOW - timedelta(hours=2),
               reason="defend", new_strike=176.0, live=False):
    return [
        {"id": f"{gid}_c", "ticker": ticker, "action": "close_short",
         "date": _iso(at), "strike": 180.0, "contracts": 1,
         "roll_group_id": gid, "roll_id": gid, "roll_reason": reason,
         "close_price_per_share": 2.0, "live_transmitted": live},
        {"id": f"{gid}_s", "ticker": ticker, "action": "sell_short",
         "date": _iso(at), "strike": new_strike, "contracts": 1,
         "roll_group_id": gid, "roll_id": gid, "roll_reason": reason,
         "premium_per_share": 3.4, "live_transmitted": live},
    ]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def test_execution_matches_open_recommendation_with_deltas():
    state = _state(recs=[_rec()], execs=[_exit_exec()])
    res = trust_derive.resolve(state, NOW)
    m = [r for r in res if r["status"] == Resolution.EXECUTED_MATCHED]
    assert len(m) == 1
    assert m[0]["rec_id"] == "rec_00001"
    assert m[0]["deltas"]["hours_from_emission"] == 4.0
    assert not [r for r in res if r["status"] == Resolution.COVERAGE_MISS]


def test_coverage_miss_synthesized_for_unmatched_execution():
    state = _state(execs=[_exit_exec()])
    res = trust_derive.resolve(state, NOW)
    misses = [r for r in res if r["status"] == Resolution.COVERAGE_MISS]
    assert len(misses) == 1
    assert misses[0]["action_type"] == ActionType.EXIT
    assert misses[0]["execution_ids"] == ["exec_001"]
    assert misses[0]["rec_id"] is None


def test_all_clear_does_not_excuse_a_coverage_miss():
    clear = _rec("rec_00001", action=ActionType.NO_ACTION,
                 trigger=TriggerRule.ALL_CLEAR, valid_hours=26)
    state = _state(recs=[clear], execs=[_exit_exec()])
    res = trust_derive.resolve(state, NOW)
    assert [r for r in res if r["status"] == Resolution.COVERAGE_MISS]


def test_stale_recommendation_never_matches_a_later_action():
    old = _rec(emitted=NOW - timedelta(hours=100), valid_hours=72)
    state = _state(recs=[old], execs=[_exit_exec(at=NOW - timedelta(hours=1))])
    res = trust_derive.resolve(state, NOW)
    assert [r for r in res if r["status"] == Resolution.COVERAGE_MISS], \
        "action after valid_until must be a miss, not a match"
    assert [r for r in res if r["rec_id"] == old["rec_id"]
            and r["status"] == Resolution.EXPIRED]


def test_pre_activation_executions_are_excluded_from_matching():
    ancient = _exit_exec(at=datetime(2025, 6, 1, tzinfo=timezone.utc))
    state = _state(execs=[ancient])
    res = trust_derive.resolve(state, NOW)
    assert res == []   # no miss synthesized for pre-trust-layer history


def test_supersession_chain_only_latest_matchable():
    r1 = _rec("rec_00001", emitted=NOW - timedelta(hours=10))
    r2 = _rec("rec_00002", emitted=NOW - timedelta(hours=5), supersedes="rec_00001")
    state = _state(recs=[r1, r2], execs=[_exit_exec(at=NOW - timedelta(hours=1))])
    res = trust_derive.resolve(state, NOW)
    by = {r["rec_id"]: r for r in res if r.get("rec_id")}
    assert by["rec_00001"]["status"] == Resolution.SUPERSEDED
    assert by["rec_00002"]["status"] == Resolution.EXECUTED_MATCHED


def test_source_rec_id_wins_over_latest_emitted():
    r1 = _rec("rec_00001", emitted=NOW - timedelta(hours=10))
    r2 = _rec("rec_00002", emitted=NOW - timedelta(hours=5))
    state = _state(recs=[r1, r2],
                   execs=[_exit_exec(at=NOW - timedelta(hours=1), src="rec_00001")])
    res = trust_derive.resolve(state, NOW)
    by = {r["rec_id"]: r for r in res if r.get("rec_id")}
    assert by["rec_00001"]["status"] == Resolution.EXECUTED_MATCHED


def test_roll_pair_matches_defend_and_records_strike_delta():
    rec = _rec("rec_00001", action=ActionType.DEFEND, strike=175.5,
               trigger=TriggerRule.DEFEND_BELOW_STRIKE)
    state = _state(recs=[rec], execs=_roll_pair(new_strike=176.0))
    res = trust_derive.resolve(state, NOW)
    m = [r for r in res if r["status"] == Resolution.EXECUTED_MATCHED]
    assert len(m) == 1
    assert m[0]["deltas"]["strike_delta"] == 0.5
    # net = 3.4 new premium - 2.0 buyback = 1.4 vs min 1.00 -> +0.4
    assert m[0]["deltas"]["credit_delta_vs_min"] == 0.4


def test_roll_reason_scopes_action_type():
    rec = _rec("rec_00001", action=ActionType.ROLL_OUT, trigger=TriggerRule.ROLL_75PCT)
    state = _state(recs=[rec], execs=_roll_pair(reason="75%-rule"))
    res = trust_derive.resolve(state, NOW)
    assert [r for r in res if r["status"] == Resolution.EXECUTED_MATCHED]
    # a defend rec must NOT match a scheduled roll
    rec2 = _rec("rec_00002", action=ActionType.DEFEND,
                trigger=TriggerRule.DEFEND_BELOW_STRIKE)
    state2 = _state(recs=[rec2], execs=_roll_pair(gid="roll2", reason="scheduled"))
    res2 = trust_derive.resolve(state2, NOW)
    assert [r for r in res2 if r["status"] == Resolution.COVERAGE_MISS]


def test_out_of_scope_executions_never_miss():
    leap_roll = [{"id": "x1", "ticker": "AAPL", "action": "close_leap",
                  "date": _iso(NOW - timedelta(hours=2)), "leap_roll_id": "lr1"},
                 {"id": "x2", "ticker": "AAPL", "action": "buy_leap",
                  "date": _iso(NOW - timedelta(hours=2)), "leap_roll_id": "lr1"}]
    scale_in = [{"id": "x3", "ticker": "AAPL", "action": "buy_leap",
                 "date": _iso(NOW - timedelta(hours=1)), "leap_add": "add"}]
    adjustment = [{"id": "x4", "ticker": "AAPL", "action": "adjustment",
                   "date": _iso(NOW - timedelta(hours=1)), "reason": "expiry"}]
    state = _state(execs=leap_roll + scale_in + adjustment)
    assert trust_derive.resolve(state, NOW) == []


# ---------------------------------------------------------------------------
# Override flow + precision
# ---------------------------------------------------------------------------
def test_override_resolution_and_precision_math():
    r1 = _rec("rec_00001", emitted=NOW - timedelta(days=2))
    r2 = _rec("rec_00002", emitted=NOW - timedelta(days=1, hours=2))
    ov = {"id": "rov_00001", "rec_id": "rec_00002", "reason": "DISAGREE_TIMING",
          "note": None, "at": _iso(NOW - timedelta(days=1))}
    state = _state(recs=[r1, r2], overrides=[ov],
                   execs=[_exit_exec(at=NOW - timedelta(days=1, hours=23))])
    res = trust_derive.resolve(state, NOW)
    by = {r["rec_id"]: r for r in res if r.get("rec_id")}
    assert by["rec_00001"]["status"] == Resolution.EXECUTED_MATCHED
    assert by["rec_00002"]["status"] == Resolution.OVERRIDDEN
    assert by["rec_00002"]["reason"] == "DISAGREE_TIMING"
    board = trust_derive.scoreboard(state, res, {}, NOW)
    p = board["by_action_type"][ActionType.EXIT]["precision"]
    assert p["executed_matched"] == 1 and p["overridden"] == 1
    assert p["rate"] == 0.5
    assert p["override_breakdown"] == {"DISAGREE_TIMING": 1}


def test_overridden_recommendation_never_matches():
    r1 = _rec("rec_00001")
    ov = {"id": "rov_00001", "rec_id": "rec_00001", "reason": "DISAGREE_ACTION",
          "at": _iso(NOW - timedelta(hours=3))}
    state = _state(recs=[r1], overrides=[ov],
                   execs=[_exit_exec(at=NOW - timedelta(hours=1))])
    res = trust_derive.resolve(state, NOW)
    assert [r for r in res if r["status"] == Resolution.COVERAGE_MISS]


def test_expired_resolution():
    r1 = _rec("rec_00001", emitted=NOW - timedelta(hours=100), valid_hours=72)
    res = trust_derive.resolve(_state(recs=[r1]), NOW)
    assert res == [{"rec_id": "rec_00001", "status": Resolution.EXPIRED,
                    "action_type": ActionType.EXIT, "ticker": "AAPL",
                    "at": r1["valid_until"]}]


def test_open_recommendations_survive_restart_and_stay_matchable():
    r1 = _rec("rec_00001", emitted=NOW - timedelta(hours=6))
    state = _state(recs=[r1])
    trust_derive.recompute(state, NOW)   # as recompute_derived would on reload
    assert [r["rec_id"] for r in trust_derive.open_recommendations(state, NOW)] == ["rec_00001"]
    # ... and a later execution still matches (nothing was lost in the crash)
    state["executions"] = [_exit_exec(at=NOW + timedelta(hours=1))]
    res = trust_derive.resolve(state, NOW + timedelta(hours=2))
    assert [r for r in res if r["status"] == Resolution.EXECUTED_MATCHED]


# ---------------------------------------------------------------------------
# Timeliness
# ---------------------------------------------------------------------------
def test_timeliness_emission_lag_recorded():
    first_true = _iso(NOW - timedelta(days=2, hours=6))
    r1 = _rec("rec_00001", emitted=NOW - timedelta(hours=6), first_true=first_true)
    board = trust_derive.scoreboard(_state(recs=[r1]), [], {}, NOW)
    row = board["timeliness"]["rows"][0]
    assert row["emission_lag_days"] == 2.0
    assert board["timeliness"]["avg_emission_lag_days"] == 2.0


def test_recommendation_emitted_after_operator_acted_is_flagged():
    # condition true at T0; the operator exited BEFORE the engine emitted.
    first_true = _iso(NOW - timedelta(days=3))
    r1 = _rec("rec_00001", emitted=NOW - timedelta(hours=2), first_true=first_true)
    ex = _exit_exec(at=NOW - timedelta(days=1))
    board = trust_derive.scoreboard(_state(recs=[r1], execs=[ex]), [], {}, NOW)
    assert board["timeliness"]["late_after_action_count"] == 1
    assert board["timeliness"]["rows"][0]["late_after_action"] is True


# ---------------------------------------------------------------------------
# Graduation math
# ---------------------------------------------------------------------------
def _clean_history(n=12, action=ActionType.EXIT):
    """n matched live cycles inside the trailing window: recs + executions."""
    recs, execs = [], []
    for i in range(n):
        emitted = NOW - timedelta(weeks=min(i, 20), hours=8)
        rid = f"rec_{i:05d}"
        recs.append(_rec(rid, action=action, emitted=emitted))
        execs.append(_exit_exec(f"exec_{i:03d}",
                                at=emitted + timedelta(hours=4), live=True))
    return recs, execs


def test_graduation_blocked_by_one_coverage_miss():
    recs, execs = _clean_history(12)
    # a DIFFERENT ticker so it can't fold into a same-day AAPL exit group
    execs.append(_exit_exec("exec_999", ticker="MSFT", at=NOW - timedelta(hours=1)))
    state = _state(recs=recs, execs=execs)
    res = trust_derive.resolve(state, NOW)
    board = trust_derive.scoreboard(state, res, {}, NOW)
    grad = board["by_action_type"][ActionType.EXIT]["graduation"]
    assert grad["eligible"] is False
    assert any("coverage miss" in f for f in grad["failing"])


def test_graduation_blocked_under_min_live_cycles():
    recs, execs = _clean_history(3)
    state = _state(recs=recs, execs=execs)
    res = trust_derive.resolve(state, NOW)
    grad = trust_derive.scoreboard(state, res, {}, NOW)["by_action_type"][ActionType.EXIT]["graduation"]
    assert grad["eligible"] is False
    assert any("GRAD_MIN_LIVE_CYCLES" in f for f in grad["failing"])


def test_graduation_blocked_by_reconciliation_not_yet_implemented():
    """A clean full window still may not graduate while RECONCILED_CLEAN is
    NOT_YET_IMPLEMENTED — and the reason is NAMED."""
    recs, execs = _clean_history(12)
    state = _state(recs=recs, execs=execs)
    res = trust_derive.resolve(state, NOW)
    # every matched resolution is live and matched; no misses, no overrides
    assert not [r for r in res if r["status"] == Resolution.COVERAGE_MISS]
    fidelity = trust_derive.derive_order_fidelity(state, NOW)
    grad = trust_derive.scoreboard(state, res, fidelity, NOW)["by_action_type"][ActionType.EXIT]["graduation"]
    assert grad["eligible"] is False
    assert any("NOT_YET_IMPLEMENTED" in f for f in grad["failing"]), grad["failing"]
    only_blocker_kinds = [f for f in grad["failing"] if "NOT_YET_IMPLEMENTED" not in f]
    assert only_blocker_kinds == [], f"unexpected extra blockers: {only_blocker_kinds}"


def test_enter_never_auto_eligible():
    grad = trust_derive.scoreboard(_state(), [], {}, NOW)["by_action_type"][ActionType.ENTER]["graduation"]
    assert grad["eligible"] is False
    assert any("never auto-eligible" in f for f in grad["failing"])


def test_override_rate_blocks_graduation():
    recs, execs = _clean_history(12)
    # two overridden recs -> 2/14 decided > 0.10
    r13 = _rec("rec_x1", emitted=NOW - timedelta(days=3))
    r14 = _rec("rec_x2", emitted=NOW - timedelta(days=2))
    ovs = [{"id": "rov_1", "rec_id": "rec_x1", "reason": "DISAGREE_TIMING",
            "at": _iso(NOW - timedelta(days=2))},
           {"id": "rov_2", "rec_id": "rec_x2", "reason": "DISAGREE_ACTION",
            "at": _iso(NOW - timedelta(days=1))}]
    state = _state(recs=recs + [r13, r14], overrides=ovs, execs=execs)
    res = trust_derive.resolve(state, NOW)
    grad = trust_derive.scoreboard(state, res, {}, NOW)["by_action_type"][ActionType.EXIT]["graduation"]
    assert grad["eligible"] is False
    assert any("override rate" in f for f in grad["failing"])
    assert any("DISAGREE_ACTION" in f for f in grad["failing"])


# ---------------------------------------------------------------------------
# Migration + writers (real store round-trip)
# ---------------------------------------------------------------------------
def test_v17_migration_adds_trust_stores_and_since_marker():
    old = {"schema_version": 16, "metadata": {}, "positions": [], "executions": []}
    out, changed = migrations.migrate(dict(old))
    assert changed and out["schema_version"] == 17
    assert out["recommendations"] == []
    assert out["recommendation_overrides"] == []
    assert out["order_fidelity"] == {}
    assert out["metadata"]["trust_layer_since"]
    # idempotent: migrating again changes nothing
    again, changed2 = migrations.migrate(dict(out))
    assert not changed2
    assert again["metadata"]["trust_layer_since"] == out["metadata"]["trust_layer_since"]


def test_writers_persist_and_recompute(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "active_state_path",
                        lambda: str(tmp_path / "state.json"))
    import logging_handler as log
    stored = log.append_recommendations([_rec("ignored_id_gets_kept")])
    assert stored[0]["rec_id"] == "ignored_id_gets_kept"
    state = log.load_state()
    assert len(state["recommendations"]) == 1
    assert state["trust_scoreboard"]["open_recommendations"] == 1
    ov = log.append_recommendation_override(
        {"rec_id": "ignored_id_gets_kept", "reason": "DISAGREE_STRIKE"})
    assert ov["id"] == "rov_00001"
    state = log.load_state()
    res = state["recommendation_resolutions"]
    assert res and res[0]["status"] == Resolution.OVERRIDDEN
    assert state["trust_scoreboard"]["open_recommendations"] == 0
