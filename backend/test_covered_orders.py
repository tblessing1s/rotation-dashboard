"""Covered one-ticket orders — buy_write (shares + call) and unwind_covered (call
buy-back + share sale) on ONE net order, so the shares are never held without
their call between two fills. The entry/exit siblings of the atomic roll."""
from __future__ import annotations

import pytest

import config
import coverage_gaps
import executor
import exit_reasons
import logging_handler as log
import position_manager
import schwab_api

SYM = "AAA   260925C00048000"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)   # paper unless `live`
    return tmp_path


def _buy_write(**kw):
    p = {"action": "buy_write", "ticker": "AAA", "qty": 100, "stock_price": 50.0,
         "price_per_share": 50.0, "strike": 48, "expiration": "2026-09-25",
         "premium_per_share": 2.6, "override_reason": "covered order path test"}
    p.update(kw)
    return executor.execute(p)


def _covered_execs():
    return [e for e in log.load_state()["executions"] if e.get("covered_group_id")]


# ---------------------------------------------------------------------------
# Paper / logged: both legs book together
# ---------------------------------------------------------------------------
def test_buy_write_books_shares_and_call_as_one_moment(store):
    res = _buy_write()
    assert res["status"] == "filled" and res["mode"] == "logged"
    legs = _covered_execs()
    assert [e["action"] for e in legs] == ["buy_shares", "sell_short"]
    assert len({e["covered_group_id"] for e in legs}) == 1
    assert len({e["date"] for e in legs}) == 1
    assert res["net_per_share"] == -47.4          # 50.00 stock - 2.60 premium, a debit
    p = log.find_position(log.load_state(), "AAA")
    assert p["shares"]["count"] == 100 and p["short_calls"][0]["strike"] == 48
    assert coverage_gaps.for_ticker(log.load_state(), "AAA")["windows"] == []


def test_unwind_buys_back_the_call_and_sells_the_shares_together(store):
    _buy_write()
    res = executor.execute({"action": "unwind_covered", "ticker": "AAA",
                            "close_prices": {"48": 3.1}, "price_per_share": 51.0,
                            "stock_price": 51.0,
                            "exit_reason": exit_reasons.ExitReason.CB_DRAWDOWN_15})
    assert res["status"] == "filled"
    legs = [e for e in _covered_execs() if e["covered_group_id"] == res["covered_group_id"]]
    assert [e["action"] for e in legs] == ["close_short", "sell_shares"]
    assert len({e["date"] for e in legs}) == 1
    assert res["net_per_share"] == 47.9           # 51.00 stock - 3.10 buy-back, a credit
    st = log.load_state()
    assert log.find_position(st, "AAA")["status"] == "closed"
    assert coverage_gaps.for_ticker(st, "AAA")["windows"] == []


def test_unwind_refuses_shares_that_do_not_match_the_calls(store):
    _buy_write(qty=200, contracts=None)
    # 200 shares, 2 calls -> matched; trim a call's worth of shares first to mismatch.
    executor.execute({"action": "close_short", "ticker": "AAA", "strike": 48,
                      "contracts": 1, "close_price_per_share": 3.0, "stock_price": 50.0})
    with pytest.raises(ValueError, match="100 shares per open call"):
        executor.execute({"action": "unwind_covered", "ticker": "AAA",
                          "price_per_share": 50.0, "stock_price": 50.0,
                          "exit_reason": exit_reasons.ExitReason.CB_DRAWDOWN_15})


def test_buy_write_keeps_the_round_lot_rule(store):
    with pytest.raises(ValueError, match="positive multiple of 100"):
        _buy_write(qty=150)


def test_covered_actions_are_non_transmitting_while_equity_placement_is_off(monkeypatch):
    monkeypatch.setattr(config, "EQUITY_ORDER_PLACEMENT_ENABLED", False)
    assert {"buy_write", "unwind_covered"} <= executor.non_transmitting_actions()
    monkeypatch.setattr(config, "EQUITY_ORDER_PLACEMENT_ENABLED", True)
    assert executor.non_transmitting_actions() == frozenset()


