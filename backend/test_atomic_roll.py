"""Atomic spread roll: the weekly short-call roll transmits as ONE Schwab
two-leg NET_CREDIT/NET_DEBIT order (buy-to-close old short + sell-to-open new
short) that fills as a unit or not at all.

Covers order construction, the live lifecycle (full / partial / canceled /
rejected / leg-imbalance), per-leg vs proportional allocation, the paper single-
net-crossing model, roll-ledger equivalence between a legacy legged roll and an
atomic roll, and the forward-only migration.
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-roll-test-"))

import config            # noqa: E402
import data_handler      # noqa: E402
import executor          # noqa: E402
import logging_handler as log  # noqa: E402
import schwab_api        # noqa: E402
import slippage          # noqa: E402
import alerts            # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)  # paper by default
    return tmp_path


def _seed_position(contracts=5, strike=140.5, premium=6.0):
    """A position holding a LEAP + one open short, ready to roll (paper)."""
    executor.execute({"action": "buy_leap", "ticker": "ON", "strike": 130,
                      "contracts": contracts, "execution_price": 3300, "stock_price": 145,
                      "override_reason": "test fixture"})
    executor.execute({"action": "sell_short", "ticker": "ON", "strike": strike,
                      "contracts": contracts, "premium_per_share": premium, "stock_price": 145,
                      "expiration": "2026-07-03"})


def _roll_payload(**over):
    p = {
        "action": "roll_short", "ticker": "ON", "contracts": 5,
        "from_strike": 140.5, "close_price_per_share": 2.5, "from_option_symbol": "ON_OLD",
        "to_strike": 139.0, "premium_per_share": 5.0, "to_option_symbol": "ON_NEW",
        "to_expiration": "2026-07-10", "to_dte": 7, "stock_price": 142,
        # Idempotency key the frontend now generates when the roll is staged.
        "client_order_ref": "cor_test_roll",
    }
    p.update(over)
    return p


def _q(mid):
    """A two-sided, fresh option quote centered on ``mid`` (no timestamp -> not aged
    out; the stale-quote path is exercised explicitly elsewhere)."""
    return {"bid": round(mid - 0.05, 2), "ask": round(mid + 0.05, 2), "quoteTimeMs": None}


# Default leg quotes: mids reproduce the default _roll_payload (buyback 2.5, new
# premium 5.0 -> +2.5 net credit), so the backend's re-read net equals the staged one.
_DEFAULT_QUOTES = {"ON_OLD": _q(2.5), "ON_NEW": _q(5.0)}


class _FakeClient:
    """A Schwab client stub. ``orders`` is a list consumed one get_order() call
    at a time (repeating the last), so multi-poll (partial) flows are scriptable.

    ``quotes`` maps option symbol -> quote dict for the F1 pre-submit quote re-read
    (defaults to two-sided fresh quotes matching the default roll). ``submit_result``
    scripts the structured submit_order outcome (defaults to accepted with an OID)."""
    def __init__(self, orders, quotes=None, submit_result=None):
        self._orders = list(orders)
        self._quotes = dict(_DEFAULT_QUOTES if quotes is None else quotes)
        self._submit_result = submit_result
        self.sent = []
        self.listed = 0

    def primary_account_hash(self):
        return "acct"

    def get_quotes(self, symbols):
        return {s: self._quotes.get(s) for s in symbols}

    def place_order(self, account_hash, order):
        self.sent.append(order)
        return {"orderId": f"OID{len(self.sent)}"}

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
        return []

    def get_order(self, account_hash, order_id):
        if len(self._orders) > 1:
            return self._orders.pop(0)
        return self._orders[0]


def _filled_order(close_px=2.4, open_px=5.1, qty=5, status="FILLED",
                  close_sym="ON_OLD", open_sym="ON_NEW", with_price=True):
    legs = [{"legId": 1, "instrument": {"symbol": close_sym}},
            {"legId": 2, "instrument": {"symbol": open_sym}}]
    ex1 = {"legId": 1, "quantity": qty}
    ex2 = {"legId": 2, "quantity": qty}
    if with_price:
        ex1["price"] = close_px
        ex2["price"] = open_px
    return {"status": status, "statusDescription": "",
            "orderLegCollection": legs,
            "orderActivityCollection": [{"executionLegs": [ex1, ex2]}]}


def _go_live(monkeypatch, fake):
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: fake)


# ---------------------------------------------------------------------------
# R1 — order construction
# ---------------------------------------------------------------------------
def test_roll_order_is_one_net_credit_two_leg_ticket(store, monkeypatch):
    _seed_position()
    fake = _FakeClient([_filled_order()])
    _go_live(monkeypatch, fake)

    res = executor.execute(_roll_payload())
    assert res["status"] == "working" and len(res["option_symbols"]) == 2
    order = fake.sent[0]
    # premium 5.0 − buyback 2.5 = +2.5 net -> a CREDIT.
    assert order["orderType"] == "NET_CREDIT"
    assert order["price"] == "2.50"
    assert order["duration"] == config.ROLL_ORDER_DURATION
    assert order["complexOrderStrategyType"] == config.ROLL_COMPLEX_STRATEGY_TYPE
    legs = order["orderLegCollection"]
    assert [l["instruction"] for l in legs] == ["BUY_TO_CLOSE", "SELL_TO_OPEN"]
    assert [l["quantity"] for l in legs] == [5, 5]
    assert [l["instrument"]["symbol"] for l in legs] == ["ON_OLD", "ON_NEW"]


def test_roll_that_costs_money_is_net_debit(store, monkeypatch):
    _seed_position()
    # Re-read leg mids: buyback 5.0 > new premium 2.5 -> net −2.5 -> a DEBIT.
    fake = _FakeClient([_filled_order()], quotes={"ON_OLD": _q(5.0), "ON_NEW": _q(2.5)})
    _go_live(monkeypatch, fake)
    executor.execute(_roll_payload(close_price_per_share=5.0, premium_per_share=2.5))
    order = fake.sent[0]
    assert order["orderType"] == "NET_DEBIT"
    assert order["price"] == "2.50"


# ---------------------------------------------------------------------------
# R2 — execution logging & allocation
# ---------------------------------------------------------------------------
def test_full_fill_books_two_linked_legs_with_broker_allocation(store, monkeypatch):
    _seed_position()
    fake = _FakeClient([_filled_order(close_px=2.4, open_px=5.1)])
    _go_live(monkeypatch, fake)

    placed = executor.execute(_roll_payload())
    # Nothing committed until the fill confirms.
    assert len(log.load_state()["executions"]) == 2  # buy_leap + sell_short only
    done = executor.order_status(placed["order_id"])

    assert done["status"] == "filled"
    execs = done["executions"]
    assert [e["roll_leg"] for e in execs] == ["close", "open"]
    # Shared roll_group_id (== roll_id) across both legs.
    assert len({e["roll_group_id"] for e in execs}) == 1
    assert all(e["roll_group_id"] == e["roll_id"] for e in execs)
    assert done["roll_group_id"] == execs[0]["roll_group_id"]
    # Booked at the REAL per-leg fills, marked as broker-reported.
    assert all(e["roll_alloc_method"] == "broker_per_leg" for e in execs)
    close = next(e for e in execs if e["roll_leg"] == "close")
    open_ = next(e for e in execs if e["roll_leg"] == "open")
    assert close["close_price_per_share"] == pytest.approx(2.4)
    assert open_["premium_per_share"] == pytest.approx(5.1)
    # Position ends with a single short at the new strike.
    pos = log.find_position(log.load_state(), "ON")
    assert len(pos["short_calls"]) == 1 and pos["short_calls"][0]["strike"] == 139.0


def test_full_fill_falls_back_to_proportional_allocation(store, monkeypatch):
    _seed_position()
    # FILLED but Schwab reports no per-leg prices -> proportional-to-mid split.
    fake = _FakeClient([_filled_order(with_price=False)])
    _go_live(monkeypatch, fake)

    placed = executor.execute(_roll_payload())
    done = executor.order_status(placed["order_id"])
    assert done["status"] == "filled"
    assert all(e["roll_alloc_method"] == "proportional_to_mid" for e in done["executions"])
    # net_limit (2.5) == reference net (5.0 − 2.5) so the split recovers the mids.
    close = next(e for e in done["executions"] if e["roll_leg"] == "close")
    open_ = next(e for e in done["executions"] if e["roll_leg"] == "open")
    assert close["close_price_per_share"] == pytest.approx(2.5)
    assert open_["premium_per_share"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# R3 — partial fills & leg imbalance
# ---------------------------------------------------------------------------
def test_partial_then_full_fill_keeps_remainder_pending(store, monkeypatch):
    _seed_position(contracts=5)
    # Poll 1: 2 of 5 whole units filled on both legs (still working).
    # Poll 2: all 5 filled.
    partial = _filled_order(qty=2, status="WORKING")
    full = _filled_order(qty=5, status="FILLED")
    fake = _FakeClient([partial, full])
    _go_live(monkeypatch, fake)

    placed = executor.execute(_roll_payload())
    oid = placed["order_id"]

    first = executor.order_status(oid)
    assert first["status"] == "partially_filled"
    assert first["filled"] == 2 and first["remaining"] == 3
    # Two legs booked for the 2 filled units; order still pending.
    assert log.get_pending_order(oid) is not None
    booked = [e for e in log.load_state()["executions"] if e.get("roll_leg")]
    assert {e["contracts"] for e in booked} == {2}

    second = executor.order_status(oid)
    assert second["status"] == "filled"
    assert log.get_pending_order(oid) is None
    # Both partials share ONE roll_group_id.
    gids = {e["roll_group_id"] for e in log.load_state()["executions"] if e.get("roll_leg")}
    assert len(gids) == 1
    # Position: old short fully closed, new short holds the full 5 (booked as two
    # sell legs from the two partial units — the aggregate at the new strike is 5).
    pos = log.find_position(log.load_state(), "ON")
    new_legs = [sc for sc in pos["short_calls"] if sc["strike"] == 139.0]
    assert sum(sc["contracts"] for sc in new_legs) == 5
    assert all(sc["strike"] == 139.0 for sc in pos["short_calls"])  # old short gone


def test_leg_imbalance_freezes_and_writes_no_execution(store, monkeypatch):
    _seed_position(contracts=5)
    # Terminal state but the legs disagree: close filled 5, open filled 0.
    imbalanced = _filled_order(status="FILLED")
    imbalanced["orderActivityCollection"] = [{"executionLegs": [
        {"legId": 1, "quantity": 5, "price": 2.4}]}]  # only the close leg
    fake = _FakeClient([imbalanced])
    _go_live(monkeypatch, fake)

    placed = executor.execute(_roll_payload())
    before = len(log.load_state()["executions"])
    res = executor.order_status(placed["order_id"])

    assert res["status"] == "leg_imbalance" and res["frozen"] is True
    assert res["close_filled"] == 5 and res["open_filled"] == 0
    # No execution written, position frozen, pending cleared.
    assert len(log.load_state()["executions"]) == before
    pos = log.find_position(log.load_state(), "ON")
    assert pos["needs_review"] is True
    assert "ROLL_LEG_IMBALANCE" in pos["review"]["classifications"]
    assert log.get_pending_order(placed["order_id"]) is None
    # A CRITICAL alert is raised for the frozen position.
    fired = alerts.check_roll_leg_imbalance(log.load_state())
    assert len(fired) == 1 and fired[0]["type"] == "ROLL_LEG_IMBALANCE"
    assert fired[0]["severity"] == "CRITICAL"


# ---------------------------------------------------------------------------
# Lifecycle — canceled leaves no trace; rejected offers legged fallback
# ---------------------------------------------------------------------------
def test_canceled_roll_leaves_no_execution_trace(store, monkeypatch):
    _seed_position()
    fake = _FakeClient([{"status": "CANCELED", "orderLegCollection": [], "orderActivityCollection": []}])
    _go_live(monkeypatch, fake)

    placed = executor.execute(_roll_payload())
    before = len(log.load_state()["executions"])
    res = executor.order_status(placed["order_id"])
    assert res["status"] == "canceled"
    assert len(log.load_state()["executions"]) == before
    assert log.get_pending_order(placed["order_id"]) is None


def test_rejected_roll_offers_legged_fallback_but_never_auto(store, monkeypatch):
    _seed_position()
    rejected = {"status": "REJECTED", "statusDescription": "account not approved for spreads",
                "orderLegCollection": [], "orderActivityCollection": []}
    fake = _FakeClient([rejected])
    _go_live(monkeypatch, fake)

    placed = executor.execute(_roll_payload())
    before = len(log.load_state()["executions"])
    res = executor.order_status(placed["order_id"])

    assert res["status"] == "rejected"
    assert "not approved for spreads" in res["reason"]
    fb = res["fallback"]
    assert fb["available"] is True and fb["confirm_field"] == "confirm_leg_manually"
    assert "legging risk" in fb["prompt"]
    assert fb["roll"]["from_strike"] == 140.5 and fb["roll"]["to_strike"] == 139.0
    # Nothing was executed or auto-legged.
    assert len(log.load_state()["executions"]) == before


# ---------------------------------------------------------------------------
# R7 — feature flag & legged fallback path
# ---------------------------------------------------------------------------
def test_flag_off_transmits_two_single_leg_orders(store, monkeypatch):
    _seed_position()
    fake = _FakeClient([_filled_order()])
    _go_live(monkeypatch, fake)
    monkeypatch.setattr(config, "ATOMIC_ROLLS_ENABLED", False)

    res = executor.execute(_roll_payload())
    assert res.get("legged") is True and len(res["orders"]) == 2
    # Two independent single-leg LIMIT orders, not one NET order.
    assert [o["orderType"] for o in fake.sent] == ["LIMIT", "LIMIT"]
    assert [l for o in fake.sent for l in [o["orderLegCollection"][0]["instruction"]]] \
        == ["BUY_TO_CLOSE", "SELL_TO_OPEN"]


def test_explicit_confirm_legs_even_when_flag_on(store, monkeypatch):
    _seed_position()
    fake = _FakeClient([_filled_order()])
    _go_live(monkeypatch, fake)
    assert config.ATOMIC_ROLLS_ENABLED is True
    res = executor.execute(_roll_payload(confirm_leg_manually=True))
    assert res.get("legged") is True  # operator opted into legging after e.g. a rejection


# ---------------------------------------------------------------------------
# R2 — recompute_derived equivalence: legged pair vs atomic pair
# ---------------------------------------------------------------------------
def _derived(state):
    # Strip the wall-clock date from the roll ledger — it's a timestamp, not part
    # of the economic equivalence between a legged and an atomic roll.
    ledger = {
        "by_ticker": state["roll_ledger"]["by_ticker"],
        "rolls": [{k: v for k, v in r.items() if k != "date"}
                  for r in state["roll_ledger"]["rolls"]],
    }
    return (state["theta_ledger"]["totals"], state["extrinsic_payback"], ledger)


def test_atomic_and_legged_rolls_produce_identical_ledgers(store, monkeypatch, tmp_path):
    # Store A: an atomic paper roll.
    _seed_position()
    executor.execute(_roll_payload())
    atomic = _derived(log.load_state())

    # Store B: the same roll booked as two independent legs carrying the shared
    # roll linkage (the legacy legged shape).
    b = tmp_path / "b"
    b.mkdir()
    monkeypatch.setattr(config, "STATE_PATH", str(b / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(b))
    import importlib
    importlib.reload(log)
    importlib.reload(executor)
    _seed_position()
    executor.execute({"action": "close_short", "ticker": "ON", "strike": 140.5,
                      "contracts": 5, "close_price_per_share": 2.5, "stock_price": 142,
                      "roll_group_id": "roll_001", "roll_leg": "close", "roll_reason": "scheduled"})
    executor.execute({"action": "sell_short", "ticker": "ON", "strike": 139.0,
                      "contracts": 5, "premium_per_share": 5.0, "stock_price": 142,
                      "expiration": "2026-07-10", "dte": 7,
                      "roll_group_id": "roll_001", "roll_leg": "open", "roll_reason": "scheduled"})
    legged = _derived(log.load_state())

    assert atomic == legged
    # And the ledger actually recorded the roll.
    assert log.load_state()["roll_ledger"]["by_ticker"]["ON"]["count"] == 1
    importlib.reload(log)
    importlib.reload(executor)


# ---------------------------------------------------------------------------
# R4 — paper mode: one net crossing, shape-identical records
# ---------------------------------------------------------------------------
def test_paper_roll_books_shape_identical_records(store):
    _seed_position()
    res = executor.execute(_roll_payload())
    assert res["status"] == "filled" and res["mode"] == "logged"
    assert [e["roll_leg"] for e in res["executions"]] == ["close", "open"]
    # Same roll_group_id / alloc marker shape as a live roll.
    assert len({e["roll_group_id"] for e in res["executions"]}) == 1
    assert all(e["roll_alloc_method"] == "mid" for e in res["executions"])
    # Net credit = new premium (2500) − buyback (1250).
    assert res["net_credit"] == 1250.0
    # The reference net mid is one net figure (5.0 − 2.5), not two per-leg legs.
    for e in res["executions"]:
        assert e["roll_reference_net_mid"] == pytest.approx(2.5)


def test_net_roll_slippage_reported_for_live_rolls_only(store, monkeypatch):
    _seed_position()
    # Live fill lands the net at 2.6 vs a 2.5 reference mid (better than mid).
    fake = _FakeClient([_filled_order(close_px=2.4, open_px=5.0)])
    _go_live(monkeypatch, fake)
    placed = executor.execute(_roll_payload())
    executor.order_status(placed["order_id"])

    rep = slippage.roll_report(log.load_state())
    assert rep["live_rolls"] == 1
    roll = rep["recent_rolls"][0]
    # realized net 5.0 − 2.4 = 2.6 vs reference 2.5 -> negative (favorable) slippage.
    assert roll["reference_net_mid"] == pytest.approx(2.5)
    assert roll["net_fill"] == pytest.approx(2.6)
    assert roll["net_slippage_pct"] == pytest.approx((2.5 - 2.6) / 2.5 * 100, abs=1e-3)


# ---------------------------------------------------------------------------
# G10 — forward-only migration
# ---------------------------------------------------------------------------
def test_migration_backfills_roll_group_id():
    import migrations
    state = {
        "schema_version": 11,
        "executions": [
            {"id": "e1", "action": "close_short", "roll_id": "roll_007"},
            {"id": "e2", "action": "sell_short", "roll_id": "roll_007"},
            {"id": "e3", "action": "buy_leap"},  # not a roll leg
        ],
    }
    out, changed = migrations.migrate(state)
    assert changed and out["schema_version"] == migrations.CURRENT_VERSION
    assert out["executions"][0]["roll_group_id"] == "roll_007"
    assert out["executions"][1]["roll_group_id"] == "roll_007"
    assert "roll_group_id" not in out["executions"][2]


def test_migration_is_idempotent_and_preserves_existing_group_id():
    import migrations
    state = {"schema_version": 11, "executions": [
        {"id": "e1", "action": "close_short", "roll_id": "roll_001", "roll_group_id": "keep_me"}]}
    out, _ = migrations.migrate(state)
    assert out["executions"][0]["roll_group_id"] == "keep_me"
