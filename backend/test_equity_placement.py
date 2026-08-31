"""Live EQUITY order submission — the path the app had never had.

The app could buy shares in its ledger and never at the broker: the dispatch
routed `buy_shares`/`sell_shares` straight to a construct-and-preview-only
committer, because `schwab_api.build_equity_order` was marked LIVE_VERIFY
throughout — the instruction verbs, `assetType: "EQUITY"` as an ORDER field, and
the share-count quantity semantics were BELIEVED but never confirmed against a
live account. The Phase-0 audit (§7) required an accepted `previewOrder` before
any place path was enabled.

That requirement is met by making the preview MANDATORY AND PER-ORDER rather than
a one-time manual capture: Schwab's own validator confirms the payload on every
order, and a rejection is a hard stop. These tests pin that, and pin that the
flag-off behaviour is unchanged.
"""
from __future__ import annotations

import pytest

import config
import executor
import logging_handler as log
import schwab_api


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


class _Client:
    """A Schwab client whose preview can be made to fail independently of place."""

    def __init__(self, preview_ok=True, place_ok=True):
        self.preview_ok, self.place_ok = preview_ok, place_ok
        self.previewed, self.placed = [], []

    def primary_account_hash(self):
        return "HASH"

    def preview_order(self, account_hash, order):
        self.previewed.append(order)
        if not self.preview_ok:
            raise schwab_api.SchwabError("field 'assetType' is not valid for this account")
        return {"orderStrategy": {"status": "ACCEPTED"}}

    def place_order(self, account_hash, order):
        self.placed.append(order)
        if not self.place_ok:
            raise schwab_api.SchwabError("place failed")
        return {"orderId": 991}


