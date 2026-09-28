"""One broker fill, one book — cross_book.py.

CONFIRMED LIVE: one Schwab login reaches two accounts; a secondary book adopted
the primary's IBIT trades, and the primary carried the secondary's early SPCX
history. Each book looked consistent on its own."""
from __future__ import annotations

import pytest

import accounts
import alerts
import config
import cross_book
import executor
import logging_handler as log


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(accounts, "registry_path", lambda: str(tmp_path / "accounts.json"))
    accounts.create("Christie", broker_account_number="12902569")
    return tmp_path


def _ex(eid, **kw):
    e = {"id": eid, "ticker": "IBIT", "action": "sell_short", "strike": 44, "contracts": 1,
         "date": "2026-09-18T13:55:58Z", "mode": "live"}
    e.update(kw)
    return e


def _save(account_id, execs, **extra):
    with accounts.use(account_id):
        st = log.load_state()
        st["executions"] = execs
        st.update(extra)
        log.save_state(st)


def test_broker_ids_come_from_live_executions_and_their_receipts():
    state = {"executions": [
        _ex("e1", transaction_id="131065102930", broker_order_id="1007975307557"),
        _ex("e2", transaction_id="999", excluded=True),                 # voided: ignored
    ], "order_receipts": [
        {"order_id": "1008063520870", "execution_ids": ["e1"]},
        {"order_id": "1007821979111", "execution_ids": ["e2"]},         # voided exec: ignored
    ]}
    assert set(cross_book.broker_ids(state)) == {
        "txn:131065102930", "order:1007975307557", "order:1008063520870"}


def test_the_same_broker_fill_in_two_books_is_found():
    a = {"executions": [_ex("exec_014", transaction_id="131065102930")]}
    b = {"executions": [_ex("exec_014", transaction_id="131065102930"),
                        _ex("exec_020", transaction_id="777")]}
    dups = cross_book.find_duplicates({"primary": a, "christie": b})
    assert [(d["id"], sorted(d["books"])) for d in dups] == [
        ("txn:131065102930", ["christie", "primary"])]


def test_a_copy_voided_in_one_book_is_not_a_duplicate():
    a = {"executions": [_ex("exec_014", transaction_id="131065102930")]}
    b = {"executions": [_ex("exec_014", transaction_id="131065102930", excluded=True)]}
    assert cross_book.find_duplicates({"primary": a, "christie": b}) == []


def test_adopting_a_trade_another_book_holds_is_refused(store):
    _save("primary", [_ex("exec_014", transaction_id="131065102930",
                          broker_order_id="1007975307557")])
    _save("christie", [], ingestion={"proposals": [{
        "proposal_id": "adopt_1007975307557", "ticker": "IBIT",
        "order_id": "1007975307557", "transaction_ids": ["131065102930"],
        "legs": [{"transaction_id": "131065102930", "asset_type": "OPTION",
                  "amount": -1, "price": 1.73, "strike": 44, "expiry": "2026-09-25",
                  "position_effect": "OPENING", "underlying": "IBIT"}]}]})
    with accounts.use("christie"):
        with pytest.raises(ValueError, match="already booked in Travis|already booked in"):
            executor.adopt_broker_trade("adopt_1007975307557")
        assert log.load_state()["executions"] == []


def test_daily_check_records_on_each_book_alerts_and_can_be_acknowledged(store):
    shared = dict(transaction_id="131065102930")
    _save("primary", [_ex("exec_014", **shared)])
    _save("christie", [_ex("exec_014", **shared)])
    rep = cross_book.run_and_persist()
    assert rep["status"] == "DIRTY" and len(rep["duplicates"]) == 1

    for acct in ("primary", "christie"):
        with accounts.use(acct):
            st = log.load_state()
            assert st["cross_book"]["status"] == "DIRTY"
            assert [a["type"] for a in alerts.check_cross_book_duplicate(st)] == ["CROSS_BOOK_DUPLICATE"]

    with accounts.use("primary"):
        with pytest.raises(ValueError):
            cross_book.acknowledge("txn:131065102930", "")
        cross_book.acknowledge("txn:131065102930", "receipt left from before the books split")
        assert alerts.check_cross_book_duplicate(log.load_state()) == []
    cross_book.run_and_persist()                       # the ack survives the next run
    with accounts.use("primary"):
        assert log.load_state()["cross_book"]["status"] == "CLEAN"
    with accounts.use("christie"):                     # …and is per book
        assert log.load_state()["cross_book"]["status"] == "DIRTY"

    # Voiding the copy clears it everywhere.
    with accounts.use("christie"):
        executor.void_executions(["exec_014"], "belongs to Travis's account")
    cross_book.run_and_persist()
    with accounts.use("christie"):
        assert log.load_state()["cross_book"]["status"] == "CLEAN"
