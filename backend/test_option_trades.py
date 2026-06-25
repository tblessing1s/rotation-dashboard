"""Option execution + theta-entry snapshot.

Covers the model-free intrinsic/extrinsic math, OSI symbol + order construction,
Schwab fill parsing, and the place_option orchestration — including the hard
kill-switch (no real order is ever transmitted in these tests; the provider is
fully mocked)."""
from unittest import mock

import pytest

import db
import option_trades
import options_math
import schwab_orders


# ---------------------------------------------------------------------------
# options_math — the deterministic decomposition
# ---------------------------------------------------------------------------
def test_call_in_the_money_splits_premium():
    out = options_math.decompose("call", strike=150, stock_price=156, premium=8.0)
    assert out["intrinsic"] == 6.0          # 156 - 150
    assert out["extrinsic"] == 2.0          # 8.0 - 6.0
    assert out["moneyness"] == "ITM"
    assert out["extrinsicPct"] == 25.0


def test_call_out_of_the_money_is_all_extrinsic():
    out = options_math.decompose("call", strike=150, stock_price=148, premium=1.25)
    assert out["intrinsic"] == 0.0
    assert out["extrinsic"] == 1.25
    assert out["moneyness"] == "OTM"


def test_put_in_the_money_splits_premium():
    out = options_math.decompose("put", strike=150, stock_price=144, premium=7.5)
    assert out["intrinsic"] == 6.0          # 150 - 144
    assert out["extrinsic"] == 1.5
    assert out["moneyness"] == "ITM"


def test_at_the_money_reads_atm():
    out = options_math.decompose("call", strike=150, stock_price=150, premium=3.0)
    assert out["intrinsic"] == 0.0
    assert out["moneyness"] == "ATM"


def test_bad_option_type_raises():
    with pytest.raises(ValueError):
        options_math.decompose("straddle", 150, 150, 3.0)


# ---------------------------------------------------------------------------
# OSI symbol + order construction
# ---------------------------------------------------------------------------
def test_osi_symbol_layout():
    assert schwab_orders.osi_symbol("AAPL", "2026-06-19", "call", 150) == "AAPL  260619C00150000"
    # Fractional strike and a short root.
    assert schwab_orders.osi_symbol("F", "2026-01-16", "put", 12.5) == "F     260116P00012500"


def test_build_option_order_limit_buy():
    order = schwab_orders.build_option_order({
        "underlying": "AAPL", "expiry": "2026-06-19", "option_type": "call",
        "strike": 150, "quantity": 2, "side": "buy", "order_type": "LIMIT",
        "limit_price": 8.05,
    })
    assert order["orderType"] == "LIMIT"
    assert order["price"] == "8.05"
    assert order["orderStrategyType"] == "SINGLE"
    leg = order["orderLegCollection"][0]
    assert leg["instruction"] == "BUY_TO_OPEN"
    assert leg["quantity"] == 2
    assert leg["instrument"] == {"symbol": "AAPL  260619C00150000", "assetType": "OPTION"}


def test_build_option_order_market_has_no_price():
    order = schwab_orders.build_option_order({
        "underlying": "MSFT", "expiry": "2026-06-19", "option_type": "put",
        "strike": 400, "quantity": 1, "side": "buy", "order_type": "MARKET",
    })
    assert order["orderType"] == "MARKET"
    assert "price" not in order


def test_build_option_order_limit_requires_price():
    with pytest.raises(ValueError):
        schwab_orders.build_option_order({
            "underlying": "AAPL", "expiry": "2026-06-19", "option_type": "call",
            "strike": 150, "quantity": 1, "side": "buy", "order_type": "LIMIT",
        })


# ---------------------------------------------------------------------------
# Fill parsing from a Schwab order-status payload
# ---------------------------------------------------------------------------
def test_parse_fill_weights_execution_legs():
    status = {
        "status": "FILLED", "filledQuantity": 3,
        "orderActivityCollection": [
            {"executionLegs": [
                {"quantity": 1, "price": 8.00},
                {"quantity": 2, "price": 8.30},
            ]},
        ],
    }
    out = schwab_orders.parse_fill(status)
    assert out["status"] == "FILLED"
    assert out["filledQuantity"] == 3.0
    assert out["fillPrice"] == round((8.00 * 1 + 8.30 * 2) / 3, 4)


def test_parse_fill_working_order_has_no_fill_price():
    out = schwab_orders.parse_fill({"status": "WORKING", "price": 8.05})
    assert out["status"] == "WORKING"
    assert out["fillPrice"] is None


