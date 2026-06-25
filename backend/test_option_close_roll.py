"""Close and roll option positions."""
import db
import option_trades
import schwab_orders


def _seed_open_fill(**over):
    """Insert one open call fill to close: 150 strike, $8 premium."""
    fill = {
        "order_id": over.get("order_id", "ORD_OPEN"),
        "underlying": "AAPL",
        "osi_symbol": "AAPL  260619C00150000",
        "option_type": "call",
        "strike": 150.0,
        "expiry": "2026-06-19",
        "side": "buy",
        "quantity": 2,
        "premium": 8.0,
        "stock_price": 156.0,
        "intrinsic": 6.0,
        "extrinsic": 2.0,
    }
    fill.update(over)
    return db.record_option_fill(fill)


def test_build_option_close_order():
    """Build a close order for a bought-to-open position."""
    close_spec = {
        "underlying": "AAPL",
        "expiry": "2026-06-19",
        "option_type": "call",
        "strike": 150.0,
        "quantity": 2,
        "side": "sell",  # opposite of the opening "buy"
        "order_type": "LIMIT",
        "limit_price": 7.5,
    }
    order = schwab_orders.build_option_close_order(close_spec)
    assert order["orderType"] == "LIMIT"
    assert order["price"] == "7.50"
    leg = order["orderLegCollection"][0]
    assert leg["instruction"] == "SELL_TO_CLOSE"
    assert leg["quantity"] == 2
    assert leg["instrument"]["symbol"] == "AAPL  260619C00150000"


def test_build_option_close_order_short_position():
    """Build a close order for a sold-to-open position."""
    close_spec = {
        "underlying": "AAPL",
        "expiry": "2026-06-19",
        "option_type": "call",
        "strike": 150.0,
        "quantity": 2,
        "side": "buy",  # opposite of the opening "sell"
        "order_type": "MARKET",
    }
    order = schwab_orders.build_option_close_order(close_spec)
    assert order["orderType"] == "MARKET"
    leg = order["orderLegCollection"][0]
    assert leg["instruction"] == "BUY_TO_CLOSE"
    assert leg["quantity"] == 2


def test_close_option_captures_close_side(fresh_db, monkeypatch):
    """Closing a position records side as 'sell_to_close'."""
    row = _seed_open_fill()

    monkeypatch.setenv("SCHWAB_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        option_trades.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        lambda self, h, o: {"orderId": "ORD_CLOSE"},
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_order",
        lambda self, h, oid: {
            "status": "FILLED", "filledQuantity": 2,
            "orderActivityCollection": [{"executionLegs": [{"quantity": 2, "price": 7.5}]}],
        },
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_quote",
        lambda self, sym: {"symbol": sym, "last": 156.0},
    )

    out = option_trades.close_option(row["id"], order_type="MARKET")
    assert out["ok"] is True
    assert out["status"] == "FILLED"

    # The close should be recorded as a new fill with side="sell_to_close".
    close_fill = out.get("fill")
    assert close_fill is not None
    assert close_fill["side"] == "sell_to_close"
    assert close_fill["premium"] == 7.5
    assert close_fill["quantity"] == 2


def test_batch_close_options(fresh_db, monkeypatch):
    """Batch close multiple positions."""
    fill1 = _seed_open_fill(order_id="ORD1")
    fill2 = _seed_open_fill(order_id="ORD2", strike=160.0, osi_symbol="AAPL  260619C00160000")

    monkeypatch.setenv("SCHWAB_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        option_trades.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        lambda self, h, o: {"orderId": f"CLOSE_{o['orderLegCollection'][0]['quantity']}"},
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_order",
        lambda self, h, oid: {
            "status": "FILLED", "filledQuantity": 2,
            "orderActivityCollection": [{"executionLegs": [{"quantity": 2, "price": 7.5}]}],
        },
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_quote",
        lambda self, sym: {"symbol": sym, "last": 156.0},
    )

    out = option_trades.batch_close_options([fill1["id"], fill2["id"]], order_type="MARKET")
    assert out["ok"] is True
    assert len(out["closed"]) == 2
    assert len(out["failed"]) == 0


def test_roll_option(fresh_db, monkeypatch):
    """Roll a position to a new strike/expiry."""
    fill = _seed_open_fill()

    monkeypatch.setenv("SCHWAB_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setattr(option_trades.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        option_trades.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )

    call_count = {"count": 0}

    def fake_place(self, account_hash, order):
        call_count["count"] += 1
        # First call is the close, second is the new open.
        order_id = "ORD_CLOSE" if call_count["count"] == 1 else "ORD_OPEN_NEW"
        return {"orderId": order_id}

    monkeypatch.setattr(
        option_trades.SchwabProvider, "place_order",
        fake_place,
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_order",
        lambda self, h, oid: {
            "status": "FILLED", "filledQuantity": 2,
            "orderActivityCollection": [{"executionLegs": [{"quantity": 2, "price": 7.5}]}],
        },
    )
    monkeypatch.setattr(
        option_trades.SchwabProvider, "get_quote",
        lambda self, sym: {"symbol": sym, "last": 156.0},
    )

    out = option_trades.roll_option(
        fill["id"],
        new_strike=160.0,
        new_expiry="2026-07-17",
        close_order_type="MARKET",
        open_order_type="LIMIT",
        open_limit_price=8.0,
    )
    assert out["ok"] is True
    assert out["closed"]["ok"] is True
    assert out["opened"]["ok"] is True
    assert call_count["count"] == 2


def test_closed_positions_skipped_in_refresh(fresh_db, monkeypatch):
    """Refresh skips closed positions."""
    import theta_ledger

    # Create an open position with future expiry.
    open_fill = _seed_open_fill(
        order_id="OPEN",
        expiry="2026-07-19",
        osi_symbol="AAPL  260719C00150000",
    )
    # Create a close fill (also with future expiry so it's not expired).
    close_fill = _seed_open_fill(
        order_id="CLOSE",
        side="sell_to_close",
        premium=7.5,
        expiry="2026-07-19",
        osi_symbol="AAPL  260719C00150000",
    )

    monkeypatch.setattr(theta_ledger.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        theta_ledger.SchwabProvider, "get_quotes",
        lambda self, symbols: {
            "AAPL  260719C00150000": {"mark": 7.0, "underlyingPrice": 157.0},
            "AAPL": {"last": 157.0},
        },
    )

    out = theta_ledger.refresh(as_of="2026-06-25")
    # Should only refresh the open position, not the closed one.
    assert out["ok"] is True
    # open_fills will include both, but only the open one should get a mark.
    assert out["refreshed"] == 1
