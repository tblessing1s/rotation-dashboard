"""A short call's expiry is stamped on its immutable execution (not only on the
position mirror), and a called-away assignment is correctable from History like
any other share exit."""
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
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    return tmp_path


def _buy_write(ticker="TQQQ", strike=75.5, expiration="2026-10-02"):
    return executor.execute({"action": "buy_write", "ticker": ticker, "qty": 200,
                             "stock_price": 78.01, "price_per_share": 78.16, "strike": strike,
                             "expiration": expiration, "premium_per_share": 3.49,
                             "override_reason": "fixture"})


def _execs(action):
    return [e for e in log.load_state()["executions"] if e["action"] == action]


def test_a_buy_writes_call_records_its_expiry_on_the_execution(store):
    # LIVE: TQQQ's buy-write call booked with no expiry, so the log implied
    # "75.5C exp null" while the position showed exp 10/02.
    _buy_write()
    [sell] = _execs("sell_short")
    assert sell["expiration"] == "2026-10-02"
    legs = executor.replay_short_calls("TQQQ")
    assert [(l["strike"], l["contracts"], l["expiration"]) for l in legs] == [(75.5, 2, "2026-10-02")]


def test_a_buy_back_inherits_the_legs_expiry_when_not_given(store):
    _buy_write()
    executor.execute({"action": "close_short", "ticker": "TQQQ", "strike": 75.5, "contracts": 2,
                      "close_price_per_share": 3.0, "stock_price": 78.0})
    [close] = _execs("close_short")
    assert close["expiration"] == "2026-10-02"


def test_the_called_away_delivery_is_correctable_from_history(store):
    _buy_write(ticker="KO", strike=62.0)
    executor.execute({"action": "close_shares_assigned", "ticker": "KO", "strike": 62.0,
                      "contracts": 2, "stock_price": 63.0})
    [ca] = _execs("close_shares_assigned")
    executor.save_transactions([{"id": ca["id"], "cost_basis": 60.0, "date": "2026-09-25"}])
    fixed = next(e for e in log.derived_executions(log.load_state()) if e["id"] == ca["id"])
    assert fixed["cost_basis_per_share"] == 60.0
    assert fixed["realized_pnl"] == 400.0            # (62 - 60) x 200
    assert str(fixed["date"]).startswith("2026-09-25")
    # The delivery price is the strike — a stray price edit changes nothing.
    assert executor._compute_txn_changes(fixed, {"price": 70}) == {}