# ---------------------------------------------------------------------------
# exit_position routes a matched position through ONE ticket
# ---------------------------------------------------------------------------
def test_exit_position_falls_back_to_legs_when_shares_exceed_calls(store, monkeypatch):
    _buy_write()
    executor.execute({"action": "buy_shares", "ticker": "AAA", "qty": 100, "stock_price": 50.0,
                      "price_per_share": 50.0, "override_reason": "t"})
    monkeypatch.setattr(position_manager, "_stock_price", lambda t: 49.0)
    res = executor.exit_position("AAA", exit_reason=exit_reasons.ExitReason.CB_DRAWDOWN_15)
    assert res["ok"] is True
    assert [s["leg"] for s in res["steps"]] == ["close_short", "sell_shares"]


# ---------------------------------------------------------------------------
# Live: ONE previewed net order, committed on the fill
# ---------------------------------------------------------------------------
class _Client:
    def __init__(self, preview_ok=True):
        self.preview_ok = preview_ok
        self.previewed, self.placed = [], []
        self.order = None

    def primary_account_hash(self):
        return "HASH"

    def get_quotes(self, symbols):
        return {SYM: {"bid": 2.50, "ask": 2.70}}

    def preview_order(self, account_hash, order):
        self.previewed.append(order)
        if not self.preview_ok:
            raise schwab_api.SchwabError("complexOrderStrategyType COVERED not allowed")
        return {"orderStrategy": {"status": "ACCEPTED"}}

    def place_order(self, account_hash, order):
        self.placed.append(order)
        return {"orderId": 4242}

    def get_order(self, account_hash, order_id):
        return self.order


@pytest.fixture()
def live(store, monkeypatch):
    monkeypatch.setattr(config, "EQUITY_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(executor.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(executor.data_handler, "latest_quote",
                        lambda t: {"price": 50.0, "bid": 49.98, "ask": 50.02})
    monkeypatch.setattr(executor, "_guard_resubmit", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_record_placement", lambda *a, **kw: None)


def _client(monkeypatch, **kw):
    c = _Client(**kw)
    monkeypatch.setattr(executor.data_handler, "client", lambda: c)
    monkeypatch.setattr(executor.data_handler, "broker_client", lambda: c)
    return c


def test_live_buy_write_is_one_order_with_both_legs(live, monkeypatch):
    c = _client(monkeypatch)
    res = _buy_write(option_symbol=SYM)
    assert res["status"] == "working" and res["order_id"] == "4242"
    assert len(c.placed) == 1 and c.previewed[0] == c.placed[0]
    o = c.placed[0]
    assert o["orderType"] == "NET_DEBIT" and o["complexOrderStrategyType"] == "COVERED"
    assert o["price"] == "47.40"                  # fresh mids: 50.00 - 2.60
    assert [(l["instruction"], l["quantity"], l["instrument"]["assetType"])
            for l in o["orderLegCollection"]] == [("BUY", 100, "EQUITY"),
                                                   ("SELL_TO_OPEN", 1, "OPTION")]
    # Nothing is booked until the ticket fills.
    assert _covered_execs() == []


def test_live_rejected_preview_never_places(live, monkeypatch):
    c = _client(monkeypatch, preview_ok=False)
    with pytest.raises(schwab_api.SchwabError, match="NOT placing"):
        _buy_write(option_symbol=SYM)
    assert c.previewed and c.placed == []


def test_live_fill_books_both_legs_at_the_broker_fills(live, monkeypatch):
    c = _client(monkeypatch)
    _buy_write(option_symbol=SYM)
    c.order = {
        "status": "FILLED",
        "orderLegCollection": [
            {"legId": 1, "instrument": {"symbol": "AAA"}},
            {"legId": 2, "instrument": {"symbol": SYM}},
        ],
        "orderActivityCollection": [{"executionLegs": [
            {"legId": 1, "price": 50.01, "time": "2026-09-21T14:00:00+0000"},
            {"legId": 2, "price": 2.64, "time": "2026-09-21T14:00:00+0000"},
        ]}],
    }
    monkeypatch.setattr(executor.data_handler, "fresh_quote", lambda t: {"price": 50.0})
    res = executor.order_status("4242")
    assert res["status"] == "filled"
    legs = _covered_execs()
    assert [(e["action"], e["mode"]) for e in legs] == [("buy_shares", "live"), ("sell_short", "live")]
    assert legs[0]["price_per_share"] == 50.01
    assert legs[1]["premium_per_share"] == 2.64
    assert len({e["date"] for e in legs}) == 1
