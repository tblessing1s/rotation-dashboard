"""A roll booked in the app that never filled at Schwab — the old call was still
open and got assigned. undo_unfilled_roll takes the booked roll back off (void
both legs, restore the old short) and books the old call's assignment on the
day it actually happened."""
from __future__ import annotations

import pytest

import config
import executor
import logging_handler as log
import reconcile


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)   # paper / logged
    return tmp_path


def _covered_then_paper_roll():
    executor.execute({"action": "buy_write", "ticker": "AAA", "qty": 100, "stock_price": 140.32,
                      "price_per_share": 140.32, "strike": 138, "expiration": "2026-09-25",
                      "premium_per_share": 4.0, "override_reason": "fixture"})
    return executor.execute({
        "action": "roll_short", "ticker": "AAA", "contracts": 1,
        "from_strike": 138, "close_price_per_share": 3.0,
        "to_strike": 141, "premium_per_share": 3.5, "to_expiration": "2026-10-02",
        "to_dte": 7, "stock_price": 140.5})


def _live(execs):
    return [e for e in execs if not e.get("excluded")]


def test_the_paper_roll_is_offered_as_unfilled(store):
    res = _covered_then_paper_roll()
    rolls = executor.find_unfilled_rolls("AAA")
    assert len(rolls) == 1
    r = rolls[0]
    assert r["roll_id"] == res["roll_group_id"]
    # The log never stamped the old call's expiry on these booking paths.
    assert (r["from_strike"], r["from_expiration"]) == (138, None)
    assert (r["to_strike"], r["to_expiration"]) == (141, "2026-10-02")


def test_undo_and_book_the_assignment(store):
    res = _covered_then_paper_roll()
    out = executor.undo_unfilled_roll("AAA", res["roll_group_id"], "order never filled",
                                      assigned=True, from_expiration="2026-09-25")
    assert out["status"] == "undone" and out["restored_expiration"] == "2026-09-25"

    st = log.load_state()
    execs = st["executions"]
    # Both roll legs voided (still on the immutable log), with the reason.
    rolled = [e for e in execs if e.get("roll_id") == res["roll_group_id"]]
    assert len(rolled) == 2 and all(e["excluded"] for e in rolled)
    assert all("order never filled" in e["void_reason"] for e in rolled)

    # The OLD 138 call was assigned and the shares delivered at 138, dated the
    # old call's expiration so the payout buckets into that week.
    assigned = [e for e in _live(execs) if e.get("assigned") or e["action"] == "close_shares_assigned"]
    assert [e["action"] for e in assigned] == ["close_short", "close_shares_assigned"]
    assert all(e["strike"] == 138 for e in assigned)
    assert all(e["date"].startswith("2026-09-25") for e in assigned)
    assert assigned[1]["realized_pnl"] == pytest.approx((138 - 140.32) * 100)

    p = log.find_position(st, "AAA")
    assert p["short_calls"] == [] and p["shares"]["count"] == 0
    assert p["status"] == "closed"
    # Nothing left of the phantom roll in the derived roll ledger.
    assert (st.get("roll_ledger") or {}).get("by_ticker", {}).get("AAA", {}).get("count", 0) == 0
    assert executor.find_unfilled_rolls("AAA") == []


def test_undo_without_assignment_restores_the_old_call(store):
    res = _covered_then_paper_roll()
    executor.undo_unfilled_roll("AAA", res["roll_group_id"], "order never filled",
                                from_expiration="2026-09-25")
    p = log.find_position(log.load_state(), "AAA")
    assert [(sc["strike"], sc["expiration"]) for sc in p["short_calls"]] == [(138, "2026-09-25")]
    assert p["shares"]["count"] == 100


def test_assignment_defaults_to_the_day_the_roll_was_booked(store):
    res = _covered_then_paper_roll()
    booked = next(e for e in log.load_state()["executions"] if e.get("roll_id"))["date"][:10]
    executor.undo_unfilled_roll("AAA", res["roll_group_id"], "never filled", assigned=True)
    ex = [e for e in log.load_state()["executions"] if e["action"] == "close_shares_assigned"]
    assert ex[0]["date"].startswith(booked)


def test_resolves_the_reconciliation_diffs(store):
    res = _covered_then_paper_roll()

    def _report(s):
        s["reconciliation"] = {"last": {"as_of": "2026-09-28T12:00:00Z", "diffs": [
            {"id": "diff_001", "ticker": "AAA", "instrument_type": "OPTION",
             "classification": "MISSING_AT_BROKER", "summary": "141 call missing"},
            {"id": "diff_002", "ticker": "AAA", "instrument_type": "EQUITY",
             "classification": "MISSING_AT_BROKER", "summary": "shares missing"},
        ]}}
        reconcile.reevaluate_freezes(s)
    log.mutate_state(_report)
    assert log.find_position(log.load_state(), "AAA")["needs_review"]

    executor.undo_unfilled_roll("AAA", res["roll_group_id"], "never filled", assigned=True,
                                diff_ids=["diff_001", "diff_002"])
    st = log.load_state()
    diffs = st["reconciliation"]["last"]["diffs"]
    assert all(d["resolution"]["status"] == "resolved" for d in diffs)
    assert not log.find_position(st, "AAA").get("needs_review")


def test_equity_diff_stays_open_when_not_assigned(store):
    res = _covered_then_paper_roll()
    log.mutate_state(lambda s: s.update({"reconciliation": {"last": {"diffs": [
        {"id": "diff_002", "ticker": "AAA", "instrument_type": "EQUITY",
         "classification": "MISSING_AT_BROKER", "summary": "shares missing"}]}}}))
    executor.undo_unfilled_roll("AAA", res["roll_group_id"], "never filled", diff_ids=["diff_002"])
    d = log.load_state()["reconciliation"]["last"]["diffs"][0]
    assert not d.get("resolution")


def test_a_broker_filled_roll_is_never_undone(store):
    res = _covered_then_paper_roll()

    def _as_filled(s):
        for e in s["executions"]:
            if e.get("roll_id") == res["roll_group_id"]:
                e["roll_alloc_method"] = "broker_per_leg"
    log.mutate_state(_as_filled)
    assert executor.find_unfilled_rolls("AAA") == []
    with pytest.raises(ValueError, match="filled by Schwab"):
        executor.undo_unfilled_roll("AAA", res["roll_group_id"], "x")


def test_a_reason_is_required(store):
    res = _covered_then_paper_roll()
    with pytest.raises(ValueError, match="typed reason"):
        executor.undo_unfilled_roll("AAA", res["roll_group_id"], "  ")