@pytest.fixture()
def live(monkeypatch):
    """Every switch on, Schwab connected, a fresh two-sided quote."""
    monkeypatch.setattr(config, "EQUITY_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(executor.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(executor.data_handler, "latest_quote",
                        lambda t: {"price": 50.0, "bid": 49.98, "ask": 50.02})
    monkeypatch.setattr(executor, "_guard_resubmit", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_record_placement", lambda *a, **kw: None)


def _client(monkeypatch, **kw):
    c = _Client(**kw)
    monkeypatch.setattr(executor.data_handler, "client", lambda: c)
    return c


def _buy(qty=100):
    # The empty test account has no cash, so the Level-5 gate blocks every entry.
    # Its own suite covers it; these tests are about the ORDER path beyond it, so
    # they take the documented (and logged) override rather than mocking the gate.
    return executor.execute({"action": "buy_shares", "ticker": "AAA", "qty": qty,
                             "stock_price": 50.0, "price_per_share": 50.0,
                             "line_in_the_sand": 45.0,
                             "override_reason": "equity order path test"})


# ---------------------------------------------------------------------------
# THE load-bearing rule: a failed preview must never become a live order
# ---------------------------------------------------------------------------
def test_a_rejected_preview_never_places(store, live, monkeypatch):
    """The whole reason this path was gated. If Schwab will not accept the
    payload, the payload is wrong — and a wrong equity order is a real trade in
    the wrong quantity or the wrong direction, not a validation error."""
    c = _client(monkeypatch, preview_ok=False)
    with pytest.raises(schwab_api.SchwabError, match="NOT placing"):
        _buy()
    assert c.previewed, "it must have tried to preview"
    assert c.placed == [], "a rejected preview must never reach place_order"


def test_the_rejection_carries_schwabs_own_words(store, live, monkeypatch):
    """A generic failure would leave the operator guessing which field is wrong —
    and the unverified fields are exactly what this preview exists to test."""
    _client(monkeypatch, preview_ok=False)
    with pytest.raises(schwab_api.SchwabError) as e:
        _buy()
    assert "assetType" in str(e.value)


def test_preview_happens_before_place_not_after(store, live, monkeypatch):
    c = _client(monkeypatch)
    _buy()
    assert len(c.previewed) == 1 and len(c.placed) == 1
    # Same payload previewed and placed — a preview of something else proves nothing.
    assert c.previewed[0] == c.placed[0]


# ---------------------------------------------------------------------------
# The order payload
# ---------------------------------------------------------------------------
def test_quantity_is_shares_not_contracts(store, live, monkeypatch):
    """An equity order counts SHARES. Sending 1 (lots) instead of 100 would be a
    real order for one share."""
    c = _client(monkeypatch)
    _buy(qty=100)
    leg = c.placed[0]["orderLegCollection"][0]
    assert leg["quantity"] == 100
    assert leg["instrument"] == {"symbol": "AAA", "assetType": "EQUITY"}
    assert leg["instruction"] == "BUY"


def test_the_limit_is_repriced_off_a_fresh_quote(store, live, monkeypatch):
    """Never the operator's snapshot — a ticket left open while the market moved
    must transmit at the current market, the same discipline the option path has."""
    monkeypatch.setattr(executor.data_handler, "latest_quote",
                        lambda t: {"price": 60.0, "bid": 59.90, "ask": 60.10})
    c = _client(monkeypatch)
    res = _buy()
    assert c.placed[0]["price"] == "60.00"          # fresh mid, not the staged 50
    assert res["repriced"] is True
    assert c.placed[0]["orderType"] == "LIMIT"


def test_a_crossed_or_missing_quote_refuses_rather_than_market_orders(store, live, monkeypatch):
    """An unpriced market order on a wide or fast tape is exactly the fill an
    operator cannot review, so it is not the fallback."""
    c = _client(monkeypatch)
    monkeypatch.setattr(executor.data_handler, "latest_quote",
                        lambda t: {"price": None, "bid": 50.10, "ask": 49.90})
    with pytest.raises(schwab_api.SchwabError, match="crossed"):
        _buy()
    monkeypatch.setattr(executor.data_handler, "latest_quote", lambda t: None)
    with pytest.raises(schwab_api.SchwabError, match="no fresh quote"):
        _buy()
    assert c.placed == []


# ---------------------------------------------------------------------------
# Mode, and the flag-off path
# ---------------------------------------------------------------------------
def test_a_placed_order_is_working_not_a_claimed_fill(store, live, monkeypatch):
    """It resolves through the existing poll -> fill -> commit lifecycle; it must
    not claim a fill at placement time."""
    _client(monkeypatch)
    res = _buy()
    assert res["status"] == "working" and res["mode"] == "live"
    assert res["order_id"] == "991" and res["shares"] == 100


def test_a_filled_equity_order_commits_as_live(store, live, monkeypatch):
    """It really did reach the broker, so reconciliation must check it — the
    flag-off path books "logged" precisely because it did not."""
    _client(monkeypatch)
    _buy()
    rec = dict(log.get_pending_order("991"))
    executor._commit_from_pending(rec, 50.05)
    ex = [e for e in log.load_state()["executions"] if e["action"] == "buy_shares"]
    assert ex and ex[-1]["mode"] == "live"


def test_with_the_flag_off_nothing_is_transmitted(store, monkeypatch):
    """The default, and it must be byte-identical to the old behaviour: preview
    for inspection, book as logged, transmit nothing."""
    assert not config.EQUITY_ORDER_PLACEMENT_ENABLED
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor.schwab_api, "configured", lambda: True)
    c = _client(monkeypatch)
    monkeypatch.setattr(executor, "_preview_equity_order",
                        lambda *a, **kw: {"order": {}, "transmitted": False})
    res = _buy()
    assert c.placed == [], "the flag is off; nothing may be transmitted"
    assert res["mode"] == "logged"


def test_demo_mode_refuses_even_with_every_flag_on(store, live, monkeypatch):
    """The broker-boundary backstop: a demo session trades a synthetic price feed,
    so placing a real order from one would trade the live account on fake prices."""
    c = _client(monkeypatch)
    monkeypatch.setattr(config, "demo_enabled", lambda: True)
    with pytest.raises(schwab_api.SchwabError, match="demo"):
        executor._place_equity_live({"qty": 100}, "AAA", "buy_shares", 1, 50.0, "schwab")
    assert c.placed == []
