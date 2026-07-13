"""Incident regression suite — roll-order hotfix (D1-D4).

Offline, mocked Schwab. Locks in the four defect fixes so the incident cannot recur:

  D1  price construction: tick rounding, exact Decimal serialization, direction
      assertion, and refuse-to-construct on a one-sided / stale quote.
  D2  truthful response: a valid ack never displays as failure; no-response /
      accepted-no-id is UNKNOWN, an explicit rejection carries the verbatim reason.
  D3  idempotency: a refresh/retry storm on one client_order_ref places ONE order.
  D4  orderId persistence: the id is written to the durable record FIRST, before any
      post-ack parsing can fault, and is recoverable when the ack carried none.

No live Schwab call is made anywhere. There are no captured incident response bodies
in the repo (see AUDIT_INCIDENT_HOTFIX.md), so the ack/rejection fixtures are
synthesized behind the mock; every Schwab schema assumption they encode is tagged
LIVE_VERIFY in IMPLEMENTATION_NOTES.
"""
import os
import re
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-hotfix-test-"))

import config            # noqa: E402
import data_handler      # noqa: E402
import executor          # noqa: E402
import logging_handler as log  # noqa: E402
import order_pricing as op     # noqa: E402
import schwab_api        # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    return tmp_path


def _seed(contracts=5):
    executor.execute({"action": "buy_leap", "ticker": "ON", "strike": 130,
                      "contracts": contracts, "execution_price": 3300, "stock_price": 145,
                      "override_reason": "test fixture"})
    executor.execute({"action": "sell_short", "ticker": "ON", "strike": 140.5,
                      "contracts": contracts, "premium_per_share": 6.0, "stock_price": 145,
                      "expiration": "2026-07-03"})


def _payload(ref="cor_1", **over):
    p = {"action": "roll_short", "ticker": "ON", "contracts": 5,
         "from_strike": 140.5, "close_price_per_share": 2.5, "from_option_symbol": "ON_OLD",
         "to_strike": 139.0, "premium_per_share": 5.0, "to_option_symbol": "ON_NEW",
         "to_expiration": "2026-07-10", "to_dte": 7, "stock_price": 142,
         "client_order_ref": ref}
    p.update(over)
    return p


def _q(mid, age_ms=None):
    return {"bid": round(mid - 0.05, 2), "ask": round(mid + 0.05, 2), "quoteTimeMs": age_ms}


_DEFAULT_QUOTES = {"ON_OLD": _q(2.5), "ON_NEW": _q(5.0)}


def _order(status="WORKING", close_px=None, open_px=None, qty=5):
    legs = [{"legId": 1, "instrument": {"symbol": "ON_OLD"}},
            {"legId": 2, "instrument": {"symbol": "ON_NEW"}}]
    act = []
    if close_px is not None:
        ex1 = {"legId": 1, "quantity": qty, "price": close_px}
        ex2 = {"legId": 2, "quantity": qty, "price": open_px}
        act = [{"executionLegs": [ex1, ex2]}]
    return {"status": status, "orderLegCollection": legs, "orderActivityCollection": act}


class FakeClient:
    def __init__(self, orders=None, quotes=None, submit_result=None, recent=None):
        self._orders = list(orders or [_order("WORKING")])
        self._quotes = dict(_DEFAULT_QUOTES if quotes is None else quotes)
        self._submit_result = submit_result
        self._recent = recent or []
        self.sent = []
        self.listed = 0

    def primary_account_hash(self):
        return "acct"

    def get_quotes(self, symbols):
        return {s: self._quotes.get(s) for s in symbols}

    def submit_order(self, account_hash, order):
        self.sent.append(order)
        if self._submit_result is not None:
            r = self._submit_result
            return r(len(self.sent)) if callable(r) else dict(r)
        return {"outcome": "accepted", "order_id": f"OID{len(self.sent)}"}

    def cancel_order(self, account_hash, order_id):
        return {"orderId": order_id, "canceled": True}

    def list_orders(self, account_hash, **kw):
        self.listed += 1
        return list(self._recent)

    def get_order(self, account_hash, order_id):
        return self._orders.pop(0) if len(self._orders) > 1 else self._orders[0]


def _go_live(monkeypatch, fake):
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: fake)


