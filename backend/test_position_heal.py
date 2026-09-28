"""position_heal — a position drifted from the trade log is rebuilt
automatically only when Schwab's last holdings agree with the log; otherwise it
is held for the operator (POSITION_DRIFT)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import alerts
import config
import executor
import logging_handler as log
import position_heal


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    return tmp_path


def _now_iso(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _broker(state, holdings, hours_ago=1):
    """A last successful reconciliation that saw ``holdings``."""
    state["reconciliation"] = {"last": {"as_of": _now_iso(hours_ago), "broker_ok": True,
                                        "status": "CLEAN", "diffs": [], "broker_view": holdings}}


def _shares(ticker, qty):
    return {"underlying": ticker, "instrument_type": "EQUITY", "put_call": None,
            "strike": None, "expiry": None, "quantity": qty}


def _call(ticker, strike, expiry, qty=-1):
    return {"underlying": ticker, "instrument_type": "OPTION", "put_call": "CALL",
            "strike": strike, "expiry": expiry, "quantity": qty}


def _book(execs, position, holdings=None, hours_ago=1):
    state = {"executions": execs, "positions": [position]}
    if holdings is not None:
        _broker(state, holdings, hours_ago)
    log.save_state(state)


def _ex(eid, action, date, **kw):
    e = {"id": eid, "ticker": "SPCX", "action": action, "date": date, "mode": "live",
         "live_transmitted": True}
    e.update(kw)
    return e


def _pos(count, calls):
    return {"ticker": "SPCX", "status": "active", "shares": {"count": count, "cap": 500},
            "short_calls": calls}


def test_voiding_a_wrong_buy_heals_the_shares_when_schwab_agrees(store):
    # LIVE shape: another account's buy in this book; Schwab holds no shares here.
    _book([_ex("exec_001", "buy_shares", "2026-09-02T13:49:42Z", qty=100, price_per_share=140.32)],
          _pos(100, []), holdings=[])
    executor.void_executions(["exec_001"], "another account's trade")
    p = log.find_position(log.load_state(), "SPCX")
    assert p["shares"]["count"] == 0
    assert any(e["action"] == "position_rebuild" for e in log.load_state()["executions"])
    assert position_heal.held(log.load_state()) == []


def test_drift_schwab_does_not_confirm_is_held_and_alerted(store):
    # Log implies 0 shares, the mirror says 100 — and Schwab HOLDS 100 (the log is
    # missing the buy). Rebuilding from the log would be wrong: hold it.
    _book([], _pos(100, []), holdings=[_shares("SPCX", 100)])
    rep = position_heal.heal()
    assert rep["healed"] == [] and [d["part"] for d in rep["held"]] == ["shares"]
    assert log.find_position(log.load_state(), "SPCX")["shares"]["count"] == 100
    a = alerts.check_position_drift(log.load_state())
    assert [x["type"] for x in a] == ["POSITION_DRIFT"] and "Schwab 100" in a[0]["message"]


@pytest.mark.parametrize("holdings,hours_ago", [(None, 1), ([], 100)])
def test_no_recent_schwab_holdings_means_hold(store, holdings, hours_ago):
    _book([], _pos(100, []), holdings=holdings, hours_ago=hours_ago)
    rep = position_heal.heal()
    assert rep["healed"] == [] and len(rep["held"]) == 1


def test_short_calls_heal_and_legs_without_an_expiry_pair(store):
    # The open never recorded its expiry, its buy-back did: they still pair, so
    # the log implies only the 142C — which is what Schwab holds.
    _book([
        _ex("o1", "sell_short", "2026-09-16T13:47:41Z", strike=138, contracts=1, premium_per_share=12.69),
        _ex("c1", "close_short", "2026-09-22T13:52:01Z", strike=138, contracts=1,
            expiration="2026-09-25", close_price_per_share=15.39),
        _ex("o2", "sell_short", "2026-09-22T13:52:01Z", strike=142, contracts=1,
            expiration="2026-10-02", premium_per_share=12.74),
    ], _pos(0, [{"strike": 144, "contracts": 1}, {"strike": 142, "contracts": 1,
                                                  "expiration": "2026-10-02"}]),
        holdings=[_call("SPCX", 142.0, "2026-10-02")])
    assert [(l["strike"]) for l in executor.replay_short_calls("SPCX")] == [142]
    position_heal.heal()
    calls = log.find_position(log.load_state(), "SPCX")["short_calls"]
    assert [(c["strike"], c["contracts"]) for c in calls] == [(142, 1)]


def test_in_sync_positions_are_left_alone(store):
    _book([_ex("b1", "buy_shares", "2026-09-01T15:40:48Z", qty=100, price_per_share=144.09)],
          _pos(100, []), holdings=[_shares("SPCX", 100)])
    assert position_heal.heal() == {"healed": [], "held": []}
    assert not any(e["action"] == "position_rebuild" for e in log.load_state()["executions"])


def test_undoing_an_unfilled_roll_never_restores_the_old_call_twice(store):
    # Schwab still holds the OLD 138C (the roll never filled, not assigned).
    executor.execute({"action": "buy_write", "ticker": "AAA", "qty": 100, "stock_price": 140.0,
                      "price_per_share": 140.0, "strike": 138, "expiration": "2026-09-25",
                      "premium_per_share": 4.0, "override_reason": "fixture"})
    res = executor.execute({"action": "roll_short", "ticker": "AAA", "contracts": 1,
                            "from_strike": 138, "close_price_per_share": 3.0, "to_strike": 141,
                            "premium_per_share": 3.5, "to_expiration": "2026-10-02",
                            "stock_price": 140.5})
    log.mutate_state(lambda s: _broker(s, [_shares("AAA", 100), _call("AAA", 138.0, "2026-09-25")]))
    executor.undo_unfilled_roll("AAA", res["roll_group_id"], "never filled",
                                from_expiration="2026-09-25")
    calls = log.find_position(log.load_state(), "AAA")["short_calls"]
    assert [(c["strike"], c["contracts"]) for c in calls] == [(138, 1)]


def test_reconciliation_keeps_schwabs_holdings_on_its_report(store, monkeypatch):
    import reconcile
    import schwab_api
    log.save_state({"executions": [], "positions": []})
    monkeypatch.setattr(reconcile, "data_handler_client_accounts", lambda: [
        {"securitiesAccount": {"accountNumber": "1", "positions": [
            {"longQuantity": 100, "instrument": {"assetType": "EQUITY", "symbol": "SPCX"}}]}}])
    monkeypatch.setattr(schwab_api, "bound_account_number", lambda: None)
    reconcile.run_reconciliation()
    view = log.load_state()["reconciliation"]["last"]["broker_view"]
    assert view == [{"underlying": "SPCX", "instrument_type": "EQUITY", "put_call": None,
                     "strike": None, "expiry": None, "quantity": 100.0}]
