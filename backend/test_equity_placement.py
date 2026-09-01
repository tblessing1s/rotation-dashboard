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


# ===========================================================================
# Unfilled -> cancelled -> retry. The loop that makes live trading usable.
# ===========================================================================
def test_a_second_order_is_refused_while_the_first_is_still_working(store, live, monkeypatch):
    """THE hazard behind 'let me try again'. `buy_shares` and `put_opened` were
    missing from `_LOCKED_INTENTS`, and for a non-locked action `_guard_resubmit`
    returns BEFORE the belt-and-suspenders pending-order check. So a retry while
    the first order was still working placed a SECOND live order, and both could
    fill — a double position on a strategy whose base leg is exactly 100 shares."""
    monkeypatch.undo()          # drop the fixture's _guard_resubmit stub
    monkeypatch.setattr(config, "EQUITY_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(executor.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(executor.data_handler, "latest_quote",
                        lambda t: {"price": 50.0, "bid": 49.98, "ask": 50.02})
    monkeypatch.setattr(executor, "_record_placement", lambda *a, **kw: None)
    c = _client(monkeypatch)

    _buy()                                    # first order -> working, pending
    with pytest.raises(executor.ResubmitLockedError, match="still pending"):
        _buy()
    assert len(c.placed) == 1, "the second order must never reach the broker"


def test_the_entry_intents_are_all_lock_gated():
    """`buy_shares` is the shares-primary replacement for `buy_leap`, which was
    gated from the start; `put_opened` is an entry too. An entry intent that is
    not gated has no resubmit protection at all."""
    for intent in ("buy_leap", "sell_short", "buy_shares", "put_opened"):
        assert intent in executor._LOCKED_INTENTS, intent


def test_a_cancelled_order_frees_the_retry(store, live, monkeypatch):
    """The point of cancelling an unfilled order: the operator reprices and sends
    again. CANCELED is terminal, so `check_resubmit` allows the next attempt."""
    import order_lifecycle as olc
    assert olc.is_terminal("CANCELED")
    allowed, _ = olc.check_resubmit(
        {"state": "CANCELED", "attempts": 1, "reconciled": True},
        config.MAX_RESUBMIT_ATTEMPTS)
    assert allowed is True


def test_retrying_is_capped_rather_than_endless():
    """Not-filling repeatedly usually means the price is wrong, not that one more
    identical order will work. The cap stops the loop and alerts."""
    import order_lifecycle as olc
    allowed, reason = olc.check_resubmit(
        {"state": "CANCELED", "attempts": config.MAX_RESUBMIT_ATTEMPTS,
         "reconciled": True},
        config.MAX_RESUBMIT_ATTEMPTS)
    assert allowed is False and "max resubmit attempts" in reason


def test_an_unconfirmed_cancel_blocks_the_retry():
    """If the broker never confirmed the cancel, the order may STILL be working.
    Placing another one then is how an operator ends up with two live orders."""
    import order_lifecycle as olc
    allowed, reason = olc.check_resubmit(
        {"state": olc.LOCKED_UNKNOWN, "attempts": 1, "reconciled": True},
        config.MAX_RESUBMIT_ATTEMPTS)
    assert allowed is False and "UNKNOWN" in reason


def test_the_placement_carries_the_fill_window_it_was_placed_under(store, live, monkeypatch):
    """The client must poll to the SAME deadline the operator configured. The 3s
    that was hard-coded in the frontend is far too short for a limit at the mid to
    fill on a normal book — nearly every order was cancelled before it had a
    chance, which would read as 'live trading does not work'."""
    _client(monkeypatch)
    res = _buy()
    assert res["fill_wait_ms"] == int(config.ORDER_FILL_WAIT_SECONDS * 1000)
    assert config.ORDER_FILL_WAIT_SECONDS >= 10, "3s cannot fill a mid-priced limit"
