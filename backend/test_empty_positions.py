"""Positions that hold nothing.

Reported from production: a SPCX row on the Positions tab showing "No shares
held", no calls, no puts — a position record for an order that was never sent to
the broker. It reads exactly like something the operator owns, which is the whole
problem: a book you cannot trust is worse than no book.

`_ensure_position` creates the position shell BEFORE the mutation that books a
leg runs, so any path that creates one and then books nothing (an order the
broker refused, a booking that raised) leaves a shell behind. `_close_if_empty`
already encoded the rule — a position holding nothing is not active — but was
called only on the put-close path.
"""
from __future__ import annotations

import pytest

import config
import executor
import logging_handler as log


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _seed(**legs):
    state = log.load_state()
    position = {"ticker": "AAA", "status": "active",
                "shares": {"count": 0, "cost_basis_per_share": None},
                "short_calls": [], "short_puts": [], "leap_legs": [], "leap": None}
    position.update(legs)
    state.setdefault("positions", []).append(position)
    log.save_state(state)
    return position


# ---------------------------------------------------------------------------
# SAFETY: it must be unable to remove anything real
# ---------------------------------------------------------------------------
def test_a_position_holding_shares_is_never_cleared(store):
    """The load-bearing safety property. However this is invoked, it must not be
    able to retire a real holding."""
    _seed(shares={"count": 100, "cost_basis_per_share": 50.0})
    assert executor.close_empty_positions()["cleared"] == []
    assert log.find_position(log.load_state(), "AAA")["status"] == "active"


def test_a_position_holding_only_a_short_call_is_never_cleared(store):
    _seed(short_calls=[{"strike": 55.0, "contracts": 1}])
    assert executor.close_empty_positions()["cleared"] == []


def test_a_position_holding_only_a_short_put_is_never_cleared(store):
    """The put is the newest of the four legs and was the term the pre-v22
    emptiness test could not see."""
    _seed(short_puts=[{"strike": 45.0, "contracts": 1}])
    assert executor.close_empty_positions()["cleared"] == []


def test_a_position_holding_only_a_leap_is_never_cleared(store):
    _seed(leap_legs=[{"strike": 30.0, "contracts": 1}],
          leap={"strike": 30.0, "contracts": 1})
    assert executor.close_empty_positions()["cleared"] == []


# ---------------------------------------------------------------------------
# The shell itself
# ---------------------------------------------------------------------------
def test_an_empty_shell_is_cleared(store):
    _seed()
    out = executor.close_empty_positions("never sent to the broker")
    assert out["cleared"] == ["AAA"] and out["count"] == 1
    assert log.find_position(log.load_state(), "AAA")["status"] == "closed"


def test_clearing_leaves_an_immutable_marker(store):
    """The execution log is append-only. Retiring a row is itself a recorded
    event, with the operator's reason, or the book loses its audit trail."""
    _seed()
    executor.close_empty_positions("order was never sent")
    marks = [e for e in log.load_state()["executions"]
             if e.get("action") == "position_cleared"]
    assert len(marks) == 1
    assert marks[0]["ticker"] == "AAA"
    assert "never sent" in marks[0]["reason"]


def test_clearing_is_idempotent(store):
    """A second sweep must not append a second marker for the same row."""
    _seed()
    executor.close_empty_positions()
    assert executor.close_empty_positions()["cleared"] == []
    marks = [e for e in log.load_state()["executions"]
             if e.get("action") == "position_cleared"]
    assert len(marks) == 1


def test_an_already_closed_position_is_left_alone(store):
    _seed(status="closed")
    assert executor.close_empty_positions()["cleared"] == []


def test_it_clears_only_the_empty_ones(store):
    """Mixed book: the real holding survives, the shell does not."""
    _seed(shares={"count": 100, "cost_basis_per_share": 50.0})
    state = log.load_state()
    state["positions"].append(
        {"ticker": "ZZZ", "status": "active",
         "shares": {"count": 0}, "short_calls": [], "short_puts": [],
         "leap_legs": [], "leap": None})
    log.save_state(state)
    assert executor.close_empty_positions()["cleared"] == ["ZZZ"]
    assert log.find_position(log.load_state(), "AAA")["status"] == "active"


# ---------------------------------------------------------------------------
# It must not recur
# ---------------------------------------------------------------------------
def test_every_commit_closes_a_position_left_empty(store, monkeypatch):
    """The root fix. A shell created faster than it can be swept is still a shell
    on the operator's book, so the invariant is enforced at every commit rather
    than only by an operator-run sweep."""
    import inspect
    src = inspect.getsource(executor._commit)
    assert "_close_if_empty(position)" in src, (
        "_commit must self-heal, or a refused order leaves a shell behind again")


def test_the_route_is_wired(store):
    import app as app_module
    with app_module.app.test_request_context():
        assert app_module.app.url_map.bind("x").match(
            "/api/positions/close-empty", method="POST")[0] == "api_close_empty_positions"
