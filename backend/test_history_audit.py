"""Trade-history audit — the book's fills vs Schwab's own transactions.

Each case is one of the live drifts the audit exists to catch the next morning:
a canceled roll booked as filled ($0/$0), another account's trades in a book,
a book missing its own broker fills, and a fill at the wrong price."""
from __future__ import annotations

import pytest

import alerts
import config
import history_audit as ha
import logging_handler as log
import schwab_api

SINCE, UNTIL = "2026-08-29", "2026-09-28"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _opt(ticker, exp, strike, amount, price, effect):
    return {"instrument": {"assetType": "OPTION", "underlyingSymbol": ticker,
                           "symbol": schwab_api.occ_option_symbol(ticker, exp, strike)},
            "amount": amount, "price": price, "cost": -amount * price * 100,
            "positionEffect": effect}


def _eq(ticker, amount, price):
    return {"instrument": {"assetType": "EQUITY", "symbol": ticker},
            "amount": amount, "price": price, "cost": -amount * price,
            "positionEffect": "OPENING" if amount > 0 else "CLOSING"}


def _txn(tid, oid, day, *items):
    return {"activityId": tid, "orderId": oid, "type": "TRADE",
            "time": f"{day}T15:00:00+0000", "transferItems": list(items)}


def _ex(eid, action, day, **kw):
    e = {"id": eid, "ticker": "SPCX", "action": action, "date": f"{day}T15:00:00Z",
         "mode": "live", "live_transmitted": True}
    e.update(kw)
    return e


def _state(execs, **extra):
    s = {"executions": execs, "positions": []}
    s.update(extra)
    return s


def _kinds(rep):
    return sorted(f["kind"] for f in rep["findings"])


def test_matching_fills_are_clean():
    feed = [_txn(1, 11, "2026-09-17",
                 _opt("SPCX", "2026-09-25", 135, 1, 20.17, "CLOSING"),
                 _opt("SPCX", "2026-09-25", 144, -1, 11.97, "OPENING"))]
    state = _state([
        _ex("e1", "close_short", "2026-09-17", strike=135, contracts=1, close_price_per_share=20.17),
        _ex("e2", "sell_short", "2026-09-17", strike=144, contracts=1, premium_per_share=11.97),
    ])
    rep = ha.audit(feed, state, SINCE, UNTIL)
    assert rep["status"] == "CLEAN" and rep["matched"] == 2 and rep["findings"] == []


def test_a_canceled_roll_booked_as_filled_is_phantom():
    # LIVE: SPCX order 1008063520870 — Schwab canceled it, the book held it at $0/$0.
    state = _state([
        _ex("exec_047", "close_short", "2026-09-25", strike=144, contracts=1, close_price_per_share=0.0),
        _ex("exec_048", "sell_short", "2026-09-25", strike=141, contracts=1, premium_per_share=0.0),
    ], order_receipts=[{"order_id": "1008063520870", "execution_ids": ["exec_047", "exec_048"]}])
    rep = ha.audit([], state, SINCE, UNTIL)
    assert rep["status"] == "DIRTY" and _kinds(rep) == [ha.PHANTOM, ha.PHANTOM]
    assert {f["execution_id"] for f in rep["findings"]} == {"exec_047", "exec_048"}


def test_another_accounts_fill_in_this_book_is_phantom():
    # LIVE: Christie's 140.32 buy sat in Travis's book; his Schwab shows 144.09 on 9/1.
    feed = [_txn(5, 55, "2026-09-01", _eq("SPCX", 100, 144.09))]
    state = _state([_ex("exec_001", "buy_shares", "2026-09-02", qty=100, price_per_share=140.32)])
    rep = ha.audit(feed, state, SINCE, UNTIL)
    # Same ticker/qty within the date slack pairs them — the price gives it away.
    assert _kinds(rep) == [ha.PRICE]
    assert "144.09" in rep["findings"][0]["summary"]


def test_a_broker_fill_the_book_lacks_is_missing():
    feed = [_txn(7, 77, "2026-09-03",
                 _opt("SPCX", "2026-09-04", 137, 1, 8.20, "CLOSING"),
                 _opt("SPCX", "2026-09-11", 138, -1, 8.60, "OPENING"))]
    rep = ha.audit(feed, _state([]), SINCE, UNTIL)
    assert _kinds(rep) == [ha.MISSING, ha.MISSING]
    assert all(f["order_id"] == "77" for f in rep["findings"])


