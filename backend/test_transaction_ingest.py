"""Execution-ingestion tests — Schwab transactions -> state.json (spec §4).

Fully offline: transactions feeds are scripted dicts, no live Schwab call. Covers
parsing/grouping, dedupe by transaction id (idempotency), matched-vs-out-of-band
classification, the linked multi-leg (manual roll) case, one-click adoption of a
broker_manual trade through the existing builders, and derived-recompute
integrity after ingestion.

Run: python -m pytest backend/test_transaction_ingest.py -q
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402
import reconcile  # noqa: E402
import schwab_api  # noqa: E402
import transaction_ingest as ingest  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


# ---- transaction-feed builders --------------------------------------------
def _occ(underlying, expiry, strike, call=True):
    return schwab_api.occ_option_symbol(underlying, expiry, strike, call=call)


def _opt_item(underlying, expiry, strike, amount, price, effect, call=True):
    return {
        "instrument": {"assetType": "OPTION", "symbol": _occ(underlying, expiry, strike, call),
                       "underlyingSymbol": underlying, "putCall": "CALL" if call else "PUT",
                       "strikePrice": strike, "expirationDate": f"{expiry}T00:00:00Z"},
        "amount": amount, "price": price, "cost": -amount * price * 100,
        "positionEffect": effect,
    }


def _txn(txn_id, order_id, items, time="2026-07-10T15:30:00Z", ttype="TRADE"):
    return {"activityId": txn_id, "orderId": order_id, "type": ttype, "time": time,
            "netAmount": sum(i["cost"] for i in items), "transferItems": items}


def _sell_short_txn(txn_id, order_id, underlying="ABC", expiry="2026-07-17",
                    strike=110.0, contracts=2, price=1.20):
    return _txn(txn_id, order_id,
                [_opt_item(underlying, expiry, strike, -contracts, price, "OPENING")])


# ---------------------------------------------------------------------------
# Parsing + grouping
# ---------------------------------------------------------------------------
def test_parse_transaction_normalizes_option_leg():
    txn = _sell_short_txn("T1", "O1")
    rec, err = ingest.parse_transaction(txn)
    assert err is None
    assert rec["transaction_id"] == "T1"
    assert rec["order_id"] == "O1"
    assert len(rec["legs"]) == 1
    leg = rec["legs"][0]
    assert leg["asset_type"] == "OPTION"
    assert leg["underlying"] == "ABC"
    assert leg["put_call"] == reconcile.CALL
    assert leg["strike"] == 110.0
    assert leg["amount"] == -2 and leg["price"] == 1.20
    assert leg["position_effect"] == "OPENING"


def test_non_trade_transaction_skipped():
    rec, err = ingest.parse_transaction({"activityId": "D1", "type": "DIVIDEND"})
    assert rec is None and err is None


def test_transaction_without_id_is_an_error():
    rec, err = ingest.parse_transaction(_txn(None, "O1", [_opt_item("ABC", "2026-07-17", 110.0, -1, 1.0, "OPENING")]))
    assert rec is None and "activityId" in err


def test_group_by_order_links_roll_legs():
    close = _opt_item("ABC", "2026-07-10", 100.0, 3, 0.40, "CLOSING")   # buy-to-close
    open_ = _opt_item("ABC", "2026-07-17", 105.0, -3, 1.10, "OPENING")  # sell-to-open
    feed = [_txn("Tc", "ORD9", [close]), _txn("To", "ORD9", [open_])]
    records, _ = ingest.parse_feed(feed)
    groups = ingest.group_by_order(records)
    assert len(groups) == 1
    g = groups[0]
    assert g["order_id"] == "ORD9"
    assert set(g["transaction_ids"]) == {"Tc", "To"}
    assert ingest.infer_action(g["legs"]) == ingest.ACT_ROLL


# ---------------------------------------------------------------------------
# 6(a) — a fill matching an app order completes the lifecycle (source: app)
# ---------------------------------------------------------------------------
def test_matched_fill_tagged_source_app_and_recorded(store):
    state = log.load_state()
    # The app knows order O1 (it lives in order_receipts).
    state.setdefault("order_receipts", []).append({"order_id": "O1", "ticker": "ABC"})
    log.save_state(state)

    feed = [_sell_short_txn("T1", "O1")]
    report = ingest.run_ingestion(feed=feed)
    assert len(report["matched"]) == 1 and not report["proposals"]
    assert report["matched"][0]["source"] == ingest.SOURCE_APP

    # matched transaction id is now in the dedupe ledger (source app).
    state = log.load_state()
    assert state["ingested_transactions"]["T1"]["source"] == ingest.SOURCE_APP


# ---------------------------------------------------------------------------
# 6(b) — a manual two-leg roll with no app record -> ONE linked broker_manual proposal
# ---------------------------------------------------------------------------
def test_out_of_band_roll_surfaces_one_linked_proposal(store):
    close = _opt_item("ABC", "2026-07-10", 100.0, 3, 0.40, "CLOSING")
    open_ = _opt_item("ABC", "2026-07-17", 105.0, -3, 1.10, "OPENING")
    feed = [_txn("Tc", "TOS9", [close]), _txn("To", "TOS9", [open_])]

    report = ingest.run_ingestion(feed=feed)
    assert not report["matched"]
    assert len(report["proposals"]) == 1
    p = report["proposals"][0]
    assert p["source"] == ingest.SOURCE_BROKER_MANUAL
    assert p["action"] == ingest.ACT_ROLL
    assert set(p["transaction_ids"]) == {"Tc", "To"}
    assert p["ticker"] == "ABC"
    assert "roll" in p["exposure"].lower()

    # surfaced on state, NOT yet in the dedupe ledger (awaits adoption).
    state = log.load_state()
    assert len(state["ingestion"]["proposals"]) == 1
    assert "Tc" not in state["ingested_transactions"]


# ---------------------------------------------------------------------------
# 6(c) — duplicate transaction ids across two runs -> second run is a no-op
# ---------------------------------------------------------------------------
def test_reingest_is_idempotent(store):
    state = log.load_state()
    state.setdefault("order_receipts", []).append({"order_id": "O1"})
    log.save_state(state)
    feed = [_sell_short_txn("T1", "O1")]

    r1 = ingest.run_ingestion(feed=feed)
    assert len(r1["matched"]) == 1
    r2 = ingest.run_ingestion(feed=feed)
    assert not r2["matched"] and not r2["proposals"]
    assert r2["skipped_duplicates"] == ["T1"]
    # ledger has exactly one entry for T1.
    state = log.load_state()
    assert list(state["ingested_transactions"].keys()) == ["T1"]


# ---------------------------------------------------------------------------
# Adoption — book the out-of-band trade through the real builders
# ---------------------------------------------------------------------------
def test_adopt_broker_manual_sell_short_books_execution_and_position(store):
    # An open position with a LEAP so the short has somewhere to attach.
    state = log.load_state()
    state["positions"].append({
        "ticker": "ABC", "status": "open",
        "leap": {"strike": 50.0, "contracts": 3, "expiration": "2027-01-15"},
        "leap_legs": [{"strike": 50.0, "contracts": 3, "expiration": "2027-01-15"}],
        "short_calls": [], "shares": {"count": 0},
    })
    log.save_state(state)

    feed = [_sell_short_txn("T1", "TOS1", contracts=3, price=1.25)]
    ingest.run_ingestion(feed=feed)
    state = log.load_state()
    pid = state["ingestion"]["proposals"][0]["proposal_id"]

    res = executor.adopt_broker_trade(pid)
    assert res["success"] and res["source"] == ingest.SOURCE_BROKER_MANUAL
    assert len(res["execution_ids"]) == 1

    state = log.load_state()
    ex = state["executions"][-1]
    assert ex["action"] == "sell_short"
    assert ex["source"] == ingest.SOURCE_BROKER_MANUAL
    assert ex["transaction_id"] == "T1"
    assert ex["premium_per_share"] == 1.25       # economics verbatim from broker
    assert ex["contracts"] == 3
    # position mutated: the short leg now exists.
    pos = log.find_position(state, "ABC")
    assert any(sc["strike"] == 110.0 and sc["contracts"] == 3 for sc in pos["short_calls"])
    # dedupe ledger + proposal cleared.
    assert state["ingested_transactions"]["T1"]["source"] == ingest.SOURCE_BROKER_MANUAL
    assert not state["ingestion"]["proposals"]


def test_adopt_broker_manual_roll_links_both_legs(store):
    state = log.load_state()
    state["positions"].append({
        "ticker": "ABC", "status": "open",
        "leap": {"strike": 50.0, "contracts": 3, "expiration": "2027-01-15"},
        "leap_legs": [{"strike": 50.0, "contracts": 3, "expiration": "2027-01-15"}],
        # An existing short at 100 that the roll buys back.
        "short_calls": [{"strike": 100.0, "contracts": 3, "expiration": "2026-07-10",
                         "entry_extrinsic_per_share": 0.9}],
        "shares": {"count": 0},
    })
    log.save_state(state)

    close = _opt_item("ABC", "2026-07-10", 100.0, 3, 0.40, "CLOSING")
    open_ = _opt_item("ABC", "2026-07-17", 105.0, -3, 1.10, "OPENING")
    feed = [_txn("Tc", "TOS9", [close]), _txn("To", "TOS9", [open_])]
    ingest.run_ingestion(feed=feed)
    state = log.load_state()
    pid = state["ingestion"]["proposals"][0]["proposal_id"]

    res = executor.adopt_broker_trade(pid)
    assert set(res["transaction_ids"]) == {"Tc", "To"}
    state = log.load_state()
    execs = state["executions"][-2:]
    actions = {e["action"] for e in execs}
    assert actions == {"close_short", "sell_short"}
    # both legs share one roll_group_id (booked as a single logical roll).
    gids = {e.get("roll_group_id") for e in execs}
    assert len(gids) == 1 and None not in gids
    # position: old 100 short gone, new 105 short present.
    pos = log.find_position(state, "ABC")
    strikes = {sc["strike"] for sc in pos["short_calls"]}
    assert strikes == {105.0}


# ---------------------------------------------------------------------------
# 9 — derived-recompute integrity: a full recompute equals the incremental result
# ---------------------------------------------------------------------------
def test_recompute_after_ingestion_is_stable(store):
    state = log.load_state()
    state["positions"].append({
        "ticker": "ABC", "status": "open",
        "leap": {"strike": 50.0, "contracts": 3, "expiration": "2027-01-15"},
        "leap_legs": [{"strike": 50.0, "contracts": 3, "expiration": "2027-01-15"}],
        "short_calls": [{"strike": 100.0, "contracts": 3, "expiration": "2026-07-10",
                         "entry_extrinsic_per_share": 0.9}],
        "shares": {"count": 0},
    })
    log.save_state(state)
    close = _opt_item("ABC", "2026-07-10", 100.0, 3, 0.40, "CLOSING")
    open_ = _opt_item("ABC", "2026-07-17", 105.0, -3, 1.10, "OPENING")
    ingest.run_ingestion(feed=[_txn("Tc", "TOS9", [close]), _txn("To", "TOS9", [open_])])
    state = log.load_state()
    executor.adopt_broker_trade(state["ingestion"]["proposals"][0]["proposal_id"])

    state = log.load_state()
    before = dict(state["theta_ledger"]["totals"])
    log.recompute_derived(state)  # idempotent replay from the immutable executions
    after = dict(state["theta_ledger"]["totals"])
    assert before == after


def test_already_booked_fill_is_confirmed_not_proposed(store):
    """Regression for the duplicate-leg defect: a broker fill the app ALREADY has
    (same ticker/action/strike/expiry/contracts) but whose orderId doesn't link to
    an app order must be CONFIRMED (source: app), never surfaced for adoption."""
    state = log.load_state()
    # The app already booked this short (via its normal fill path — no orderId link).
    state["executions"].append({
        "id": "exec_001", "ticker": "ABC", "action": "sell_short",
        "strike": 110.0, "contracts": 2, "expiration": "2026-07-17",
        "premium_per_share": 1.20, "mode": "live",
    })
    log.save_state(state)

    # Broker reports the same fill with an orderId the app never recorded.
    feed = [_sell_short_txn("T99", "UNKNOWN_ORDER", contracts=2, strike=110.0)]
    report = ingest.run_ingestion(feed=feed)
    assert not report["proposals"], "an already-booked fill must not be adoptable"
    assert len(report["matched"]) == 1
    assert report["matched"][0]["source"] == ingest.SOURCE_APP
    assert "already booked" in report["matched"][0]["summary"]


def test_group_already_booked_counts_are_consumed(store):
    """Two identical booked shorts match two identical broker legs, but a THIRD
    identical broker leg is genuinely new (not swallowed by the two)."""
    state = log.load_state()
    for i in (1, 2):
        state["executions"].append({
            "id": f"exec_00{i}", "ticker": "ABC", "action": "sell_short",
            "strike": 110.0, "contracts": 1, "expiration": "2026-07-17", "mode": "live"})
    keys = ingest.existing_execution_keys(state)
    two = [{"asset_type": "OPTION", "amount": -1, "strike": 110.0, "expiry": "2026-07-17",
            "position_effect": "OPENING", "underlying": "ABC", "put_call": reconcile.CALL} for _ in range(2)]
    three = two + [dict(two[0])]
    assert ingest._group_already_booked(two, keys) is True
    assert ingest._group_already_booked(three, keys) is False


def test_adopt_refuses_when_already_booked(store):
    """Defense-in-depth: if state changed so the proposal is now already booked,
    adoption refuses rather than double-booking."""
    state = log.load_state()
    state["positions"].append({
        "ticker": "ABC", "status": "open", "short_calls": [], "shares": {"count": 0},
        "leap_legs": [{"strike": 50.0, "contracts": 1, "expiration": "2027-01-15"}]})
    # Stash a proposal directly, then also book the matching execution.
    state["ingestion"]["proposals"] = [{
        "proposal_id": "adopt_TOSX", "ticker": "ABC", "order_id": "TOSX",
        "action": "sell_short",
        "legs": [{"asset_type": "OPTION", "amount": -2, "price": 1.2, "strike": 110.0,
                  "expiry": "2026-07-17", "position_effect": "OPENING",
                  "underlying": "ABC", "put_call": reconcile.CALL, "transaction_id": "TX"}]}]
    state["executions"].append({
        "id": "exec_001", "ticker": "ABC", "action": "sell_short", "strike": 110.0,
        "contracts": 2, "expiration": "2026-07-17", "mode": "live"})
    log.save_state(state)

    with pytest.raises(ValueError, match="already booked"):
        executor.adopt_broker_trade("adopt_TOSX")
    # No duplicate short leg created; stale proposal dropped.
    state = log.load_state()
    assert log.find_position(state, "ABC")["short_calls"] == []
    assert not state["ingestion"]["proposals"]


def test_fetch_failure_reports_error_and_touches_nothing(store, monkeypatch):
    def boom():
        raise RuntimeError("broker down")
    monkeypatch.setattr(ingest, "fetch_transactions", boom)
    report = ingest.run_ingestion()
    assert report["broker_ok"] is False
    assert any("fetch failed" in e for e in report["errors"])
    state = log.load_state()
    assert not state["ingested_transactions"]