# ===========================================================================
# TEST 1 — Incident regression (D2/D4): a valid ack never shows as failure; the
# orderId is durable before any post-ack fault; an id-less ack resolves to WORKING.
# ===========================================================================
def test_orderid_persisted_before_postack_fault(store, monkeypatch):
    """A fault injected into post-ack parsing (save_pending_order) must NOT lose the
    orderId — it is written to the durable record FIRST (ORDERID_PERSIST_FIRST)."""
    _seed()
    fake = FakeClient(submit_result={"outcome": "accepted", "order_id": "OID77"})
    _go_live(monkeypatch, fake)

    boom = RuntimeError("post-ack parse fault")
    monkeypatch.setattr(log, "save_pending_order",
                        lambda *a, **k: (_ for _ in ()).throw(boom))

    with pytest.raises(RuntimeError):
        executor.execute(_payload(ref="cor_fault"))

    rec = log.get_order_submission("cor_fault")
    assert rec is not None, "durable submission record must survive the fault"
    assert rec["order_id"] == "OID77", "orderId persisted BEFORE the post-ack fault"
    assert rec["status"] == executor.SUB_WORKING


def test_idless_ack_is_unknown_not_failed_then_resolves_working(store, monkeypatch):
    """A 2xx ack with no order id is UNKNOWN ('confirming…'), never 'failed'; a
    subsequent status query recovers the id by recent-orders match and resolves to
    WORKING. The old behavior (failure displayed, no orderId) is unreachable here."""
    _seed()
    recent = [_order("WORKING")]
    recent[0]["orderId"] = "OID_RECOVERED"
    fake = FakeClient(orders=[_order("WORKING")],
                      submit_result={"outcome": "accepted", "order_id": None},
                      recent=recent)
    _go_live(monkeypatch, fake)

    res = executor.execute(_payload(ref="cor_noid"))
    # Truthful: UNKNOWN, not failed. One order was placed.
    assert res["status"] == "unknown"
    assert res["success"] is True  # not an error surface
    assert len(fake.sent) == 1
    rec = log.get_order_submission("cor_noid")
    assert rec["status"] == executor.SUB_UNKNOWN

    # Manual status check recovers the id and resolves to WORKING.
    out = executor.submission_status("cor_noid")
    assert out["status"] == "working"
    assert out["order_id"] == "OID_RECOVERED"
    assert fake.listed >= 1


def test_no_response_from_broker_is_unknown_never_failed(store, monkeypatch):
    """A lost/timed-out submission (no confirmed response) is UNKNOWN, never failed —
    the order may be live at Schwab. No exception is raised to the API layer."""
    _seed()
    fake = FakeClient(submit_result={"outcome": "unknown", "status_code": None,
                                     "detail": "no response from broker (request error): timeout"})
    _go_live(monkeypatch, fake)

    res = executor.execute(_payload(ref="cor_lost"))
    assert res["status"] == "unknown"
    assert "confirming" in (res.get("message") or "").lower()
    assert log.get_order_submission("cor_lost")["status"] == executor.SUB_UNKNOWN


# ===========================================================================
# TEST 2 — D1 price matrix (a-e)
# ===========================================================================
def test_d1a_offtick_mid_is_rounded_to_a_valid_tick(store, monkeypatch):
    """(a) An off-tick net (from mids landing between ticks) is rounded to a valid
    increment; the submitted price conforms. Legs >= $3 quote in $0.05 -> the net
    must be a multiple of $0.05."""
    _seed()
    # buyback mid 3.33, new-premium mid 5.71 -> raw net 2.38 -> nearest $0.05 = 2.40.
    fake = FakeClient(quotes={"ON_OLD": _q(3.33), "ON_NEW": _q(5.71)})
    _go_live(monkeypatch, fake)
    executor.execute(_payload(ref="cor_tick", close_price_per_share=3.33, premium_per_share=5.71))
    price = fake.sent[0]["price"]
    cents = round(float(price) * 100)
    assert cents % 5 == 0, f"submitted price {price} is off the $0.05 tick"


