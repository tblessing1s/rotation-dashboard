"""Schwab account sync: trade activity normalizes into the same ledger rows a
CSV import produces, the positions snapshot is summarized, and partial provider
failures are reported per-source instead of aborting the whole sync."""
from unittest import mock

import pytest

import schwab_account
from providers.base import ProviderError


def _trade(symbol, qty, price, effect, cost, asset="EQUITY"):
    return {
        "type": "TRADE",
        "tradeDate": "2026-06-09T14:30:00+0000",
        "orderId": 555,
        "description": f"{symbol} fill",
        "transferItems": [
            {  # a fee leg that must be ignored
                "feeType": "COMMISSION",
                "amount": 0,
                "cost": -0.65,
            },
            {
                "instrument": {"symbol": symbol, "assetType": asset},
                "amount": qty,
                "price": price,
                "cost": cost,
                "positionEffect": effect,
            },
        ],
    }


def test_normalize_trade_maps_a_buy_to_open_long_row():
    rows = schwab_account.normalize_trade(_trade("XLV", 10, 140.0, "OPENING", -1400.0))
    assert len(rows) == 1  # the commission leg is dropped
    row = rows[0]
    assert row["symbol"] == "XLV"
    assert row["date"] == "2026-06-09"
    assert row["qty"] == 10
    assert row["price"] == 140.0
    assert row["leg"] == "long"
    assert row["flowType"] == "open"
    assert row["action"] == "BUY TO OPEN"
    assert row["amount"] == -1400.0      # buy is a cash debit
    assert row["strategy"] == "SCHWAB"
    assert row["positionId"] == "555"


def test_normalize_trade_maps_a_sell_to_close_long_row():
    rows = schwab_account.normalize_trade(_trade("XLV", -10, 150.0, "CLOSING", 1500.0))
    row = rows[0]
    assert row["leg"] == "long"          # selling closes the long leg
    assert row["flowType"] == "close"
    assert row["action"] == "SELL TO CLOSE"
    assert row["amount"] == 1500.0       # sale is a cash credit


def test_normalize_trade_infers_cash_when_cost_missing():
    txn = _trade("AAPL", 5, 200.0, "OPENING", None)
    row = schwab_account.normalize_trade(txn)[0]
    assert row["amount"] == -1000.0      # -(amount * price)


def test_normalize_account_summarizes_positions_and_balances():
    account = {
        "securitiesAccount": {
            "accountNumber": "12345678",
            "type": "MARGIN",
            "currentBalances": {"liquidationValue": 50000.0, "cashBalance": 12000.0},
            "positions": [
                {
                    "instrument": {"symbol": "XLV", "assetType": "EQUITY"},
                    "longQuantity": 10, "shortQuantity": 0,
                    "averagePrice": 140.0, "marketValue": 1500.0,
                    "currentDayProfitLoss": 25.0, "longOpenProfitLoss": 100.0,
                },
            ],
        }
    }
    snap = schwab_account.normalize_account(account)
    assert snap["account"] == "****5678"  # masked
    assert snap["liquidationValue"] == 50000.0
    assert snap["positions"][0]["symbol"] == "XLV"
    assert snap["positions"][0]["netQty"] == 10


def test_sync_without_credentials_is_a_soft_no_op(monkeypatch):
    monkeypatch.setattr(schwab_account.SchwabProvider, "configured", staticmethod(lambda: False))
    out = schwab_account.sync()
    assert out["configured"] is False
    assert out["transactions"] == []


def test_sync_reports_partial_failure_and_still_returns_positions(fresh_db, monkeypatch):
    monkeypatch.setattr(schwab_account.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        schwab_account.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(
        schwab_account.SchwabProvider, "get_accounts",
        lambda self, positions=True: [{
            "securitiesAccount": {"accountNumber": "12345678", "type": "CASH",
                                  "currentBalances": {}, "positions": []}
        }],
    )

    def boom(self, account_hash, start, end, types="TRADE"):
        raise ProviderError("schwab account: HTTP 403 Forbidden")

    monkeypatch.setattr(schwab_account.SchwabProvider, "get_transactions", boom)

    out = schwab_account.sync(days=30)
    assert out["configured"] is True
    assert len(out["accounts"]) == 1            # snapshot still came back
    assert out["transactions"] == []
    assert "transactions:****5678" in out["errors"]


def test_sync_normalizes_trades_across_accounts(fresh_db, monkeypatch):
    monkeypatch.setattr(schwab_account.SchwabProvider, "configured", staticmethod(lambda: True))
    monkeypatch.setattr(
        schwab_account.SchwabProvider, "account_numbers",
        lambda self: [{"accountNumber": "12345678", "hashValue": "HASH"}],
    )
    monkeypatch.setattr(schwab_account.SchwabProvider, "get_accounts", lambda self, positions=True: [])
    monkeypatch.setattr(
        schwab_account.SchwabProvider, "get_transactions",
        lambda self, account_hash, start, end, types="TRADE": [
            _trade("XLV", 10, 140.0, "OPENING", -1400.0),
            {"type": "DIVIDEND", "transferItems": []},  # non-trade is ignored
        ],
    )
    out = schwab_account.sync(days=30)
    assert [r["symbol"] for r in out["transactions"]] == ["XLV"]
    assert out["errors"] == {}
