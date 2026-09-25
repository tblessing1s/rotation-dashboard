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


def _equity_item(symbol, amount, price):
    return {
        "instrument": {"assetType": "EQUITY", "symbol": symbol},
        "amount": amount, "price": price, "cost": -amount * price,
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


def test_pure_fee_trade_row_is_silently_skipped():
    """A TRADE row whose only item is a fee (no instrument at all) is a
    legitimate no-op — nothing was actually traded."""
    fee_only = {"feeType": "COMMISSION", "cost": 0.65}
    rec, err = ingest.parse_transaction(_txn("F1", None, [fee_only]))
    assert rec is None and err is None


def test_unrecognized_instrument_type_is_a_loud_error_not_a_silent_drop():
    """A TRADE row whose item DOES carry a real instrument + assetType — just
    one this code doesn't recognize — must surface as a parse error, never
    vanish the same way a harmless fee row does. Regression for exactly the
    failure mode that let the DIVIDEND_TYPES gap go undetected: a real trade
    silently dropped with no trace anywhere in the ingestion report."""
    bond_item = {"instrument": {"assetType": "FIXED_INCOME", "symbol": "912796XY5"},
                "amount": -1000, "price": 99.5, "cost": 995.00}
    rec, err = ingest.parse_transaction(_txn("E1", None, [bond_item]))
    assert rec is None
    assert "FIXED_INCOME" in err


def test_etf_leg_parses_as_equity_with_ticker_resolved(store):
    """CONFIRMED LIVE (2026-09-24): Schwab classifies an ETF's own transaction
    legs under assetType "COLLECTIVE_INVESTMENT", not "EQUITY" — this is what
    silently dropped a real IBIT share sale before it was found and fixed.
    A COLLECTIVE_INVESTMENT leg must parse exactly like a plain equity leg,
    including ticker resolution — a share leg has no underlyingSymbol field
    (that's option-only), so its own instrument symbol must be used instead.
    A CURRENCY settlement leg riding alongside it (also seen live) must be
    silently ignored, not treated as a second, unrecognized instrument."""
    etf_item = {"instrument": {"assetType": "COLLECTIVE_INVESTMENT", "symbol": "IBIT"},
                "amount": -100, "price": 48.7601, "cost": 4875.89}
    currency_item = {"instrument": {"assetType": "CURRENCY", "symbol": "USD"},
                     "amount": 4875.89, "cost": 0.0}
    rec, err = ingest.parse_transaction(_txn("E1", "OE1", [etf_item, currency_item]))
    assert err is None
    assert len(rec["legs"]) == 1
    leg = rec["legs"][0]
    assert leg["asset_type"] == "EQUITY"
    assert leg["underlying"] == "IBIT"
    assert leg["amount"] == -100

    report = ingest.build_report([_txn("E1", "OE1", [etf_item, currency_item])], log.load_state())
    assert not report["matched"]
    assert len(report["proposals"]) == 1
    proposal = report["proposals"][0]
    assert proposal["ticker"] == "IBIT"
    # Not ACT_UNKNOWN / the generic "SHORT STOCK... assignment likely" scare
    # text — a plain closing sale is now recognized for what it is.
    assert proposal["action"] == ingest.ACT_SELL_SHARES
    assert "sold out-of-band" in proposal["exposure"]


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


def test_lone_equity_leg_infers_shares_action_not_unknown():
    """CONFIRMED LIVE regression: a plain share sale (closing a real long
    position) used to infer_action() as ACT_UNKNOWN (it only ever looked at
    OPTION legs), which _exposure() then rendered as the generic "SHORT STOCK
    appeared out-of-band — assignment likely; review immediately" — alarming,
    wrong language for a legitimate closing sale. A lone equity leg must infer
    buy_shares/sell_shares, with matching plain-language exposure text."""
    sell_legs = [ingest._leg_from_transfer_item(_equity_item("IBIT", -100, 48.7601))]
    sell = ingest.infer_action(sell_legs)
    assert sell == ingest.ACT_SELL_SHARES
    assert "sold out-of-band" in ingest._exposure(sell, sell_legs)

    buy_legs = [ingest._leg_from_transfer_item(_equity_item("IBIT", 100, 44.51))]
    buy = ingest.infer_action(buy_legs)
    assert buy == ingest.ACT_BUY_SHARES
    assert "bought out-of-band" in ingest._exposure(buy, buy_legs)


# ---------------------------------------------------------------------------
# 6(a) — a fill matching an app order completes the lifecycle (source: app)
# ---------------------------------------------------------------------------
def test_matched_fill_tagged_source_app_and_recorded(store):
    state = log.load_state()
    # The app knows order O1 (it lives in order_receipts) AND actually booked
    # the execution at fill time — both must be true for "matched" (see the
    # next test for the case where only the order id is known).
    state.setdefault("order_receipts", []).append({"order_id": "O1", "ticker": "ABC"})
    state.setdefault("executions", []).append({
        "ticker": "ABC", "action": "sell_short", "strike": 110.0,
        "expiration": "2026-07-17", "contracts": 2})
    log.save_state(state)

    feed = [_sell_short_txn("T1", "O1")]
    report = ingest.run_ingestion(feed=feed)
    assert len(report["matched"]) == 1 and not report["proposals"]
    assert report["matched"][0]["source"] == ingest.SOURCE_APP

    # matched transaction id is now in the dedupe ledger (source app).
    state = log.load_state()
    assert state["ingested_transactions"]["T1"]["source"] == ingest.SOURCE_APP


def test_known_app_order_with_no_booked_execution_is_a_proposal_not_matched(store):
    """Regression for a real production incident: a known Schwab order id
    (the app placed it — it's in order_receipts) is NOT proof its fill ever
    got turned into an execution — order polling can die between submission
    and fill. Trusting the order id alone used to mark this "matched",
    permanently burying a real, unbooked broker fill in the dedupe ledger
    with no execution behind it (a matched fill books nothing further by
    design). It must surface as an adoptable proposal instead, with a note
    that it was placed from the app."""
    state = log.load_state()
    state.setdefault("order_receipts", []).append({"order_id": "O1", "ticker": "ABC"})
    # Deliberately NO matching execution in state["executions"].
    log.save_state(state)

    feed = [_sell_short_txn("T1", "O1")]
    report = ingest.run_ingestion(feed=feed)
    assert not report["matched"]
    assert len(report["proposals"]) == 1
    p = report["proposals"][0]
    assert p["source"] == ingest.SOURCE_BROKER_MANUAL
    assert "placed from the app" in p["summary"]

    # not recorded as ingested — it must keep resurfacing until adopted.
    state = log.load_state()
    assert "T1" not in state["ingested_transactions"]


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
    state.setdefault("executions", []).append({
        "ticker": "ABC", "action": "sell_short", "strike": 110.0,
        "expiration": "2026-07-17", "contracts": 2})
    log.save_state(state)
    feed = [_sell_short_txn("T1", "O1")]

    r1 = ingest.run_ingestion(feed=feed)
    assert len(r1["matched"]) == 1
    r2 = ingest.run_ingestion(feed=feed)
    assert not r2["matched"] and not r2["proposals"]
    assert r2["skipped_duplicates"] == ["T1"]
    # WHY it was skipped is visible too, not just that it was — this is what
    # lets an operator tell "confirmed app fill" apart from "wrongly marked
    # ingested with no execution behind it" from the ingestion report alone.
    assert len(r2["skipped_detail"]) == 1
    detail = r2["skipped_detail"][0]
    assert detail["transaction_id"] == "T1"
    assert detail["ticker"] == "ABC"
    assert detail["source"] == "app"
    assert detail["order_id"] == "O1"
    # ledger has exactly one entry for T1.
    state = log.load_state()
    assert list(state["ingested_transactions"].keys()) == ["T1"]


def test_release_ingested_lets_a_wrongly_matched_transaction_be_reclassified(store):
    """release_ingested is the recovery tool for a transaction the OLD
    (pre-fix) logic wrongly marked "matched" on a known order id alone, with
    no execution ever actually booked — permanently buried, since a matched
    fill books nothing further. Releasing it removes only the dedupe marker;
    the next ingest re-verifies it for real and it correctly becomes an
    adoptable proposal instead of silently staying lost."""
    state = log.load_state()
    state.setdefault("order_receipts", []).append({"order_id": "O1"})
    # Simulate the pre-fix bug's aftermath directly: dedupe-recorded as
    # "matched", but no execution behind it.
    ingest.record_ingested(state, "T1", source=ingest.SOURCE_APP, order_id="O1")
    log.save_state(state)

    feed = [_sell_short_txn("T1", "O1")]
    stuck = ingest.run_ingestion(feed=feed)
    assert not stuck["matched"] and not stuck["proposals"]
    assert stuck["skipped_duplicates"] == ["T1"]

    state = log.load_state()
    removed = ingest.release_ingested(state, ["T1"])
    assert removed == ["T1"]
    assert "T1" not in state["ingested_transactions"]
    log.save_state(state)

    recovered = ingest.run_ingestion(feed=feed)
    assert not recovered["matched"]
    assert len(recovered["proposals"]) == 1
    assert recovered["proposals"][0]["transaction_ids"] == ["T1"]

    # Releasing a transaction id the ledger never had is a harmless no-op.
    state = log.load_state()
    assert ingest.release_ingested(state, ["NOPE"]) == []


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


def test_adopt_broker_manual_sell_shares_closes_position_and_execution(store):
    """CONFIRMED LIVE regression: adopting a plain share-sale proposal used to
    silently book NOTHING — adopt_broker_trade only ever handled OPTION legs,
    so an equity leg fell through _adopt_action_for_leg returning None and was
    just skipped, while the call still reported success=True and dropped the
    proposal from the list. A real closing trade, with its real price already
    captured by ingestion, vanished with no error and no execution behind it.
    A closing sell_shares here must also empty and close the position (the
    same cleanup a normal fill gets via _commit) and complete the shares
    cycle so it actually shows up in History's Cycle log."""
    state = log.load_state()
    state["positions"].append({
        "ticker": "IBIT", "status": "open", "position_type": "SHARES",
        "shares": {"count": 100, "cost_basis_per_share": 44.51},
        "short_calls": [],
    })
    state.setdefault("executions", []).append({
        "ticker": "IBIT", "action": "buy_shares", "qty": 100,
        "price_per_share": 44.51, "execution_total": 4451.0, "date": "2026-09-08",
    })
    log.save_state(state)

    feed = [_txn("S1", "OS1", [_equity_item("IBIT", -100, 48.7601)])]
    ingest.run_ingestion(feed=feed)
    state = log.load_state()
    proposals = state["ingestion"]["proposals"]
    assert len(proposals) == 1
    pid = proposals[0]["proposal_id"]

    res = executor.adopt_broker_trade(pid)
    assert res["success"]
    assert len(res["execution_ids"]) == 1

    state = log.load_state()
    ex = state["executions"][-1]
    assert ex["action"] == "sell_shares"
    assert ex["qty"] == 100
    assert ex["price_per_share"] == 48.7601
    assert ex["source"] == ingest.SOURCE_BROKER_MANUAL
    assert ex["transaction_id"] == "S1"

    pos = log.find_position(state, "IBIT")
    assert pos["shares"]["count"] == 0
    assert pos["status"] == "closed"   # _close_if_empty cleanup, same as a normal fill

    # dedupe ledger + proposal cleared, same as any other adoption.
    assert state["ingested_transactions"]["S1"]["source"] == ingest.SOURCE_BROKER_MANUAL
    assert not state["ingestion"]["proposals"]

    # the shares cycle closes too, once both ends of the round trip are real
    # executions — this is what makes it show up in History's Cycle log.
    cycles = state["cycles"]
    assert len(cycles) == 1
    assert cycles[0]["ticker"] == "IBIT"
    assert cycles[0]["exit_date"] is not None


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


# ---------------------------------------------------------------------------
# Lost-update guard: a fill committed WHILE the transactions fetch is in flight
# ---------------------------------------------------------------------------
def test_fill_committed_during_transactions_fetch_survives_persist(store, monkeypatch):
    """Interval ingestion runs right after reconciliation on the same slow cadence.
    Loading state before the transactions fetch and saving that copy afterwards
    overwrote a fill the order poll booked in between. The fresh copy must be
    what gets classified and persisted: the fill survives AND is recognised as
    already booked (confirmed, never offered for adoption)."""
    def fetch_with_concurrent_fill():
        log.append_execution({"ticker": "ABC", "action": "sell_short", "strike": 110.0,
                              "contracts": 2, "expiration": "2026-07-17",
                              "premium_per_share": 1.20, "mode": "live"})
        return [_sell_short_txn("T1", "ORD-LATE", contracts=2, strike=110.0)]
    monkeypatch.setattr(ingest, "fetch_transactions", fetch_with_concurrent_fill)

    report = ingest.run_ingestion()
    assert report["broker_ok"] is True
    assert not report["proposals"]
    assert len(report["matched"]) == 1

    state = log.load_state()
    assert [e["action"] for e in state["executions"]] == ["sell_short"]
    assert state["ingested_transactions"]["T1"]["source"] == ingest.SOURCE_APP
    assert state["ingestion"]["last"]["matched"] == 1


def test_adoption_recovers_the_stock_price_from_the_order_journal(store):
    """A fill placed FROM the app whose execution was lost from the store comes
    back through ingestion as an "out-of-band" proposal. The journal still holds
    the price the app captured at the fill — the proposal carries it and
    adoption books the same extrinsic split the original fill would have."""
    log.append_order_journal({"event": "placed", "order_id": "TOS1", "ticker": "ABC",
                              "action": "sell_short", "stock_price": 110.4,
                              "price_source": "schwab"})
    log.append_order_journal({"event": "filled", "order_id": "TOS1", "ticker": "ABC",
                              "action": "sell_short", "stock_price": 110.9,
                              "stock_price_at_placement": 110.4,
                              "stock_price_at_fill": 110.9,
                              "stock_price_source": "fill_quote:schwab",
                              "execution_ids": ["exec_0007"]})

    feed = [_sell_short_txn("T1", "TOS1", contracts=3, price=1.25)]
    report = ingest.run_ingestion(feed=feed)
    assert len(report["proposals"]) == 1
    p = report["proposals"][0]
    assert p["app_order"] is True
    assert p["app_stock_price"] == 110.9
    assert p["app_stock_price_source"] == "fill_quote:schwab"
    assert "placed from this app" in p["summary"]
    state = log.load_state()
    assert state["ingestion"]["proposals"][0]["app_stock_price"] == 110.9

    res = executor.adopt_broker_trade(p["proposal_id"])   # no stock price typed
    assert res["stock_price"] == 110.9
    assert res["stock_price_source"] == "order_journal:fill_quote:schwab"
    ex = log.load_state()["executions"][-1]
    assert ex["stock_price"] == 110.9
    assert ex["stock_price_source"] == "order_journal:fill_quote:schwab"
    # extrinsic = 1.25 − (110.9 − 110.0)
    assert ex["entry_extrinsic_per_share"] == pytest.approx(0.35)


def test_adoption_prefers_the_operator_price_over_the_journal(store):
    log.append_order_journal({"event": "filled", "order_id": "TOS1", "ticker": "ABC",
                              "action": "sell_short", "stock_price": 110.9,
                              "stock_price_source": "fill_quote:schwab"})
    feed = [_sell_short_txn("T1", "TOS1", contracts=3, price=1.25)]
    report = ingest.run_ingestion(feed=feed)
    res = executor.adopt_broker_trade(report["proposals"][0]["proposal_id"], stock_price=110.5)
    assert res["stock_price"] == 110.5 and res["stock_price_source"] == "supplied"
    ex = log.load_state()["executions"][-1]
    assert ex["stock_price"] == 110.5 and ex["stock_price_source"] == "supplied"


# ---- GET /api/executions/order-journal -------------------------------------
def test_order_journal_route_filters_by_ticker_newest_first(store):
    import app as app_module

    log.append_order_journal({"event": "placed", "order_id": "O1", "ticker": "SPCX",
                              "action": "sell_short", "stock_price": 137.2,
                              "price_source": "schwab"})
    log.append_order_journal({"event": "filled", "order_id": "O1", "ticker": "SPCX",
                              "action": "sell_short", "stock_price": 137.5,
                              "price_source": "fill_quote:schwab"})
    log.append_order_journal({"event": "filled", "order_id": "O2", "ticker": "ABC",
                              "action": "sell_short", "stock_price": 50.0,
                              "price_source": "schwab"})

    client = app_module.app.test_client()
    resp = client.get("/api/executions/order-journal?ticker=spcx")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total_matched"] == 2
    assert [e["order_id"] for e in body["entries"]] == ["O1", "O1"]
    # newest first
    assert body["entries"][0]["event"] == "filled"
    assert body["entries"][0]["stock_price"] == 137.5

    all_body = client.get("/api/executions/order-journal").get_json()
    assert all_body["total_matched"] == 3


def test_order_journal_route_empty_ticker_returns_no_entries(store):
    import app as app_module

    log.append_order_journal({"event": "filled", "order_id": "O1", "ticker": "ABC",
                              "stock_price": 50.0})
    client = app_module.app.test_client()
    body = client.get("/api/executions/order-journal?ticker=ZZZ").get_json()
    assert body["entries"] == [] and body["total_matched"] == 0


# ---- ingestion's one-off deeper lookback -----------------------------------
def test_run_ingestion_default_window_matches_config(store, monkeypatch):
    seen = {}

    def fake_fetch(lookback_days=None):
        seen["lookback_days"] = lookback_days
        return []
    monkeypatch.setattr(ingest, "fetch_transactions", fake_fetch)
    ingest.run_ingestion(lookback_days=17)
    assert seen["lookback_days"] == 17


def test_start_end_window_overrides_and_caps_lookback(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(config, "INGESTION_LOOKBACK_DAYS", 7)

    start7, end7 = ingest._start_end_window()
    start17, _ = ingest._start_end_window(17)
    start_capped, _ = ingest._start_end_window(9999)

    def _days_back(start, end):
        return (datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                - datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days

    assert _days_back(start7, end7) == 7
    assert _days_back(start17, end7) == 17
    assert _days_back(start_capped, end7) == ingest.INGESTION_MAX_LOOKBACK_DAYS


def test_ingestion_route_pulls_a_deeper_window_and_finds_an_old_out_of_band_trade(store, monkeypatch):
    import app as app_module

    seen = {}

    def fake_fetch(lookback_days=None):
        seen["lookback_days"] = lookback_days
        return [_sell_short_txn("OLD1", "TOS-OLD", contracts=1, price=2.02, strike=43.0)]
    monkeypatch.setattr(ingest, "fetch_transactions", fake_fetch)

    client = app_module.app.test_client()
    resp = client.post("/api/ingestion", json={"lookback_days": 30})
    assert resp.status_code == 200
    body = resp.get_json()
    assert seen["lookback_days"] == 30
    assert len(body["proposals"]) == 1
    assert body["proposals"][0]["transaction_ids"] == ["OLD1"]