def test_d1b_price_serialized_as_exact_decimal(store, monkeypatch):
    """(b) The outgoing price string is an exact 2-dp decimal — no binary-float
    artifact like '2.3500000001'."""
    _seed()
    fake = FakeClient(quotes={"ON_OLD": _q(2.30), "ON_NEW": _q(4.65)})
    _go_live(monkeypatch, fake)
    executor.execute(_payload(ref="cor_dec", close_price_per_share=2.30, premium_per_share=4.65))
    price = fake.sent[0]["price"]
    assert re.fullmatch(r"\d+\.\d{2}", price), f"price {price!r} is not a clean 2-dp decimal"


def test_d1c_direction_contradiction_is_assertion_not_submission(store, monkeypatch):
    """(c) Staged legs imply a credit but the re-read quotes imply a debit — a
    direction contradiction is an assertion failure pre-submit, never a flipped
    order. Nothing is sent."""
    _seed()
    # Staged: buyback 2.5, premium 5.0 -> credit. Re-read flips it -> debit.
    fake = FakeClient(quotes={"ON_OLD": _q(5.0), "ON_NEW": _q(2.5)})
    _go_live(monkeypatch, fake)
    with pytest.raises(AssertionError):
        executor.execute(_payload(ref="cor_flip", close_price_per_share=2.5, premium_per_share=5.0))
    assert fake.sent == [], "no order may be transmitted on a direction contradiction"
    assert log.get_order_submission("cor_flip") is None


def test_d1d_one_sided_buyback_quote_refuses_naming_the_leg(store, monkeypatch):
    """(d) A one-sided quote on the buy-to-close leg refuses construction with a
    reason naming that leg. Nothing is sent."""
    _seed()
    fake = FakeClient(quotes={"ON_OLD": {"bid": None, "ask": 2.6, "quoteTimeMs": None},
                              "ON_NEW": _q(5.0)})
    _go_live(monkeypatch, fake)
    with pytest.raises(ValueError) as ei:
        executor.execute(_payload(ref="cor_1sided"))
    msg = str(ei.value)
    assert "Refusing to construct" in msg
    assert "buy-to-close" in msg and "ON_OLD" in msg
    assert fake.sent == []


def test_d1e_stale_quote_refuses(store, monkeypatch):
    """(e) A quote older than QUOTE_MAX_AGE_FOR_ORDER_SECONDS refuses construction."""
    _seed()
    fake = FakeClient(quotes={"ON_OLD": _q(2.5, age_ms=0), "ON_NEW": _q(5.0, age_ms=0)})
    _go_live(monkeypatch, fake)
    with pytest.raises(ValueError) as ei:
        executor.execute(_payload(ref="cor_stale"))
    assert "stale" in str(ei.value).lower()
    assert fake.sent == []


# ===========================================================================
# TEST 3 — D3 idempotency: a refresh/retry storm places exactly ONE order.
# ===========================================================================
def test_d3_refresh_storm_places_exactly_one_order(store, monkeypatch):
    _seed()
    fake = FakeClient(orders=[_order("WORKING")])
    _go_live(monkeypatch, fake)

    results = [executor.execute(_payload(ref="cor_storm")) for _ in range(5)]
    assert len(fake.sent) == 1, "5 submits on one ref must place exactly one order"
    # Every call returns a truthful working state; the repeats are idempotent replays.
    assert results[0]["status"] == "working"
    assert all(r.get("order_id") == results[0]["order_id"] for r in results)
    assert any(r.get("idempotent") for r in results[1:])


# ===========================================================================
# TEST 4 — Truthful rejection: an explicit Schwab rejection shows the verbatim
# reason and does NOT auto-retry.
# ===========================================================================
def test_d2_explicit_rejection_shows_verbatim_reason_no_retry(store, monkeypatch):
    _seed()
    reason = "REJECTED: buying power exceeded on account ...1234"
    fake = FakeClient(submit_result={"outcome": "rejected", "status_code": 400, "reason": reason})
    _go_live(monkeypatch, fake)

    res = executor.execute(_payload(ref="cor_rej"))
    assert res["status"] == "rejected"
    assert res["reason"] == reason  # verbatim
    assert res["success"] is False
    assert len(fake.sent) == 1, "a rejection must not trigger an automatic resubmit"
    assert log.get_order_submission("cor_rej")["broker_reason"] == reason