def test_wrong_price_on_a_matched_fill():
    feed = [_txn(9, 99, "2026-09-22", _opt("SPCX", "2026-09-25", 138, 1, 15.39, "CLOSING"))]
    state = _state([_ex("e1", "close_short", "2026-09-22", strike=138, contracts=1,
                        close_price_per_share=15.00, broker_order_id="99")])
    rep = ha.audit(feed, state, SINCE, UNTIL)
    assert _kinds(rep) == [ha.PRICE]


def test_an_adopted_fill_matches_by_transaction_id_even_with_a_wrong_date():
    feed = [_txn(131330947347, 1008011054795, "2026-09-22",
                 _opt("SPCX", "2026-09-25", 138, 1, 15.39, "CLOSING"))]
    state = _state([_ex("exec_016", "close_short", "2026-09-30", strike=138, contracts=1,
                        close_price_per_share=15.39, transaction_id="131330947347")])
    rep = ha.audit(feed, state, SINCE, "2026-10-05")
    assert rep["status"] == "CLEAN" and rep["matched"] == 1


def test_partial_fills_of_one_order_are_summed():
    feed = [_txn(1, 50, "2026-09-10", _opt("SPCX", "2026-09-18", 140, -1, 11.80, "OPENING")),
            _txn(2, 50, "2026-09-10", _opt("SPCX", "2026-09-18", 140, -1, 11.90, "OPENING"))]
    state = _state([_ex("e1", "sell_short", "2026-09-10", strike=140, contracts=2,
                        premium_per_share=11.85, broker_order_id="50")])
    rep = ha.audit(feed, state, SINCE, UNTIL)
    assert rep["status"] == "CLEAN"


def test_paper_voided_assigned_and_out_of_window_fills_are_not_audited():
    state = _state([
        _ex("paper", "sell_short", "2026-09-10", strike=140, contracts=1, premium_per_share=1,
            mode="logged", live_transmitted=False),
        _ex("void", "sell_short", "2026-09-10", strike=141, contracts=1, premium_per_share=1,
            excluded=True),
        _ex("asg", "close_short", "2026-09-25", strike=144, contracts=1,
            close_price_per_share=0, assigned=True),
        _ex("old", "sell_short", "2026-08-01", strike=150, contracts=1, premium_per_share=1),
        _ex("today", "sell_short", UNTIL, strike=152, contracts=1, premium_per_share=1),
    ])
    rep = ha.audit([], state, SINCE, UNTIL)
    assert rep["book_fills"] == 0 and rep["status"] == "CLEAN"


def test_puts_are_left_to_the_csp_lifecycle():
    put = _opt("SPCX", "2026-09-25", 130, -1, 2.0, "OPENING")
    put["instrument"]["symbol"] = schwab_api.occ_option_symbol("SPCX", "2026-09-25", 130, call=False)
    rep = ha.audit([_txn(3, 33, "2026-09-10", put)], _state([]), SINCE, UNTIL)
    assert rep["status"] == "CLEAN"


def test_run_persists_and_acknowledging_clears_the_finding(store, monkeypatch):
    log.save_state(_state([
        _ex("exec_047", "close_short", "2026-09-25", strike=144, contracts=1, close_price_per_share=0.0)]))
    monkeypatch.setattr(ha, "_window", lambda d, now=None: (SINCE, UNTIL, 30))
    rep = ha.run_history_audit(feed=[])
    assert rep["status"] == "DIRTY"
    fid = rep["findings"][0]["id"]
    assert [a["type"] for a in alerts.check_history_diverged(log.load_state())] == ["HISTORY_DIVERGED"]

    with pytest.raises(ValueError):
        ha.acknowledge(fid, " ")
    ha.acknowledge(fid, "roll canceled at Schwab; voided")
    last = log.load_state()["history_audit"]["last"]
    assert last["status"] == "CLEAN" and last["open"] == 0
    assert alerts.check_history_diverged(log.load_state()) == []
    # The ack survives the next run.
    assert ha.run_history_audit(feed=[])["status"] == "CLEAN"


def test_a_failed_fetch_never_replaces_the_last_verdict(store, monkeypatch):
    log.save_state(_state([]))
    monkeypatch.setattr(ha, "_window", lambda d, now=None: (SINCE, UNTIL, 30))
    ha.run_history_audit(feed=[])

    def _boom(*a, **k):
        raise RuntimeError("schwab down")
    monkeypatch.setattr(ha.ingest, "fetch_transactions", _boom)
    rep = ha.run_history_audit()
    assert rep["status"] == "FAILED"
    st = log.load_state()["history_audit"]
    assert st["last"]["status"] == "CLEAN" and st["last_failed"]["error"] == "schwab down"
