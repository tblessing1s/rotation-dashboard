"""One-click booking of a covered call that was assigned (shares called away)."""
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
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    return tmp_path


def _tqqq():
    # 200 shares at 78.16, two 76.5 calls expiring 10/09 (the position from the report).
    executor.execute({"action": "buy_write", "ticker": "TQQQ", "qty": 200, "stock_price": 78.01,
                      "price_per_share": 78.16, "strike": 76.5, "expiration": "2026-10-09",
                      "premium_per_share": 0.39, "override_reason": "fixture"})


def _report(equity_cls="MISSING_AT_BROKER", broker_qty=None, with_call=True):
    diffs = [{"id": "diff_001", "stable_id": "s-eq", "classification": equity_cls,
              "ticker": "TQQQ", "instrument_type": reconcile.EQUITY, "put_call": None,
              "strike": None, "expiry": None, "expected_qty": 200, "broker_qty": broker_qty,
              "summary": "TQQQ shares: state expects 200, broker does not hold them."}]
    if with_call:
        diffs.append({"id": "diff_002", "stable_id": "s-call", "classification": "MISSING_AT_BROKER",
                      "ticker": "TQQQ", "instrument_type": reconcile.OPTION,
                      "put_call": reconcile.CALL, "strike": 76.5, "expiry": "2026-10-09",
                      "expected_qty": -2, "broker_qty": None,
                      "summary": "TQQQ 76.5 call: state expects -2, broker does not hold it."})
    st = log.load_state()
    st["reconciliation"] = {"last": {"as_of": "2026-10-10T12:00:00Z", "status": "DIVERGED",
                                     "broker_ok": True, "diffs": diffs}, "history": []}
    log.save_state(st)


def _execs(action):
    return [e for e in log.load_state()["executions"] if e["action"] == action]


def test_books_the_called_away_and_closes_the_position(store):
    _tqqq()
    _report()
    out = executor.record_called_away("diff_001")
    assert out["success"] and out["contracts"] == 2 and out["shares"] == 200 and out["strike"] == 76.5
    [ca] = _execs("close_shares_assigned")
    assert ca["qty"] == 200 and ca["strike"] == 76.5 and str(ca["date"]).startswith("2026-10-09")
    assert ca["realized_pnl"] == pytest.approx((76.5 - 78.16) * 200)        # -332
    assert ca["exit_reason"] == "CALLED_AWAY"
    [cs] = [e for e in _execs("close_short") if e.get("assigned")]
    assert cs["strike"] == 76.5 and cs["contracts"] == 2
    p = log.find_position(log.load_state(), "TQQQ")
    assert p["status"] == "closed" and not p["short_calls"] and not p["shares"].get("count")


def test_clears_both_diffs_and_lifts_the_freeze(store):
    _tqqq()
    _report()
    executor.record_called_away("diff_001")
    diffs = {d["id"]: d for d in log.load_state()["reconciliation"]["last"]["diffs"]}
    assert diffs["diff_001"]["resolution"]["status"] == "resolved"
    assert diffs["diff_002"]["resolution"]["how"] == "record_called_away"
    assert not (log.find_position(log.load_state(), "TQQQ") or {}).get("needs_review")


def test_a_partial_share_reduction_books_just_those_lots(store):
    _tqqq()
    _report(equity_cls="QUANTITY_MISMATCH", broker_qty=100)
    out = executor.record_called_away("diff_001")
    assert out["contracts"] == 1 and out["shares"] == 100
    assert _execs("close_shares_assigned")[0]["qty"] == 100


def test_refuses_a_diff_that_is_not_a_share_reduction(store):
    _tqqq()
    _report()
    with pytest.raises(ValueError, match="share-reduction"):
        executor.record_called_away("diff_002")                  # the option diff
    with pytest.raises(ValueError, match="unknown diff"):
        executor.record_called_away("diff_999")
    assert not _execs("close_shares_assigned")


def test_refuses_a_non_lot_share_change(store):
    _tqqq()
    _report(equity_cls="QUANTITY_MISMATCH", broker_qty=150)
    with pytest.raises(ValueError, match="whole number"):
        executor.record_called_away("diff_001")


def test_falls_back_to_the_positions_only_short_when_the_call_diff_is_absent(store):
    _tqqq()
    _report(with_call=False)
    assert executor.record_called_away("diff_001")["strike"] == 76.5


def test_route_books_it(store):
    import app as app_module
    _tqqq()
    _report()
    c = app_module.create_app().test_client()
    assert c.post("/api/reconcile/record-called-away", json={}).status_code == 400
    r = c.post("/api/reconcile/record-called-away", json={"diff_id": "diff_001"})
    assert r.status_code == 200 and r.get_json()["status"] == "resolved"
    again = c.post("/api/reconcile/record-called-away", json={"diff_id": "diff_001"})
    assert again.status_code == 400 and "already" in again.get_json()["error"]
    assert len(_execs("close_shares_assigned")) == 1                # never booked twice


def test_refuses_when_the_book_holds_fewer_shares_than_left(store):
    _tqqq()
    st = log.load_state()
    log.find_position(st, "TQQQ")["shares"]["count"] = 100       # book already short of the report
    log.save_state(st)
    _report()
    with pytest.raises(ValueError, match="holds only 100"):
        executor.record_called_away("diff_001")
    assert not _execs("close_shares_assigned")
