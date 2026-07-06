"""Atomic open: establish a position by buying the deep-ITM LEAP and selling the
weekly short on ONE ticket (a diagonal) — booked as two linked legs in paper,
and as a single NET_DEBIT order in live.
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-open-test-"))

import config            # noqa: E402
import data_handler      # noqa: E402
import executor          # noqa: E402
import logging_handler as log  # noqa: E402
import schwab_api        # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)  # paper by default
    return tmp_path


def _open_payload(**over):
    p = {
        "action": "open_position_atomic", "ticker": "XLK", "contracts": 1, "stock_price": 184.0,
        # LEAP leg (buy to open)
        "strike": 137.5, "execution_price": 5325, "expiration": "2027-01-15", "dte": 193,
        "option_symbol": "XLK_LEAP",
        # weekly short leg (sell to open)
        "short_strike": 181, "short_premium_per_share": 5.40, "short_expiration": "2026-07-10",
        "short_dte": 4, "short_option_symbol": "XLK_SHORT", "weekly_extrinsic_per_share": 1.99,
        # entry context
        "circuit_breaker_price": 178.58, "override_reason": "test",
    }
    p.update(over)
    return p


def test_paper_open_books_both_legs(store):
    res = executor.execute(_open_payload())
    assert res["status"] == "filled" and res["mode"] == "logged"
    assert len(res["executions"]) == 2
    assert {e["open_leg"] for e in res["executions"]} == {"leap", "short"}
    assert len({e["open_id"] for e in res["executions"]}) == 1  # one shared open id

    pos = log.find_position(log.load_state(), "XLK")
    assert pos["status"] == "active"
    assert pos["leap"]["strike"] == 137.5 and pos["leap"]["contracts"] == 1
    assert len(pos["short_calls"]) == 1 and pos["short_calls"][0]["strike"] == 181
    # net debit = LEAP cost 5325 − short credit 540 = 4785
    assert res["net_debit"] == pytest.approx(4785.0)


def test_open_rejected_when_leap_already_held(store):
    executor.execute(_open_payload())
    with pytest.raises(ValueError, match="already holds a LEAP"):
        executor.execute(_open_payload())


class _FakeClient:
    def __init__(self, order):
        self._order = order
        self.sent = None

    def primary_account_hash(self):
        return "acct"

    def place_order(self, account_hash, order):
        self.sent = order
        return {"orderId": "OID_OPEN"}

    def get_order(self, account_hash, order_id):
        return self._order


def test_live_open_lifecycle_mocked(store, monkeypatch):
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    filled = {
        "status": "FILLED",
        "orderLegCollection": [
            {"legId": 1, "instrument": {"symbol": "XLK_LEAP"}},
            {"legId": 2, "instrument": {"symbol": "XLK_SHORT"}},
        ],
        "orderActivityCollection": [{"executionLegs": [
            {"legId": 1, "price": 53.10}, {"legId": 2, "price": 5.55}]}],
    }
    fake = _FakeClient(filled)
    monkeypatch.setattr(data_handler, "client", lambda: fake)

    placed = executor.execute(_open_payload())
    assert placed["status"] == "working" and placed["order_id"] == "OID_OPEN"
    # One NET_DEBIT diagonal: buy-to-open the LEAP + sell-to-open the short.
    assert fake.sent["orderType"] == "NET_DEBIT"
    assert [l["instruction"] for l in fake.sent["orderLegCollection"]] == ["BUY_TO_OPEN", "SELL_TO_OPEN"]

    # Nothing committed until the fill confirms.
    assert log.find_position(log.load_state(), "XLK")["leap"] is None

    done = executor.order_status("OID_OPEN")
    assert done["status"] == "filled"
    pos = log.find_position(log.load_state(), "XLK")
    assert pos["leap"]["strike"] == 137.5 and len(pos["short_calls"]) == 1
    # Legs booked at the REAL per-leg fills, not the staged estimates.
    leap_leg = next(e for e in done["executions"] if e.get("open_leg") == "leap")
    short_leg = next(e for e in done["executions"] if e.get("open_leg") == "short")
    assert leap_leg["execution_price"] == pytest.approx(5310.0)  # 53.10 × 100
    assert short_leg["premium_per_share"] == pytest.approx(5.55)


def test_demo_open_never_transmits(store, monkeypatch):
    # Live enabled but demo on: must book paper, never touch the broker.
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(config, "_demo_mode", True)

    class _Boom:
        def primary_account_hash(self): raise AssertionError("demo must not reach the broker")
        def place_order(self, *a): raise AssertionError("demo must not place an order")
    monkeypatch.setattr(data_handler, "client", lambda: _Boom())

    res = executor.execute(_open_payload())
    assert res["status"] == "filled" and res["mode"] == "logged"
    assert len(res["executions"]) == 2