def test_parse_fill_falls_back_to_order_price_when_filled():
    out = schwab_orders.parse_fill({"status": "FILLED", "price": 8.05, "filledQuantity": 1})
    assert out["fillPrice"] == 8.05


# ---------------------------------------------------------------------------
# Kill-switch — place_option refuses without the env flag
# ---------------------------------------------------------------------------
_SPEC = {
    "underlying": "AAPL", "expiry": "2026-06-19", "option_type": "call",
    "strike": 150, "quantity": 1, "side": "buy", "order_type": "LIMIT",
    "limit_price": 8.05,
}


def test_place_option_blocked_when_kill_switch_off(monkeypatch):
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.delenv("SCHWAB_LIVE_TRADING_ENABLED", raising=False)
    # Guard: place_order must never be reached.
    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        lambda self, h, o: (_ for _ in ()).throw(AssertionError("must not place")),
    )
    out = option_trades.place_option(_SPEC)
    assert out["ok"] is False
    assert out["liveDisabled"] is True


# ---------------------------------------------------------------------------
# Full place flow — armed kill-switch, fully mocked provider (no real order)
# ---------------------------------------------------------------------------
def test_place_option_fills_and_snapshots_theta(fresh_db, monkeypatch):
    monkeypatch.setenv("SCHWAB_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        option_trades.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        lambda self, h, o: {"orderId": "ORD1", "location": ".../orders/ORD1"},
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_order",
        lambda self, h, oid: {
            "status": "FILLED", "filledQuantity": 1,
            "orderActivityCollection": [{"executionLegs": [{"quantity": 1, "price": 8.0}]}],
        },
    )
    # Stock at 156 -> intrinsic 6, extrinsic 2 on an 8.00 premium 150 call.
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_quote",
        lambda self, sym: {"symbol": sym, "last": 156.0, "mark": 156.0, "quoteTimeMs": 1718900000000},
    )

    out = option_trades.place_option(_SPEC, sleep_fn=lambda s: None)
    assert out["ok"] is True
    assert out["orderId"] == "ORD1"
    assert out["status"] == "FILLED"
    assert out["split"]["intrinsic"] == 6.0
    assert out["split"]["extrinsic"] == 2.0
    # The ledger row was persisted.
    fills = db.list_option_fills(underlying="AAPL")
    assert len(fills) == 1
    assert fills[0]["order_id"] == "ORD1"
    assert fills[0]["intrinsic"] == 6.0
    assert fills[0]["extrinsic"] == 2.0
    assert fills[0]["osi_symbol"] == "AAPL  260619C00150000"


def test_place_option_records_are_idempotent(fresh_db, monkeypatch):
    monkeypatch.setenv("SCHWAB_LIVE_TRADING_ENABLED", "1")
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        option_trades.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        lambda self, h, o: {"orderId": "ORD9"},
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_order",
        lambda self, h, oid: {"status": "FILLED", "filledQuantity": 1, "price": 8.0},
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_quote",
        lambda self, sym: {"symbol": sym, "last": 156.0},
    )
    option_trades.place_option(_SPEC, sleep_fn=lambda s: None)
    option_trades.place_option(_SPEC, sleep_fn=lambda s: None)  # retry, same order id
    assert len(db.list_option_fills(underlying="AAPL")) == 1


def test_place_option_working_order_takes_no_snapshot(fresh_db, monkeypatch):
    monkeypatch.setenv("SCHWAB_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        option_trades.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        lambda self, h, o: {"orderId": "ORD2"},
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_order",
        lambda self, h, oid: {"status": "WORKING", "price": 8.05},
    )
    # A working order must never call get_quote or write a fill.
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_quote",
        lambda self, sym: (_ for _ in ()).throw(AssertionError("no quote on working order")),
    )
    out = option_trades.place_option(_SPEC, max_polls=2, sleep_fn=lambda s: None)
    assert out["ok"] is True
    assert out["status"] == "WORKING"
    assert out["fill"] is None
    assert db.list_option_fills() == []


def test_preview_option_never_places(fresh_db, monkeypatch):
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        option_trades.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "preview_order",
        lambda self, h, o: {"orderStrategy": {"orderValue": 805.0, "quantity": 1, "price": 8.05}},
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        lambda self, h, o: (_ for _ in ()).throw(AssertionError("preview must not place")),
    )
    out = option_trades.preview_option(_SPEC)
    assert out["ok"] is True
    assert out["mode"] == "PREVIEW"
    assert out["preview"]["status"] == "OK"
