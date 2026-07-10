"""Order lifecycle: entry order-type correctness + broker-side cancel/retry state
machine. Everything here runs OFFLINE — the broker is a mock and the clock is
driven to ~0 (poll intervals set to 0), so no real order is ever placed and no
wall-clock waiting happens. Covers the pure state machine (order_lifecycle.py) and
the ten fixture-driven lifecycle branches from the implementation spec.
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-lifecycle-test-"))

import config            # noqa: E402
import data_handler      # noqa: E402
import executor          # noqa: E402
import logging_handler as log  # noqa: E402
import order_lifecycle as olc  # noqa: E402
import schwab_api        # noqa: E402


# ===========================================================================
# Pure state machine (no I/O)
# ===========================================================================
def test_map_broker_status_clean_paths():
    assert olc.map_broker_status("WORKING") == olc.WORKING
    assert olc.map_broker_status("QUEUED") == olc.WORKING            # unknown-live -> working
    assert olc.map_broker_status("FILLED") == olc.FILLED
    assert olc.map_broker_status("CANCELED") == olc.CANCELED
    assert olc.map_broker_status("REJECTED") == olc.REJECTED
    assert olc.map_broker_status("EXPIRED") == olc.EXPIRED
    assert olc.map_broker_status("PENDING_CANCEL") == olc.PENDING_CANCEL


def test_map_broker_status_cancel_requested_disambiguates():
    # A fill that lands AFTER we asked to cancel is a fill-DURING-cancel.
    assert olc.map_broker_status("FILLED", cancel_requested=True) == olc.FILLED_DURING_CANCEL
    # A "canceled" with a partial fill is an unbalanced position, never clean.
    assert olc.map_broker_status("CANCELED", filled_qty=2, ordered_qty=5) == olc.PARTIAL_FILL_CANCELED
    # A "canceled" that actually fully filled is a fill.
    assert olc.map_broker_status("CANCELED", filled_qty=5, ordered_qty=5) == olc.FILLED
    assert olc.map_broker_status("CANCELED", filled_qty=5, ordered_qty=5,
                                 cancel_requested=True) == olc.FILLED_DURING_CANCEL
    # An expired DAY order that partially filled is unbalanced too.
    assert olc.map_broker_status("EXPIRED", filled_qty=1, ordered_qty=3) == olc.PARTIAL_FILL_CANCELED
    # Still working after we asked to cancel -> PENDING_CANCEL.
    assert olc.map_broker_status("WORKING", cancel_requested=True) == olc.PENDING_CANCEL


def test_is_terminal():
    for s in (olc.FILLED, olc.CANCELED, olc.REJECTED, olc.EXPIRED,
              olc.FILLED_DURING_CANCEL, olc.PARTIAL_FILL_CANCELED):
        assert olc.is_terminal(s)
    for s in (olc.SUBMITTED, olc.WORKING, olc.CANCEL_REQUESTED, olc.PENDING_CANCEL,
              olc.LOCKED_UNKNOWN, None):
        assert not olc.is_terminal(s)


def test_check_resubmit_invariant():
    MAX = 3
    assert olc.check_resubmit(None, MAX)[0] is True                      # first ever
    assert olc.check_resubmit({"state": olc.WORKING, "attempts": 1}, MAX)[0] is False   # still live
    assert olc.check_resubmit({"state": olc.PENDING_CANCEL, "attempts": 1}, MAX)[0] is False
    assert olc.check_resubmit({"state": olc.LOCKED_UNKNOWN, "attempts": 1}, MAX)[0] is False
    assert olc.check_resubmit({"state": olc.PARTIAL_FILL_CANCELED, "attempts": 1}, MAX)[0] is False
    assert olc.check_resubmit({"state": olc.FILLED_DURING_CANCEL, "attempts": 1}, MAX)[0] is False
    # Clean terminal + reconciled + attempts left -> allowed.
    assert olc.check_resubmit({"state": olc.CANCELED, "attempts": 1, "reconciled": True}, MAX)[0] is True
    # Terminal but not yet reconciled -> blocked.
    assert olc.check_resubmit({"state": olc.FILLED, "attempts": 1, "reconciled": False}, MAX)[0] is False
    # Attempt cap reached -> blocked.
    assert olc.check_resubmit({"state": olc.CANCELED, "attempts": 3, "reconciled": True}, MAX)[0] is False


# ===========================================================================
# Live lifecycle (mocked broker + mocked clock)
# ===========================================================================
class FakeSchwab:
    """Mock Schwab client with a mutable status + optional per-leg fills, filled
    quantity, and an injectable cancel hook (to model DELETE ack/refuse/race)."""

    def __init__(self, status="WORKING", fill_price=None, filled_qty=None, ordered_qty=None):
        self._status = status
        self._fill_price = fill_price
        self._filled_qty = filled_qty
        self._ordered_qty = ordered_qty
        self.placed = []
        self.canceled = []
        self.cancel_hook = None
        self._seq = 0

    def primary_account_hash(self):
        return "HASH"

    def place_order(self, account_hash, order):
        self._seq += 1
        self.placed.append((account_hash, order))
        return {"orderId": f"ORD{self._seq}"}

    def get_order(self, account_hash, order_id):
        out = {"status": self._status}
        if self._fill_price is not None:
            out["orderActivityCollection"] = [{"executionLegs": [{"price": self._fill_price}]}]
        if self._filled_qty is not None:
            out["filledQuantity"] = self._filled_qty
        if self._ordered_qty is not None:
            out["quantity"] = self._ordered_qty
        return out

    def cancel_order(self, account_hash, order_id):
        self.canceled.append((account_hash, order_id))
        if self.cancel_hook is not None:
            return self.cancel_hook(account_hash, order_id)
        self._status = "CANCELED"  # a normal cancel lands terminal
        return {"canceled": True}


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A live, broker-configured session with the cancel-confirm clock driven to 0
    so the async-cancel poll loops run instantly."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    # Mocked clock: no real waiting. TIMEOUT>0 so the confirm loop runs one poll.
    monkeypatch.setattr(executor, "CANCEL_CONFIRM_POLL_S", 0.0)
    monkeypatch.setattr(executor, "CANCEL_CONFIRM_TIMEOUT_S", 0.05)
    monkeypatch.setattr(config, "CANCEL_POLL_INTERVAL_SEC", 0.0)

    def _use(fake):
        monkeypatch.setattr(data_handler, "client", lambda: fake)
        return fake
    return _use


def _sell_payload(**over):
    p = {"action": "sell_short", "ticker": "ON", "strike": 139.5, "contracts": 5,
         "premium_per_share": 6.0, "stock_price": 142, "expiration": "2026-07-10"}
    p.update(over)
    return p


def _open_payload(**over):
    p = {"action": "open_position_atomic", "ticker": "XLK", "contracts": 5, "stock_price": 184.0,
         "strike": 137.5, "execution_price": 5325, "expiration": "2027-01-15", "dte": 193,
         "option_symbol": "XLK_LEAP",
         "short_strike": 181, "short_premium_per_share": 5.40, "short_expiration": "2026-07-10",
         "short_dte": 4, "short_option_symbol": "XLK_SHORT",
         "circuit_breaker_price": 178.58, "override_reason": "test"}
    p.update(over)
    return p


def _alert_types(ticker=None):
    logl = log.load_state().get("alerts", {}).get("log", [])
    return [a.get("type") for a in logl]


def _lock(ticker, action):
    return log.get_order_lock(executor._intent_key(ticker, action))


# --- Case 10: entry multi-leg payload golden test ---------------------------
def test_entry_multileg_payload_golden(live):
    fake = live(FakeSchwab(status="WORKING"))
    res = executor.execute(_open_payload())
    assert res["status"] == "working"
    sent = fake.placed[0][1]
    # net = short 5.40 − LEAP 53.25 = −47.85 -> NET_DEBIT 47.85; CUSTOM/DAY from config.
    assert sent == {
        "orderType": "NET_DEBIT",
        "session": "NORMAL",
        "price": "47.85",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "orderLegCollection": [
            {"instruction": "BUY_TO_OPEN", "quantity": 5,
             "instrument": {"symbol": "XLK_LEAP", "assetType": "OPTION"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 5,
             "instrument": {"symbol": "XLK_SHORT", "assetType": "OPTION"}},
        ],
    }


def test_entry_strategy_type_is_config_driven(live, monkeypatch):
    # The entry honors the provenance-tagged constant (LIVE-VERIFY: DIAGONAL vs CUSTOM).
    monkeypatch.setattr(config, "ENTRY_COMPLEX_STRATEGY_TYPE", "DIAGONAL")
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_open_payload())
    assert fake.placed[0][1]["complexOrderStrategyType"] == "DIAGONAL"


# --- Case 1: clean cancel -> resubmit allowed -------------------------------
def test_clean_cancel_then_resubmit_allowed(live):
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())
    out = executor.cancel_order("ORD1")
    assert out["status"] == "canceled"
    assert fake.canceled == [("HASH", "ORD1")]
    assert "ORD1" not in log.load_state()["pending_orders"]
    lk = _lock("ON", "sell_short")
    assert lk["state"] == olc.CANCELED and lk["reconciled"] is True
    # Order state is DERIVED from the append-only event log.
    assert log.load_state()["order_state"]["ORD1"]["state"] == olc.CANCELED
    # A fresh order for the same intent is now allowed (no raise).
    executor._guard_resubmit("ON", "sell_short")


# --- Case 2: fill during cancel -> reconcile, no resubmit, alert -------------
def test_fill_during_cancel_reconciles_and_blocks_resubmit(live):
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())

    # DELETE is acknowledged but the order races to a FILL before the cancel takes.
    def ack_then_fill(account_hash, order_id):
        fake._status, fake._fill_price = "FILLED", 5.0
        return {"canceled": True}
    fake.cancel_hook = ack_then_fill

    out = executor.cancel_order("ORD1")
    assert out["status"] == "filled"                       # the fill was reconciled
    pos = log.find_position(log.load_state(), "ON")
    assert len(pos["short_calls"]) == 1                    # position is LIVE
    assert "ORDER_FILLED_DURING_CANCEL" in _alert_types()  # high-priority alert fired
    assert _lock("ON", "sell_short")["state"] == olc.FILLED_DURING_CANCEL
    with pytest.raises(executor.ResubmitLockedError):      # do NOT resubmit
        executor._guard_resubmit("ON", "sell_short")


# --- Case 3: partial fill during cancel -> defensive review, blocked ---------
def test_partial_fill_on_cancel_freezes_and_blocks(live):
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_open_payload())

    # DELETE lands the order CANCELED but 2 of 5 already filled — unbalanced.
    def cancel_partial(account_hash, order_id):
        fake._status, fake._filled_qty, fake._ordered_qty = "CANCELED", 2, 5
        return {"canceled": True}
    fake.cancel_hook = cancel_partial

    out = executor.cancel_order("ORD1")
    assert out["status"] == "partial_fill_canceled" and out["frozen"] is True
    pos = log.find_position(log.load_state(), "XLK")
    assert pos["needs_review"] is True
    # Trips the delta-coverage guardrail review + records the distinct coded state.
    assert "PARTIAL_FILL_CANCELED" in pos["review"]["classifications"]
    assert "DELTA_COVERAGE_CHECK" in pos["review"]["classifications"]
    assert "ORDER_PARTIAL_FILL_CANCELED" in _alert_types()
    assert _lock("XLK", "open_position_atomic")["state"] == olc.PARTIAL_FILL_CANCELED
    with pytest.raises(executor.ResubmitLockedError):
        executor._guard_resubmit("XLK", "open_position_atomic")


# --- Case 4: DELETE errors but order already FILLED -> reconcile as fill -----
def test_cancel_delete_error_but_already_filled_reconciles(live):
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())

    # The order filled just before our DELETE, so the DELETE is refused; on re-check
    # the broker shows FILLED — we must settle the fill, not lose it.
    def refuse(account_hash, order_id):
        fake._status, fake._fill_price = "FILLED", 5.0
        raise schwab_api.SchwabError("HTTP 400 order not cancelable (already filled)")
    fake.cancel_hook = refuse

    out = executor.cancel_order("ORD1")
    assert out["status"] == "filled"
    assert len(log.find_position(log.load_state(), "ON")["short_calls"]) == 1
    assert "ORD1" not in log.load_state()["pending_orders"]


# --- Case 5: DELETE errors, still WORKING, exhausted -> alert + hard lock ----
def test_cancel_delete_error_still_working_exhausts_to_hard_lock(live, monkeypatch):
    monkeypatch.setattr(config, "CANCEL_POLL_MAX_ATTEMPTS", 3)
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())

    attempts = []
    def always_refuse(account_hash, order_id):
        attempts.append(order_id)  # order stays WORKING throughout
        raise schwab_api.SchwabError("HTTP 500 broker unavailable")
    fake.cancel_hook = always_refuse

    with pytest.raises(schwab_api.SchwabError):
        executor.cancel_order("ORD1")
    assert len(attempts) == 3                                  # retried per poll policy
    assert "ORD1" in log.load_state()["pending_orders"]        # never forgotten
    assert _lock("ON", "sell_short")["state"] == olc.LOCKED_UNKNOWN
    assert "ORDER_STATE_UNKNOWN" in _alert_types()
    # No resubmit EVER while the broker state is unknown.
    with pytest.raises(executor.ResubmitLockedError):
        executor._guard_resubmit("ON", "sell_short")


# --- Case 6: REJECTED on submit -> terminal, resubmit allowed ----------------
def test_rejected_is_terminal_and_allows_resubmit(live):
    fake = live(FakeSchwab(status="REJECTED"))
    executor.execute(_sell_payload())
    out = executor.order_status("ORD1")
    assert out["status"] == "rejected"
    assert "ORD1" not in log.load_state()["pending_orders"]
    lk = _lock("ON", "sell_short")
    assert lk["state"] == olc.REJECTED and lk["reconciled"] is True
    assert log.load_state()["order_state"]["ORD1"]["state"] == olc.REJECTED
    executor._guard_resubmit("ON", "sell_short")  # allowed, no raise


# --- Case 7: crash sim -> startup reconciliation resolves before new orders --
def test_startup_reconciliation_resolves_orphaned_order(live):
    # Simulate a crash: a WORKING order + its lock persisted, then restart. The
    # broker now shows it FILLED. Startup reconcile must settle it before trading.
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())
    assert "ORD1" in log.load_state()["pending_orders"]

    fake._status, fake._fill_price = "FILLED", 5.0            # it actually filled while we were down
    summary = executor.reconcile_pending_orders_on_startup()
    assert summary == {"reconciled": 1, "pending": 1}
    assert "ORD1" not in log.load_state()["pending_orders"]   # resolved
    assert len(log.find_position(log.load_state(), "ON")["short_calls"]) == 1
    executor._guard_resubmit("ON", "sell_short")              # position freed


def test_startup_reconciliation_hard_locks_unreachable_order(live):
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())

    def boom(account_hash, order_id):
        raise schwab_api.SchwabError("broker unreachable")
    fake.get_order = boom
    executor.reconcile_pending_orders_on_startup()
    assert _lock("ON", "sell_short")["state"] == olc.LOCKED_UNKNOWN
    with pytest.raises(executor.ResubmitLockedError):
        executor._guard_resubmit("ON", "sell_short")


# --- Case 8: resubmit while lock held -> blocked ----------------------------
def test_resubmit_while_working_lock_held_is_blocked(live):
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())                         # ORD1 now WORKING
    # A second order for the same intent while the first is live must be refused.
    with pytest.raises(executor.ResubmitLockedError):
        executor.execute(_sell_payload())
    assert len(fake.placed) == 1                              # never hit the broker again


# --- Case 9: MAX_RESUBMIT_ATTEMPTS exhausted -> alert, stop ------------------
def test_max_resubmit_attempts_exhausts_and_alerts(live, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESUBMIT_ATTEMPTS", 2)
    fake = live(FakeSchwab(status="WORKING"))

    # Attempt 1: place then cleanly cancel.
    executor.execute(_sell_payload())
    executor.cancel_order("ORD1")
    fake._status = "WORKING"                                  # broker ready for the next
    # Attempt 2: place then cancel — now at the cap.
    executor.execute(_sell_payload())
    executor.cancel_order("ORD2")
    fake._status = "WORKING"
    # Attempt 3: blocked, and the exhaustion is alerted.
    with pytest.raises(executor.ResubmitLockedError):
        executor.execute(_sell_payload())
    assert "ORDER_RESUBMIT_EXHAUSTED" in _alert_types()
    assert len(fake.placed) == 2                              # the 3rd never transmitted


# --- Every transition is an append-only event -------------------------------
def test_transitions_are_appended_and_derived(live):
    fake = live(FakeSchwab(status="WORKING"))
    executor.execute(_sell_payload())
    executor.cancel_order("ORD1")
    events = [e for e in log.load_state()["order_events"] if e["order_id"] == "ORD1"]
    seq = [e["new_state"] for e in events]
    # SUBMITTED->WORKING (placement), WORKING->CANCEL_REQUESTED, ->CANCELED.
    assert seq == [olc.WORKING, olc.CANCEL_REQUESTED, olc.CANCELED]
    assert all(e.get("raw_status") for e in events)          # raw broker status carried
    assert log.load_state()["order_state"]["ORD1"]["state"] == olc.CANCELED
