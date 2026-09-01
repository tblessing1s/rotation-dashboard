"""The spot stamped on an execution — the number the juice ledger is made of.

WHY EXECUTING FROM THE APP EXISTS. A short leg's entry extrinsic is
`premium - max(spot - strike, 0)` (`executor._short_extrinsic`), and CFM's
covered call is deliberately ITM, so intrinsic is almost always positive and the
spot enters the juice figure DOLLAR FOR DOLLAR. Routing the order through the app
rather than the broker is what makes it possible to capture that spot at the
moment of the trade.

The bug these pin: `_capture_price` returned any supplied value verbatim, and the
UI always supplies one — `OptionChainModal` sends `chain.underlying_price`, from
a chain cached for `option_chain._CHAIN_TTL` = 300 SECONDS. So a live order could
book its extrinsic against a five-minute-old spot, and the error is permanent
because the execution log is append-only.
"""
from __future__ import annotations

import pytest

import config
import executor


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


@pytest.fixture()
def quote(monkeypatch):
    """A live quote that differs from the operator's stale snapshot."""
    monkeypatch.setattr(executor.data_handler, "latest_quote",
                        lambda t: {"price": 142.94, "source": "schwab"})


# ---------------------------------------------------------------------------
# The two callers are genuinely different
# ---------------------------------------------------------------------------
def test_a_real_order_requotes_the_spot_and_ignores_the_stale_snapshot(quote):
    """The staged 140.00 is the chain snapshot, up to 5 minutes old. The order is
    going to the broker NOW, so the spot must be NOW."""
    price, source = executor._capture_price("AAA", 140.00, at_order_time=True)
    assert price == 142.94
    assert source == "schwab"


def test_a_manual_booking_keeps_the_operators_price(quote):
    """The mirror image, and it matters just as much. Recording a fill that
    happened at 10:31 must not stamp the 15:55 quote on it — a live quote is a
    DIFFERENT moment than the fill being recorded, and would corrupt exactly what
    the record exists to preserve."""
    price, source = executor._capture_price("AAA", 140.00)
    assert price == 140.00 and source == "supplied"


def test_a_stale_fallback_is_labelled_as_such(monkeypatch):
    """When the quote is unavailable the snapshot is still better than nothing —
    but the record must say so, or a suspect extrinsic is unauditable later."""
    monkeypatch.setattr(executor.data_handler, "latest_quote", lambda t: None)
    price, source = executor._capture_price("AAA", 140.00, at_order_time=True)
    assert price == 140.00
    assert source == "supplied_stale_quote_unavailable"
    assert source != "supplied", "must not pass a stale price off as a fresh one"


# ---------------------------------------------------------------------------
# The consequence: extrinsic
# ---------------------------------------------------------------------------
def test_a_stale_spot_corrupts_extrinsic_dollar_for_dollar():
    """The arithmetic that makes this worth fixing. An ITM covered call, spot
    drifting $0.50 while the ticket sat open."""
    premium, strike = 5.68, 139.0
    stale = executor._short_extrinsic(premium, 142.44, strike)   # 5-min-old spot
    fresh = executor._short_extrinsic(premium, 142.94, strike)   # spot at the order
    assert stale - fresh == pytest.approx(0.50, abs=1e-9)
    # Per contract, on one weekly, on one name.
    assert (stale - fresh) * 100 == pytest.approx(50.0, abs=1e-6)


def test_extrinsic_uses_the_captured_spot_not_the_strike_alone():
    """Guards the derivation itself: extrinsic must fall as spot rises on an ITM
    short call, which is why the captured spot cannot be approximate."""
    assert executor._short_extrinsic(5.68, 140.0, 139.0) > \
           executor._short_extrinsic(5.68, 141.0, 139.0)


# ---------------------------------------------------------------------------
# Which calls re-quote
# ---------------------------------------------------------------------------
def test_transmits_is_false_when_nothing_will_be_sent(monkeypatch):
    """A paper session keeps the operator's price — re-quoting a booking that
    never leaves the app would replace a known number with an unrelated one."""
    monkeypatch.setattr(executor, "live_transmit", lambda: False)
    assert executor._transmits("sell_short") is False


def test_transmits_tracks_each_flag(monkeypatch):
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    monkeypatch.setattr(executor.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(config, "EQUITY_ORDER_PLACEMENT_ENABLED", False)
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", False)
    assert executor._transmits("buy_shares") is False
    assert executor._transmits("put_opened") is False
    assert executor._transmits("sell_short") is True      # never flag-gated

    monkeypatch.setattr(config, "EQUITY_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    assert executor._transmits("buy_shares") is True
    assert executor._transmits("put_opened") is True
    # An assignment is an EVENT, not an order — nothing is sent, so the
    # operator's price for a fill that already happened stands.
    assert executor._transmits("put_assigned") is False


def test_transmits_is_false_without_schwab(monkeypatch):
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    monkeypatch.setattr(executor.schwab_api, "configured", lambda: False)
    assert executor._transmits("sell_short") is False
