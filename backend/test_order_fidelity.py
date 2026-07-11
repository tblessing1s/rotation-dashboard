"""Order-fidelity ledger tests — lifecycle legality, slippage bound, orphan-leg
detection (incl. the fill-during-cancel race), confirmed-dead cancels, paper
tickets, retention past the event cap, and the paging path."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-fid-test-"))

import alerts  # noqa: E402
import config  # noqa: E402
import order_lifecycle as olc  # noqa: E402
import trust_derive  # noqa: E402
from rec_types import CheckStatus, FidelityCheck, FidelityDefect  # noqa: E402

NOW = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
SINCE = "2026-01-01T00:00:00Z"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _events(order_id, chain, ticker="AAPL", intent="roll_short", start=None):
    start = start or NOW - timedelta(hours=2)
    out, prior = [], None
    for i, new in enumerate(chain):
        out.append({"order_id": order_id, "ticker": ticker, "intent": intent,
                    "prior_state": prior, "new_state": new, "raw_status": new,
                    "seq": i + 1, "at": _iso(start + timedelta(minutes=i))})
        prior = new
    return out


def _state(events=(), receipts=(), execs=(), fidelity=None, recs=()):
    return {"metadata": {"trust_layer_since": SINCE},
            "executions": list(execs), "order_events": list(events),
            "order_receipts": list(receipts), "order_fidelity": dict(fidelity or {}),
            "recommendations": list(recs), "recommendation_overrides": [],
            "positions": [], "roll_ledger": {"rolls": []}}


def _roll_execs(gid="g1", live=True, mid=1.40, net=1.40):
    return [
        {"id": "e1", "ticker": "AAPL", "action": "close_short", "date": _iso(NOW),
         "roll_group_id": gid, "roll_reason": "scheduled", "contracts": 1,
         "close_price_per_share": 2.0, "live_transmitted": live,
         "roll_reference_net_mid": mid, "roll_net_fill": net},
        {"id": "e2", "ticker": "AAPL", "action": "sell_short", "date": _iso(NOW),
         "roll_group_id": gid, "roll_reason": "scheduled", "contracts": 1,
         "premium_per_share": 3.4, "live_transmitted": live,
         "roll_reference_net_mid": mid, "roll_net_fill": net},
    ]


def test_clean_two_leg_fill_passes_all_applicable_checks():
    ev = _events("o1", [olc.SUBMITTED, olc.WORKING, olc.FILLED])
    state = _state(events=ev, receipts=[{"order_id": "o1", "execution_ids": ["e1", "e2"]}],
                   execs=_roll_execs())
    fid = trust_derive.derive_order_fidelity(state, NOW)["o1"]
    checks = fid["checks"]
    assert checks[FidelityCheck.LIFECYCLE_LEGAL]["status"] == CheckStatus.PASS
    assert checks[FidelityCheck.NO_ORPHAN_LEG]["status"] == CheckStatus.PASS
    assert checks[FidelityCheck.SLIPPAGE_IN_BOUND]["status"] == CheckStatus.PASS
    assert checks[FidelityCheck.CANCEL_CONFIRMED_DEAD]["status"] == CheckStatus.NOT_APPLICABLE
    # NEVER a silent pass while the reconciliation layer doesn't exist:
    assert checks[FidelityCheck.RECONCILED_CLEAN]["status"] == CheckStatus.NOT_YET_IMPLEMENTED
    assert fid["pass"] is True and fid["paper"] is False


def test_fill_during_cancel_orphan_leg_fails_and_pages():
    ev = _events("o2", [olc.SUBMITTED, olc.WORKING, olc.CANCEL_REQUESTED,
                        olc.PENDING_CANCEL, olc.FILLED_DURING_CANCEL])
    # the race committed only ONE leg — a naked short
    state = _state(events=ev, receipts=[{"order_id": "o2", "execution_ids": ["e2"]}],
                   execs=_roll_execs()[1:])
    fid = trust_derive.derive_order_fidelity(state, NOW)["o2"]
    orphan = fid["checks"][FidelityCheck.NO_ORPHAN_LEG]
    assert orphan["status"] == CheckStatus.FAIL
    assert orphan["defect"] == FidelityDefect.ORPHAN_LEG
    # cancel WAS confirmed terminal (FILLED_DURING_CANCEL is terminal)
    assert fid["checks"][FidelityCheck.CANCEL_CONFIRMED_DEAD]["status"] == CheckStatus.PASS
    assert fid["pass"] is False
    # ... and the failure pages through the existing alert engine
    state["order_fidelity"] = {"o2": fid}
    fired = alerts.check_order_fidelity_fail(state)
    assert fired and fired[0]["type"] == "ORDER_FIDELITY_FAIL"
    assert "ORPHAN_LEG" in fired[0]["message"]


def test_cancel_requested_never_confirmed_dead_fails_after_deadline():
    ev = _events("o3", [olc.SUBMITTED, olc.WORKING, olc.CANCEL_REQUESTED,
                        olc.PENDING_CANCEL],
                 start=NOW - timedelta(hours=3))
    fid = trust_derive.derive_order_fidelity(_state(events=ev), NOW)["o3"]
    cancel = fid["checks"][FidelityCheck.CANCEL_CONFIRMED_DEAD]
    assert cancel["status"] == CheckStatus.FAIL
    assert cancel["defect"] == FidelityDefect.CANCEL_NOT_CONFIRMED_DEAD
    assert fid["pass"] is False


def test_cancel_pending_within_deadline_is_pending_not_fail():
    ev = _events("o3b", [olc.SUBMITTED, olc.WORKING, olc.CANCEL_REQUESTED],
                 start=NOW - timedelta(minutes=10))
    fid = trust_derive.derive_order_fidelity(_state(events=ev), NOW)["o3b"]
    assert fid["checks"][FidelityCheck.CANCEL_CONFIRMED_DEAD]["status"] == CheckStatus.PENDING
    assert fid["pass"] is None   # verdict not in yet — never graded pass early


def test_fill_outside_slippage_bound_fails():
    # reference net mid 1.40, realized net 1.20 -> adverse 14.3% > 5% bound
    ev = _events("o4", [olc.SUBMITTED, olc.WORKING, olc.FILLED])
    state = _state(events=ev, receipts=[{"order_id": "o4", "execution_ids": ["e1", "e2"]}],
                   execs=_roll_execs(mid=1.40, net=1.20))
    fid = trust_derive.derive_order_fidelity(state, NOW)["o4"]
    slip = fid["checks"][FidelityCheck.SLIPPAGE_IN_BOUND]
    assert slip["status"] == CheckStatus.FAIL
    assert slip["defect"] == FidelityDefect.SLIPPAGE_EXCEEDED
    assert fid["pass"] is False


def test_illegal_transition_fails_lifecycle_check():
    ev = _events("o5", [olc.SUBMITTED, olc.WORKING, olc.FILLED])
    ev.append({"order_id": "o5", "ticker": "AAPL", "intent": "roll_short",
               "prior_state": olc.FILLED, "new_state": olc.WORKING,
               "raw_status": "WORKING", "seq": 4, "at": _iso(NOW)})
    fid = trust_derive.derive_order_fidelity(_state(events=ev), NOW)["o5"]
    lc = fid["checks"][FidelityCheck.LIFECYCLE_LEGAL]
    assert lc["status"] == CheckStatus.FAIL
    assert lc["defect"] == FidelityDefect.ILLEGAL_TRANSITION


def test_locked_unknown_fails_lifecycle():
    ev = _events("o6", [olc.SUBMITTED, olc.WORKING, olc.CANCEL_REQUESTED,
                        olc.LOCKED_UNKNOWN])
    fid = trust_derive.derive_order_fidelity(_state(events=ev), NOW)["o6"]
    assert fid["checks"][FidelityCheck.LIFECYCLE_LEGAL]["defect"] == FidelityDefect.HARD_LOCKED


def test_paper_two_leg_ticket_flagged_paper_and_graded():
    state = _state(execs=_roll_execs(live=False))
    fid = trust_derive.derive_order_fidelity(state, NOW)["paper:g1"]
    assert fid["paper"] is True
    assert fid["checks"][FidelityCheck.NO_ORPHAN_LEG]["status"] == CheckStatus.PASS
    assert fid["checks"][FidelityCheck.LIFECYCLE_LEGAL]["status"] == CheckStatus.NOT_APPLICABLE
    assert fid["checks"][FidelityCheck.RECONCILED_CLEAN]["status"] == CheckStatus.NOT_YET_IMPLEMENTED
    assert fid["pass"] is True


def test_paper_single_leg_roll_group_is_orphan():
    state = _state(execs=_roll_execs(live=False)[:1])
    fid = trust_derive.derive_order_fidelity(state, NOW)["paper:g1"]
    assert fid["checks"][FidelityCheck.NO_ORPHAN_LEG]["status"] == CheckStatus.FAIL
    assert fid["pass"] is False


def test_graded_verdicts_survive_event_cap_rollover():
    ev = _events("old1", [olc.SUBMITTED, olc.WORKING, olc.FILLED])
    state = _state(events=ev, receipts=[{"order_id": "old1", "execution_ids": ["e1", "e2"]}],
                   execs=_roll_execs())
    first = trust_derive.derive_order_fidelity(state, NOW)
    assert "old1" in first
    # the events roll off the capped log; the graded verdict must be retained
    state2 = _state(events=_events("new1", [olc.SUBMITTED, olc.WORKING]),
                    fidelity=first)
    second = trust_derive.derive_order_fidelity(state2, NOW)
    assert "old1" in second and second["old1"]["pass"] is True
    assert "new1" in second


def test_executor_shaped_events_with_lock_derived_priors_pass():
    """Regression (v2.6.1): the executor stamps each event's prior_state from
    the per-intent LOCK — None for un-locked intents (rolls/exits), and shared
    across successive orders for locked ones. A clean lifecycle whose events
    carry None/foreign prior_state pointers must PASS: the grader reads the
    observed new_state sequence, never the recorded prior pointers."""
    ev = [
        {"order_id": "o8", "ticker": "XLK", "intent": "XLK:open_position_atomic",
         "prior_state": olc.SUBMITTED, "new_state": olc.WORKING,
         "raw_status": "SUBMITTED", "seq": 1, "at": _iso(NOW - timedelta(hours=2))},
        # settle event: prior_state None (lock lookup missed) — the exact shape
        # that produced the false ILLEGAL_TRANSITION pages at first deploy
        {"order_id": "o8", "ticker": "XLK", "intent": "XLK:open_position_atomic",
         "prior_state": None, "new_state": olc.FILLED,
         "raw_status": "FILLED", "seq": 2, "at": _iso(NOW - timedelta(hours=1))},
    ]
    state = _state(events=ev, receipts=[{"order_id": "o8", "execution_ids": ["e1", "e2"]}],
                   execs=_roll_execs())
    fid = trust_derive.derive_order_fidelity(state, NOW)["o8"]
    assert fid["checks"][FidelityCheck.LIFECYCLE_LEGAL]["status"] == CheckStatus.PASS
    assert fid["pass"] is True


def test_plain_fill_discovered_during_cancel_is_legal():
    """A fill that races the cancel but is discovered by the plain poll path
    records CANCEL_REQUESTED -> FILLED (not FILLED_DURING_CANCEL). Legal."""
    ev = _events("o9", [olc.SUBMITTED, olc.WORKING, olc.CANCEL_REQUESTED, olc.FILLED])
    fid = trust_derive.derive_order_fidelity(_state(events=ev), NOW)["o9"]
    assert fid["checks"][FidelityCheck.LIFECYCLE_LEGAL]["status"] == CheckStatus.PASS
    assert fid["checks"][FidelityCheck.CANCEL_CONFIRMED_DEAD]["status"] == CheckStatus.PASS


def test_pre_activation_lifecycles_graded_but_never_page_or_block():
    """Orders whose lifecycle ended BEFORE trust_layer_since are graded for the
    record, flagged pre_activation, excluded from paging and graduation — the
    deploy-time migration must not retroactively wake the operator."""
    old = datetime(2025, 11, 3, 15, 0, tzinfo=timezone.utc)   # before SINCE
    ev = _events("hist1", [olc.SUBMITTED, olc.WORKING, olc.FILLED], start=old)
    ev.append({"order_id": "hist1", "ticker": "XLK", "intent": "XLK:buy_leap",
               "prior_state": olc.FILLED, "new_state": olc.WORKING,   # genuinely illegal
               "raw_status": "WORKING", "seq": 4, "at": _iso(old + timedelta(minutes=5))})
    state = _state(events=ev)
    fid = trust_derive.derive_order_fidelity(state, NOW)["hist1"]
    assert fid["pass"] is False and fid["pre_activation"] is True
    state["order_fidelity"] = {"hist1": fid}
    assert alerts.check_order_fidelity_fail(state) == []   # never pages
    board = trust_derive.scoreboard(state, [], state["order_fidelity"], NOW)
    assert board["totals"]["fidelity_failures"] == 0
    assert board["totals"]["pre_activation_failures"] == 1
    grad = board["by_action_type"]["ENTER"]["graduation"]
    assert not any("fidelity failures" in f for f in grad["failing"])


def test_slippage_bound_from_source_recommendation_ticket():
    """A ticket staged from a recommendation grades against ITS OWN bound."""
    ev = _events("o7", [olc.SUBMITTED, olc.WORKING, olc.FILLED])
    execs = _roll_execs(mid=1.40, net=1.30)   # adverse ~7.1%
    execs[0]["source_rec_id"] = "rec_00009"
    rec = {"rec_id": "rec_00009", "action_type": "ROLL_OUT", "ticker": "AAPL",
           "proposed_ticket": {"max_slippage_pct_of_mid": 0.10}}
    state = _state(events=ev, receipts=[{"order_id": "o7", "execution_ids": ["e1", "e2"]}],
                   execs=execs, recs=[rec])
    fid = trust_derive.derive_order_fidelity(state, NOW)["o7"]
    # 7.1% adverse would fail the 5% default but passes the rec's 10% bound
    assert fid["checks"][FidelityCheck.SLIPPAGE_IN_BOUND]["status"] == CheckStatus.PASS
