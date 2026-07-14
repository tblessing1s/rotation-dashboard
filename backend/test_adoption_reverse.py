"""Reverse-adoption tests — undo an accidental broker_manual adoption exactly.

Offline. Reconstructs the reported incident: adopting a broker trade the app
already had (a) duplicated a short leg and (b) — in the LEAP case — a close_leap
adoption removed a real LEAP leg. Reversal must restore the position exactly,
including a restored LEAP's entry extrinsic pulled from the original immutable
buy_leap, and must leave the derived ledgers as if the adoption never happened.

Run: python -m pytest backend/test_adoption_reverse.py -q
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
    monkeypatch.setattr(executor, "live_enabled", lambda: False)  # paper
    return tmp_path


def _occ(u, e, k, call=True):
    return schwab_api.occ_option_symbol(u, e, k, call=call)


def _opt_item(u, e, k, amount, price, effect, call=True):
    return {"instrument": {"assetType": "OPTION", "symbol": _occ(u, e, k, call),
                           "underlyingSymbol": u, "putCall": "CALL" if call else "PUT",
                           "strikePrice": k, "expirationDate": f"{e}T00:00:00Z"},
            "amount": amount, "price": price, "cost": -amount * price * 100,
            "positionEffect": effect}


def _txn(tid, oid, items):
    return {"activityId": tid, "orderId": oid, "type": "TRADE",
            "time": "2026-07-10T15:30:00Z", "netAmount": 0, "transferItems": items}


# ---------------------------------------------------------------------------
# Duplicate short: adopt then reverse removes exactly the adopted leg
# ---------------------------------------------------------------------------
def test_reverse_removes_adopted_duplicate_short(store):
    state = log.load_state()
    state["positions"].append({
        "ticker": "XLK", "status": "open", "short_calls": [], "shares": {"count": 0},
        "leap_legs": [{"strike": 135.0, "contracts": 1, "expiration": "2027-01-15"}]})
    log.save_state(state)

    feed = [_txn("T1", "TOS1", [_opt_item("XLK", "2026-07-17", 179.0, -1, 1.2, "OPENING")])]
    ingest.run_ingestion(feed=feed)
    pid = log.load_state()["ingestion"]["proposals"][0]["proposal_id"]
    executor.adopt_broker_trade(pid, stock_price=170.0)

    assert len(log.find_position(log.load_state(), "XLK")["short_calls"]) == 1
    res = executor.reverse_adoption(pid)
    assert res["status"] == "reversed"
    pos = log.find_position(log.load_state(), "XLK")
    assert pos["short_calls"] == []          # adopted leg gone
    # ledger entry cleared so a corrected re-ingest is possible.
    assert "T1" not in log.load_state()["ingested_transactions"]


# ---------------------------------------------------------------------------
# The LEAP case: a close_leap adoption removed a real LEAP; reverse restores it
# with the entry extrinsic from the ORIGINAL buy_leap.
# ---------------------------------------------------------------------------
def test_reverse_restores_leap_with_original_extrinsic(store):
    # Buy the real LEAP (paper): 1x 135 call, cost $3300/contract, underlying 145
    # -> intrinsic 10*100=1000, extrinsic_at_entry = 3300-1000 = 2300.
    executor.execute({"action": "buy_leap", "ticker": "XLK", "strike": 135.0,
                      "contracts": 1, "execution_price": 3300, "stock_price": 145.0,
                      "expiration": "2027-01-15", "override_reason": "fixture"})
    pos = log.find_position(log.load_state(), "XLK")
    leg0 = log.leap_legs(pos)[0]
    orig_extrinsic = leg0["extrinsic_at_entry"]
    assert orig_extrinsic == 2300.0

    # An out-of-band SELL_TO_CLOSE of that 135 LEAP -> adopted as close_leap, which
    # REMOVES the leap leg (reproducing "broker holds it, state does not expect it").
    feed = [_txn("TL1", "TOSL", [_opt_item("XLK", "2027-01-15", 135.0, -1, 40.0, "CLOSING")])]
    ingest.run_ingestion(feed=feed)
    pid = log.load_state()["ingestion"]["proposals"][0]["proposal_id"]
    assert log.load_state()["ingestion"]["proposals"][0]["action"] == ingest.ACT_CLOSE_LEAP
    executor.adopt_broker_trade(pid, stock_price=175.0)
    assert log.leap_legs(log.find_position(log.load_state(), "XLK")) == []  # leap gone

    # Reverse -> the 135 LEAP leg is restored with its ORIGINAL entry extrinsic.
    executor.reverse_adoption(pid)
    pos = log.find_position(log.load_state(), "XLK")
    legs = log.leap_legs(pos)
    assert len(legs) == 1
    restored = legs[0]
    assert restored["strike"] == 135.0
    assert restored["contracts"] == 1
    assert restored["extrinsic_at_entry"] == orig_extrinsic   # pulled from the buy_leap
    assert restored["cost_basis"] == 3300.0


def test_reverse_is_idempotent(store):
    state = log.load_state()
    state["positions"].append({
        "ticker": "XLK", "status": "open", "short_calls": [], "shares": {"count": 0},
        "leap_legs": [{"strike": 135.0, "contracts": 1, "expiration": "2027-01-15"}]})
    log.save_state(state)
    feed = [_txn("T1", "TOS1", [_opt_item("XLK", "2026-07-17", 179.0, -1, 1.2, "OPENING")])]
    ingest.run_ingestion(feed=feed)
    pid = log.load_state()["ingestion"]["proposals"][0]["proposal_id"]
    executor.adopt_broker_trade(pid, stock_price=170.0)
    executor.reverse_adoption(pid)
    with pytest.raises(ValueError, match="already reversed|no broker_manual"):
        executor.reverse_adoption(pid)


def test_list_broker_manual_adoptions(store):
    state = log.load_state()
    state["positions"].append({
        "ticker": "XLK", "status": "open", "short_calls": [], "shares": {"count": 0},
        "leap_legs": [{"strike": 135.0, "contracts": 1, "expiration": "2027-01-15"}]})
    log.save_state(state)
    feed = [_txn("T1", "TOS1", [_opt_item("XLK", "2026-07-17", 179.0, -1, 1.2, "OPENING")])]
    ingest.run_ingestion(feed=feed)
    pid = log.load_state()["ingestion"]["proposals"][0]["proposal_id"]
    executor.adopt_broker_trade(pid, stock_price=170.0)

    adoptions = executor.list_broker_manual_adoptions()
    assert len(adoptions) == 1
    assert adoptions[0]["proposal_id"] == pid
    assert adoptions[0]["reversible"] is True
    assert any(l["action"] == "sell_short" for l in adoptions[0]["legs"])


# ---------------------------------------------------------------------------
# Manual (out-of-band) roll: record from captured fills + derived stock price
# ---------------------------------------------------------------------------
def test_derive_stock_price_from_call():
    # 179 sold for 2.50 with 1.50 extrinsic -> intrinsic 1.00 -> stock 180.
    assert executor.derive_stock_price_from_call(179.0, 2.50, 1.50) == 180.0
    # ATM/OTM (premium == extrinsic) -> can't pin above strike -> returns strike.
    assert executor.derive_stock_price_from_call(179.0, 0.80, 0.80) == 179.0


def test_record_manual_roll_computes_both_extrinsics(store):
    # Position holds the OLD 183 short (entry extrinsic 0.90) + a LEAP.
    state = log.load_state()
    state["positions"].append({
        "ticker": "XLK", "status": "open", "shares": {"count": 0},
        "leap_legs": [{"strike": 135.0, "contracts": 1, "expiration": "2027-01-15"}],
        "short_calls": [{"strike": 183.0, "contracts": 1, "expiration": "2026-07-17",
                         "entry_extrinsic_per_share": 0.90}]})
    log.save_state(state)

    # Roll executed in ToS at underlying 180: bought back 183 @ 0.40, sold 179 @ 2.50.
    stock = executor.derive_stock_price_from_call(179.0, 2.50, 1.50)  # -> 180.0
    res = executor.record_manual_roll(
        "XLK", from_strike=183.0, buyback_per_share=0.40, to_strike=179.0,
        premium_per_share=2.50, stock_price=stock, to_expiration="2026-07-24",
        from_expiration="2026-07-17")
    assert res["status"] == "recorded" and res["stock_price"] == 180.0

    state = log.load_state()
    pos = log.find_position(state, "XLK")
    strikes = {sc["strike"] for sc in pos["short_calls"]}
    assert strikes == {179.0}                      # 183 closed, 179 opened
    new = next(sc for sc in pos["short_calls"] if sc["strike"] == 179.0)
    # 179 entry extrinsic = 2.50 − max(180−179,0) = 1.50 (matches what the operator saw).
    assert new["entry_extrinsic_per_share"] == 1.50

    execs = state["executions"]
    close = next(e for e in execs if e.get("action") == "close_short" and e.get("strike") == 183.0)
    # 183 buyback extrinsic = 0.40 − max(180−183,0) = 0.40 (fully extrinsic, OTM).
    assert close["extrinsic_paid_back"] == 0.40
    # net juice on the 183 close = entry extrinsic 0.90 − paid back 0.40 = +0.50/sh.
    assert close["net_juice"] == 0.50
    # both legs linked as ONE roll.
    gids = {e.get("roll_group_id") for e in execs if e.get("action") in ("close_short", "sell_short")}
    assert len(gids) == 1 and None not in gids
    assert all(e.get("source") == ingest.SOURCE_BROKER_MANUAL for e in execs
               if e.get("action") in ("close_short", "sell_short"))


# ---------------------------------------------------------------------------
# Rebuild a tangled position from broker truth (the XLK cleanup)
# ---------------------------------------------------------------------------
def test_rebuild_position_from_broker_restores_economics(store):
    import reconcile
    # Immutable log carries the real economics for each leg (as in the incident).
    state = log.load_state()
    state["executions"] += [
        {"id": "exec_a", "action": "buy_leap", "ticker": "XLK", "strike": 135.0,
         "contracts": 1, "execution_price": 5680, "extrinsic_captured": 567, "mode": "live"},
        {"id": "exec_b", "action": "buy_leap", "ticker": "XLK", "strike": 137.5,
         "contracts": 1, "execution_price": 5305, "extrinsic_captured": 649, "mode": "live"},
        {"id": "exec_c", "action": "sell_short", "ticker": "XLK", "strike": 179.0,
         "contracts": 1, "premium_per_share": 9.45, "entry_extrinsic_per_share": 2.25, "mode": "live"},
        {"id": "exec_d", "action": "sell_short", "ticker": "XLK", "strike": 179.0,
         "contracts": 1, "premium_per_share": 5.10, "entry_extrinsic_per_share": 5.10, "mode": "live"},
    ]
    # A tangled current position (duplicate/phantom legs, $0 economics).
    state["positions"].append({
        "ticker": "XLK", "status": "open", "shares": {"count": 0},
        "leap_legs": [{"strike": 135.0, "contracts": 1}],  # 137.5 missing
        "short_calls": [
            {"strike": 179.0, "contracts": 2, "entry_extrinsic_per_share": 0},  # spurious
            {"strike": 183.0, "contracts": 1, "entry_extrinsic_per_share": 0}]})  # phantom
    log.save_state(state)

    # Broker ground truth: two 179 weeklies (5.10 / 9.45) + two LEAPs.
    broker_legs = [
        {"instrument_type": reconcile.OPTION, "strike": 179.0, "quantity": -1,
         "expiry": "2026-07-17", "avg_price": 5.10, "underlying": "XLK"},
        {"instrument_type": reconcile.OPTION, "strike": 179.0, "quantity": -1,
         "expiry": "2026-07-24", "avg_price": 9.45, "underlying": "XLK"},
        {"instrument_type": reconcile.OPTION, "strike": 135.0, "quantity": 1,
         "expiry": "2027-01-15", "avg_price": 56.80, "underlying": "XLK"},
        {"instrument_type": reconcile.OPTION, "strike": 137.5, "quantity": 1,
         "expiry": "2027-01-15", "avg_price": 53.05, "underlying": "XLK"},
    ]
    # Step 1 — dry run proposes legs from broker truth + log-matched economics.
    prop = executor.rebuild_position_from_broker("XLK", broker_legs=broker_legs, dry_run=True)
    assert prop["status"] == "proposed"
    by_exp = {l["expiration"]: l for l in prop["legs"] if l["leg_type"] == "short"}
    assert by_exp["2026-07-24"]["entry_extrinsic_per_share"] == 2.25   # from exec_c
    assert by_exp["2026-07-17"]["entry_extrinsic_per_share"] == 5.10   # log match (WRONG value)

    # Step 2 — operator corrects the 179 07-17 entry extrinsic to the real 1.83,
    # then confirms. The correction wins over the log.
    edited = []
    for l in prop["legs"]:
        l = dict(l)
        if l.get("leg_type") == "short" and l.get("expiration") == "2026-07-17":
            l["entry_extrinsic_per_share"] = 1.83
        edited.append(l)
    res = executor.rebuild_position_from_broker("XLK", legs=edited)
    assert res["status"] == "rebuilt"

    pos = log.find_position(log.load_state(), "XLK")
    # Exactly the broker legs, no phantom 183.
    shorts = sorted(pos["short_calls"], key=lambda s: s["expiration"])
    assert [(s["strike"], s["contracts"], s["expiration"]) for s in shorts] == [
        (179.0, 1, "2026-07-17"), (179.0, 1, "2026-07-24")]
    by_exp = {s["expiration"]: s for s in shorts}
    assert by_exp["2026-07-17"]["entry_extrinsic_per_share"] == 1.83   # operator correction won
    assert by_exp["2026-07-24"]["entry_extrinsic_per_share"] == 2.25   # from exec_c
    leaps = sorted(log.leap_legs(pos), key=lambda l: l["strike"])
    assert [(l["strike"], l["contracts"]) for l in leaps] == [(135.0, 1), (137.5, 1)]
    assert leaps[0]["cost_basis"] == 5680 and leaps[0]["extrinsic_at_entry"] == 567
    assert leaps[1]["cost_basis"] == 5305 and leaps[1]["extrinsic_at_entry"] == 649
    assert pos["short_calls"][0].get("rebuilt") is True
