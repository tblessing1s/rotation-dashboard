"""Phase-1 roll-dialog audit, §1.6 — ROLL_STRIKE_CHOICE (TRAVIS_EXTENSION,
telemetry-only) must never reach the theta/accrual ledgers. Those ledgers are
derived purely from each close_short execution's net_juice/net_juice_total
(extrinsic sold − extrinsic paid back, see executor._close_short and
logging_handler.recompute_derived) — never from roll_strike_choice's own
numbers, and never from raw premium/close cash totals."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-roll-ledger-test-"))

import config            # noqa: E402
import executor          # noqa: E402
import logging_handler as log  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _seed_open_short(ticker="PG", strike=100.0, entry_extrinsic=2.0, contracts=1):
    state = log.load_state()
    state.setdefault("positions", []).append({
        "ticker": ticker, "status": "open", "position_type": "SHARES",
        "shares": {"count": 100, "cost_basis_per_share": 100.0},
        "short_calls": [{
            "strike": strike, "contracts": contracts, "open_date": "2026-08-25",
            "expiration": "2026-09-11", "dte": 8,
            "entry_extrinsic_per_share": entry_extrinsic,
            "entry_premium_total": entry_extrinsic * contracts * 100,
            "current_bid": entry_extrinsic, "current_cost": entry_extrinsic * contracts * 100,
        }],
    })
    log.save_state(state)


def test_roll_strike_choice_never_reaches_the_ledger(store):
    _seed_open_short(entry_extrinsic=2.0)  # sold $2.00/sh extrinsic (ATM at entry)

    # An unmistakably WRONG number (99999.99) that would corrupt the ledger
    # instantly if roll_strike_choice ever leaked into it.
    choice = {
        "regime": "yellow", "regime_target_strike": 103.5, "floor_strike": 108.0,
        "chosen_strike": 105.0, "juice_per_week_at_chosen": 99999.99,
        "cushion_atr_at_chosen": 1.2,
    }
    payload = {
        "from_strike": 100.0, "to_strike": 105.0,
        "close_price_per_share": 1.20,   # buyback: intrinsic 1.00 (101-100), extrinsic 0.20
        "premium_per_share": 1.50,       # new sale: intrinsic 0.00 (101<105), extrinsic 1.50
        "to_expiration": "2026-09-18", "to_dte": 15,
        "roll_strike_choice": choice,
    }
    executor._commit_roll(payload, "PG", 1, 101.0, "logged", "test")

    state = log.load_state()
    sell_execs = [e for e in state["executions"] if e.get("action") == "sell_short"]
    close_execs = [e for e in state["executions"] if e.get("action") == "close_short"]
    assert sell_execs and close_execs

    # Stored verbatim on the OPEN leg's execution — additive, not authoritative.
    assert sell_execs[-1]["roll_strike_choice"] == choice

    close_exec = close_execs[-1]
    assert close_exec["extrinsic_sold"] == pytest.approx(2.0)
    assert close_exec["extrinsic_paid_back"] == pytest.approx(0.20)
    expected_net_juice_total = round((2.0 - 0.20) * 1 * 100, 2)  # $180.00
    assert close_exec["net_juice_total"] == pytest.approx(expected_net_juice_total)

    # The ledger reflects EXACTLY the net_juice figure — a fresh, isolated store
    # with only this one roll in it, so YTD must equal it exactly. Not
    # roll_strike_choice's 99999.99, and not the raw premium/close cash either
    # (premium_total=150, close_total=120 — a cash leak would show ~30 or ~150,
    # neither of which is 180).
    ytd = state["theta_ledger"]["totals"]["ytd"]
    assert ytd == pytest.approx(expected_net_juice_total)
    assert "99999" not in repr(state["theta_ledger"])
    assert "99999" not in repr(state["accrual_ledger"])

    # And explicitly: nothing in recompute_derived's inputs is keyed off
    # roll_strike_choice — mutating/removing it must not change the ledger at all.
    state2 = log.load_state()
    for e in state2["executions"]:
        e.pop("roll_strike_choice", None)
    log.recompute_derived(state2)
    assert state2["theta_ledger"]["totals"]["ytd"] == pytest.approx(expected_net_juice_total)