def test_submit_order_maps_http_outcomes(store):
    """The client's structured submit_order tells an explicit rejection (400/422)
    apart from an ambiguous no-confirmation (auth/5xx/network) — the D2 hinge."""
    class _Resp:
        def __init__(self, code, text="", loc=None):
            self.status_code = code
            self.text = text
            self.headers = {"Location": loc} if loc else {}

    client = schwab_api.SchwabClient()
    client._auth_headers = lambda extra=None: {}  # no live token in tests
    import schwab_api as sa

    def fake_post(code, text="", loc=None):
        return lambda *a, **k: _Resp(code, text, loc)

    # 2xx with Location -> accepted + id
    sa.requests.post = fake_post(201, loc="https://x/accounts/h/orders/OID9")
    r = client.submit_order("h", {})
    assert r == {"outcome": "accepted", "order_id": "OID9", "location": "https://x/accounts/h/orders/OID9"}
    # 2xx no Location -> accepted, id None (UNKNOWN downstream, never failed)
    sa.requests.post = fake_post(200)
    assert client.submit_order("h", {})["outcome"] == "accepted"
    assert client.submit_order("h", {})["order_id"] is None
    # 400 -> explicit rejection with verbatim body
    sa.requests.post = fake_post(400, text="no can do")
    r = client.submit_order("h", {})
    assert r["outcome"] == "rejected" and r["reason"] == "no can do"
    # 500 -> UNKNOWN, not a rejection
    sa.requests.post = fake_post(500, text="server error")
    assert client.submit_order("h", {})["outcome"] == "unknown"
    # network error -> UNKNOWN with no status code
    def boom(*a, **k):
        raise sa.requests.exceptions.ConnectionError("reset")
    sa.requests.post = boom
    r = client.submit_order("h", {})
    assert r["outcome"] == "unknown" and r["status_code"] is None


# ===========================================================================
# TEST 5 — Cancel truth: a fill that beat the cancel displays as FILLED (cancel lost).
# ===========================================================================
def test_d2_cancel_that_races_a_fill_displays_filled(store, monkeypatch):
    _seed()
    # Place a working roll, then a fill lands: the order reads FILLED at cancel time.
    filled = _order("FILLED", close_px=2.4, open_px=5.0)
    fake = FakeClient(orders=[filled])
    _go_live(monkeypatch, fake)

    placed = executor.execute(_payload(ref="cor_cancel"))
    order_id = placed["order_id"]
    out = executor.cancel_order(order_id)
    # The fill beat the cancel — final state is FILLED, not canceled.
    assert out["status"] == "filled"
    assert log.get_order_submission("cor_cancel")["status"] == executor.SUB_FILLED


# ===========================================================================
# Pure-function guards (order_pricing) — the F1 primitives in isolation.
# ===========================================================================
def test_round_to_tick_pure():
    assert op.round_to_tick("2.3500000001") == op.Decimal("2.35")
    assert op.round_to_tick(3.37) == op.Decimal("3.35")   # $0.05 tick above $3
    assert op.round_to_tick(3.38) == op.Decimal("3.40")
    assert op.round_to_tick(-2.37) == op.Decimal("-2.37")  # $0.01 tick below $3
    assert op.format_price(op.Decimal("-2.5")) == "2.50"


def test_net_credit_debit_single_source_of_direction():
    assert op.net_credit_debit(2.5, 5.0) == (op.Decimal("2.50"), "NET_CREDIT")
    assert op.net_credit_debit(5.0, 2.5) == (op.Decimal("2.50"), "NET_DEBIT")
    with pytest.raises(AssertionError):
        op.assert_direction("NET_DEBIT", "NET_CREDIT")
    op.assert_direction("NET_CREDIT", "NET_CREDIT")   # agrees -> no raise
    op.assert_direction("NET_DEBIT", None)            # no intent -> no raise


def test_validate_roll_quotes_reasons():
    good = _q(2.5)
    assert op.validate_roll_quotes(good, good, close_symbol="A", new_symbol="B", now_ms=None) == []
    r = op.validate_roll_quotes({"bid": 0, "ask": 2.6}, good, close_symbol="A", new_symbol="B")
    assert r and "A" in r[0]
    r = op.validate_roll_quotes(None, good, close_symbol="A", new_symbol="B")
    assert r and "no quote" in r[0]
