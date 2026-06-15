"""Schwab bracket-order build + previewOrder dry-run.

Verifies the signal -> Schwab one-triggers-OCO mapping (long/short, market entry),
the defensive preview-response normalization, and that ``preview_bracket`` resolves
an account and dry-runs without ever placing a live order.
"""
from unittest import mock

import pytest

import schwab_orders
from providers.base import ProviderError


def _signal(**over):
    base = {
        "date": "2026-06-15",
        "ticker": "hood",
        "candle_time": "08:35",
        "direction": "Long",
        "entry_price": 88.27,
        "stop_price": 88.12,
        "target_price": 88.57,
        "position_size": 133,
    }
    base.update(over)
    return base


# -- build_bracket_order -----------------------------------------------------
def test_build_long_bracket_is_trigger_with_oco_exits():
    order = schwab_orders.build_bracket_order(_signal())
    assert order["orderStrategyType"] == "TRIGGER"
    assert order["orderType"] == "LIMIT"
    assert order["price"] == "88.27"
    entry_leg = order["orderLegCollection"][0]
    assert entry_leg["instruction"] == "BUY"
    assert entry_leg["quantity"] == 133
    assert entry_leg["instrument"] == {"symbol": "HOOD", "assetType": "EQUITY"}

    oco = order["childOrderStrategies"][0]
    assert oco["orderStrategyType"] == "OCO"
    take_profit, stop_loss = oco["childOrderStrategies"]
    assert take_profit["orderType"] == "LIMIT" and take_profit["price"] == "88.57"
    assert stop_loss["orderType"] == "STOP" and stop_loss["stopPrice"] == "88.12"
    # Both protective legs SELL to close the long.
    assert take_profit["orderLegCollection"][0]["instruction"] == "SELL"
    assert stop_loss["orderLegCollection"][0]["instruction"] == "SELL"


def test_build_short_bracket_uses_sell_short_and_buy_to_cover():
    order = schwab_orders.build_bracket_order(
        _signal(direction="Short", entry_price=25.13, stop_price=25.40, target_price=24.59)
    )
    assert order["orderLegCollection"][0]["instruction"] == "SELL_SHORT"
    oco = order["childOrderStrategies"][0]["childOrderStrategies"]
    assert all(leg["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER" for leg in oco)


def test_build_market_entry_drops_price():
    order = schwab_orders.build_bracket_order(_signal(order_type="MARKET"))
    assert order["orderType"] == "MARKET"
    assert "price" not in order


@pytest.mark.parametrize("bad", [
    {"direction": "sideways"},
    {"position_size": 0},
    {"entry_price": 0},
])
def test_build_rejects_malformed_signal(bad):
    with pytest.raises((ValueError, KeyError)):
        schwab_orders.build_bracket_order(_signal(**bad))


# -- normalize_preview -------------------------------------------------------
def test_normalize_preview_ok_with_fee_total():
    payload = {
        "orderStrategy": {"orderValue": 11739.91, "quantity": 133, "price": 88.27},
        "orderValidationResult": {"rejects": [], "alerts": []},
        "commissionAndFee": {
            "commission": {"commissionLegs": [{"commissionValues": [{"value": 0.0}]}]},
            "fee": {"feeLegs": [{"feeValues": [{"value": 0.03}, {"value": 0.01}]}]},
        },
    }
    out = schwab_orders.normalize_preview(payload)
    assert out["status"] == "OK"
    assert out["orderValue"] == 11739.91
    assert out["estimatedCost"] == 0.04
    assert out["rejects"] == [] and out["alerts"] == []


def test_normalize_preview_surfaces_rejects():
    payload = {"orderValidationResult": {"rejects": [{"message": "Buying power exceeded"}]}}
    out = schwab_orders.normalize_preview(payload)
    assert out["status"] == "REJECTED"
    assert out["rejects"] == ["Buying power exceeded"]


def test_normalize_preview_alerts_only_is_warning():
    payload = {"orderValidationResult": {"alerts": [{"message": "Extended hours"}]}}
    assert schwab_orders.normalize_preview(payload)["status"] == "WARNING"


# -- preview_bracket ---------------------------------------------------------
def test_preview_bracket_requires_credentials():
    with mock.patch.object(schwab_orders.SchwabProvider, "configured", return_value=False):
        out = schwab_orders.preview_bracket(_signal())
    assert out["ok"] is False and "credentials" in out["error"]


def test_preview_bracket_resolves_account_and_previews():
    provider = mock.Mock()
    provider.account_numbers.return_value = [{"accountNumber": "12345678", "hashValue": "HASH"}]
    provider.preview_order.return_value = {
        "orderStrategy": {"orderValue": 11739.91},
        "orderValidationResult": {"rejects": []},
    }
    with mock.patch.object(schwab_orders.SchwabProvider, "configured", return_value=True), \
         mock.patch.object(schwab_orders, "SchwabProvider", return_value=provider):
        out = schwab_orders.preview_bracket(_signal())

    assert out["ok"] is True and out["mode"] == "PREVIEW"
    assert out["account"] == "****5678"
    assert out["preview"]["status"] == "OK"
    # The dry-run hit previewOrder with the resolved hash — never a place_order.
    provider.preview_order.assert_called_once()
    assert provider.preview_order.call_args[0][0] == "HASH"
    assert not hasattr(provider, "place_order") or not provider.place_order.called


def test_preview_bracket_reports_provider_error():
    provider = mock.Mock()
    provider.account_numbers.side_effect = ProviderError("HTTP 403 not approved")
    with mock.patch.object(schwab_orders.SchwabProvider, "configured", return_value=True), \
         mock.patch.object(schwab_orders, "SchwabProvider", return_value=provider):
        out = schwab_orders.preview_bracket(_signal())
    assert out["ok"] is False and "403" in out["error"]


def test_preview_bracket_rejects_bad_signal_before_calling_schwab():
    provider = mock.Mock()
    with mock.patch.object(schwab_orders.SchwabProvider, "configured", return_value=True), \
         mock.patch.object(schwab_orders, "SchwabProvider", return_value=provider):
        out = schwab_orders.preview_bracket(_signal(position_size=0))
    assert out["ok"] is False and "Invalid signal" in out["error"]
    provider.preview_order.assert_not_called()
