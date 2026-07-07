"""Multi-tranche LEAPs: one ticker holds several LEAP legs keyed by
(strike, expiration). Same-contract adds MERGE into the leg (scale-in);
different strike/expiration APPENDS a new leg; closes remove exactly the
identified leg and the position stays active until the last leg is gone.
The extrinsic-payback cycle is continuous across adds (target grows, juice
carries) — the same continuity rule LEAP rolls already use."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-multileap-test-"))

import config            # noqa: E402
import executor          # noqa: E402
import logging_handler as log  # noqa: E402
import position_manager  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    return tmp_path


def _buy(strike=140.0, contracts=5, price=5000.0, exp="2027-01-15", **over):
    p = {"action": "buy_leap", "ticker": "XLK", "strike": strike,
         "contracts": contracts, "execution_price": price, "expiration": exp,
         "dte": 190, "stock_price": 184.0, "override_reason": "test"}
    p.update(over)
    return executor.execute(p)


def _close(strike=140.0, contracts=5, price=5500.0, exp=None, **over):
    p = {"action": "close_leap", "ticker": "XLK", "strike": strike,
         "contracts": contracts, "close_price": price, "stock_price": 190.0}
    if exp:
        p["expiration"] = exp
    p.update(over)
    return executor.execute(p)


def test_same_strike_same_expiration_merges(store):
    _buy(contracts=5, price=5000)          # extrinsic 600/contract -> 3000
    _buy(contracts=3, price=5100)          # extrinsic 700/contract -> 2100
    state = log.load_state()
    pos = log.find_position(state, "XLK")
    assert len(pos["leap_legs"]) == 1
    leg = pos["leap_legs"][0]
    assert leg["contracts"] == 8
    assert leg["cost_basis"] == pytest.approx(5 * 5000 + 3 * 5100)
    assert leg["extrinsic_at_entry"] == pytest.approx(3000 + 2100)
    assert pos["leap"] is pos["leap_legs"][0]  # alias intact in-session

    buys = [e for e in state["executions"] if e["action"] == "buy_leap"]
    assert "leap_add" not in buys[0] and buys[1]["leap_add"] == "merge"
    # payback target = both buys' extrinsic; the cycle never reset
    pb = state["extrinsic_payback"]["XLK"]
    assert pb["leap_extrinsic_at_entry"] == pytest.approx(5100)


def test_different_strike_or_expiration_appends_leg(store):
    _buy(strike=140, contracts=5, price=5000)
    _buy(strike=150, contracts=2, price=4200, exp="2027-06-18")
    _buy(strike=140, contracts=1, price=5050, exp="2028-01-21")  # same strike, new expiry
    state = log.load_state()
    pos = log.find_position(state, "XLK")
    assert len(pos["leap_legs"]) == 3
    assert pos["leap"]["strike"] == 140 and pos["leap"]["expiration"] == "2027-01-15"
    buys = [e for e in state["executions"] if e["action"] == "buy_leap"]
    assert buys[1]["leap_add"] == "add" and buys[2]["leap_add"] == "add"
    # deployed capital counts every leg
    assert position_manager.position_capital(pos) == pytest.approx(25000 + 8400 + 5050)


def test_close_one_leg_keeps_position_active(store):
    _buy(strike=140, contracts=5, price=5000)
    _buy(strike=150, contracts=2, price=4200, exp="2027-06-18")
    res = _close(strike=140, contracts=5, price=5500)
    state = log.load_state()
    pos = log.find_position(state, "XLK")
    assert pos["status"] == "active"
    assert len(pos["leap_legs"]) == 1 and pos["leap"]["strike"] == 150
    close = [e for e in state["executions"] if e["action"] == "close_leap"][0]
    assert close["legs_remaining"] == 1
    # realized P&L keyed to the CLOSED leg's cost basis, not the survivor's
    assert close["cost_basis"] == pytest.approx(25000)
    assert res["execution"]["realized_pnl"] == pytest.approx(5 * 5500 - 25000)
    # cycle survives: payback target still includes both legs' entry extrinsic
    assert state["extrinsic_payback"]["XLK"]["leap_extrinsic_at_entry"] > 0

    _close(strike=150, contracts=2, price=4400, exp="2027-06-18")
    pos = log.find_position(log.load_state(), "XLK")
    assert pos["status"] == "closed" and pos["leap"] is None and pos["leap_legs"] == []


def test_juice_carries_across_an_add(store):
    _buy(strike=140, contracts=5, price=5000)
    executor.execute({"action": "sell_short", "ticker": "XLK", "strike": 181,
                      "contracts": 5, "premium_per_share": 5.40, "stock_price": 184.0,
                      "expiration": "2026-07-10", "override_reason": "test"})
    executor.execute({"action": "close_short", "ticker": "XLK", "strike": 181,
                      "contracts": 5, "close_price_per_share": 4.00, "stock_price": 183.0})
    banked = log.load_state()["extrinsic_payback"]["XLK"]["collected_to_date"]
    assert banked > 0

    _buy(strike=150, contracts=2, price=4200, exp="2027-06-18")
    pb = log.load_state()["extrinsic_payback"]["XLK"]
    assert pb["collected_to_date"] == pytest.approx(banked)  # juice carried
    assert pb["leap_extrinsic_at_entry"] == pytest.approx(3000 + 2 * 800)


def test_enrichment_emits_legs_and_totals(store):
    _buy(strike=140, contracts=5, price=5000)
    _buy(strike=150, contracts=2, price=4200, exp="2027-06-18")
    state = log.load_state()
    view = position_manager.positions_view(state)
    p = next(v for v in view if v["ticker"] == "XLK")
    assert len(p["leap_legs"]) == 2
    t = p["leap_totals"]
    assert t["legs"] == 2 and t["contracts"] == 7
    assert t["cost_basis"] == pytest.approx(25000 + 8400)
    assert p["leap_health_agg"]["legs"] == 2


def test_legacy_single_leap_state_migrates(store):
    _buy(strike=140, contracts=5, price=5000)
    # Simulate a pre-v10 file: strip the legs list, leave only the single leap.
    state = log.load_state()
    pos = log.find_position(state, "XLK")
    pos.pop("leap_legs", None)
    log.save_state(state)
    reloaded = log.load_state()
    pos = log.find_position(reloaded, "XLK")
    assert pos["leap_legs"] and pos["leap"] is pos["leap_legs"][0]
