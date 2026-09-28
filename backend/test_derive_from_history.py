"""Figures a fill inherits from an EARLIER fill are derived from the whole log
in date order (logging_handler._derive_from_history), never frozen at booking:
a buy-back's extrinsic sold, and a share sale's cost basis / realized P&L.

Every case is a live one: fills adopted out of order froze a $0 basis and a 0
extrinsic, and a buy-back kept a stale copy after its opening sale was fixed."""
from __future__ import annotations

import logging_handler as log


def _ex(eid, action, date, **kw):
    e = {"id": eid, "ticker": "SPCX", "action": action, "date": date, "mode": "live"}
    e.update(kw)
    return e


def _by_id(state):
    return {e["id"]: e for e in log.derived_executions(state)}


def test_a_sale_imported_before_its_purchase_gets_the_real_cost_basis():
    # LIVE (Travis, IBIT): the 9/21 sale was adopted before the 9/8 buy and froze
    # a $0 basis — $4,876 "profit" on what was a $425 trade.
    state = {"executions": [
        _ex("exec_018", "sell_shares", "2026-09-21T18:19:52Z", ticker="IBIT", qty=100,
            price_per_share=48.7601, execution_total=4876.01,
            cost_basis_per_share=0.0, realized_pnl=4876.01),
        _ex("exec_083", "buy_shares", "2026-09-08T18:47:02Z", ticker="IBIT", qty=100,
            price_per_share=44.51),
    ]}
    sale = _by_id(state)["exec_018"]
    assert sale["cost_basis_per_share"] == 44.51
    assert sale["realized_pnl"] == 425.01
    # The immutable log itself is untouched.
    assert state["executions"][0]["realized_pnl"] == 4876.01


def test_a_buy_back_imported_before_its_sale_gets_the_real_extrinsic_sold():
    state = {"executions": [
        _ex("c1", "close_short", "2026-09-22T13:52:01Z", strike=138, contracts=1,
            expiration="2026-09-25", close_price_per_share=15.39, stock_price=153.25,
            extrinsic_sold=0.0, net_juice_total=-14.0),
        _ex("o1", "sell_short", "2026-09-16T13:47:41Z", strike=138, contracts=1,
            expiration="2026-09-25", premium_per_share=12.69, entry_extrinsic_per_share=1.4797),
    ]}
    close = _by_id(state)["c1"]
    assert close["extrinsic_sold"] == 1.4797 and close["extrinsic_sold_from"] == "o1"
    # paid back = 15.39 - (153.25 - 138) = 0.14 -> net (1.4797 - 0.14) * 100
    assert close["extrinsic_paid_back"] == 0.14
    assert close["net_juice_total"] == 133.97


def test_fixing_the_opening_sale_flows_to_its_buy_back():
    # LIVE (Travis, 137C): the sale's stock price was corrected (extrinsic 7.82 ->
    # 0.73) and the buy-back kept its stale 7.82 copy until edited by hand.
    state = {"executions": [
        _ex("o1", "sell_short", "2026-09-01T15:42:01Z", strike=137, contracts=1,
            expiration="2026-09-04", premium_per_share=7.82, entry_extrinsic_per_share=7.82),
        _ex("c1", "close_short", "2026-09-03T13:39:23Z", strike=137, contracts=1,
            expiration="2026-09-04", close_price_per_share=8.20, stock_price=144.94,
            extrinsic_sold=7.82),
        {"id": "x1", "action": log.TXN_CORRECTION_ACTION, "ticker": "SPCX", "corrects": "o1",
         "changes": {"stock_price": 144.09, "entry_extrinsic_per_share": 0.73}},
    ]}
    assert _by_id(state)["c1"]["extrinsic_sold"] == 0.73


def test_an_explicit_history_correction_of_the_field_wins():
    state = {"executions": [
        _ex("o1", "sell_short", "2026-09-16", strike=138, contracts=1, entry_extrinsic_per_share=1.48),
        _ex("c1", "close_short", "2026-09-22", strike=138, contracts=1, close_price_per_share=15.39,
            stock_price=153.25, extrinsic_sold=0.0),
        _ex("b1", "buy_shares", "2026-09-01", qty=100, price_per_share=144.09),
        _ex("s1", "sell_shares", "2026-09-30", qty=100, execution_total=15000.0,
            cost_basis_per_share=0.0, realized_pnl=15000.0),
        {"id": "x1", "action": log.TXN_CORRECTION_ACTION, "corrects": "c1",
         "changes": {"extrinsic_sold": 1.2}},
        {"id": "x2", "action": log.TXN_CORRECTION_ACTION, "corrects": "s1",
         "changes": {"cost_basis_per_share": 140.0, "realized_pnl": 1000.0}},
    ]}
    got = _by_id(state)
    assert got["c1"]["extrinsic_sold"] == 1.2
    assert got["s1"]["cost_basis_per_share"] == 140.0 and got["s1"]["realized_pnl"] == 1000.0


def test_history_before_the_book_began_keeps_the_stored_figures():
    state = {"executions": [
        _ex("c1", "close_short", "2026-09-03", strike=133, contracts=1, extrinsic_sold=1.53,
            net_juice_total=94.68),
        _ex("s1", "sell_shares", "2026-09-05", qty=100, execution_total=15000.0,
            cost_basis_per_share=140.32, realized_pnl=968.0),
    ]}
    got = _by_id(state)
    assert got["c1"]["extrinsic_sold"] == 1.53 and "extrinsic_sold_from" not in got["c1"]
    assert got["s1"]["realized_pnl"] == 968.0 and "cost_basis_from" not in got["s1"]


def test_buy_backs_pair_fifo_and_prefer_the_same_expiry():
    state = {"executions": [
        _ex("o1", "sell_short", "2026-09-01T10:00:00Z", strike=140, contracts=1,
            expiration="2026-09-11", entry_extrinsic_per_share=1.0),
        _ex("o2", "sell_short", "2026-09-08T10:00:00Z", strike=140, contracts=1,
            expiration="2026-09-18", entry_extrinsic_per_share=2.0),
        # Same strike, no expiry recorded -> the oldest lot.
        _ex("c1", "close_short", "2026-09-09T10:00:00Z", strike=140, contracts=1),
        # Expiry recorded -> that lot, not the next FIFO one.
        _ex("c2", "close_short", "2026-09-10T10:00:00Z", strike=140, contracts=1,
            expiration="2026-09-18"),
    ]}
    got = _by_id(state)
    assert got["c1"]["extrinsic_sold_from"] == "o1"
    assert got["c2"]["extrinsic_sold_from"] == "o2"


def test_an_assigned_call_keeps_all_the_extrinsic_it_sold():
    state = {"executions": [
        _ex("o1", "sell_short", "2026-09-17", strike=144, contracts=1, entry_extrinsic_per_share=1.02),
        _ex("c1", "close_short", "2026-09-25T21:00:00Z", strike=144, contracts=1,
            close_price_per_share=0.0, assigned=True, extrinsic_sold=0.0),
    ]}
    c = _by_id(state)["c1"]
    assert c["extrinsic_sold"] == 1.02 and c["net_juice_total"] == 102.0


def test_same_second_sell_then_rebuy_keeps_booking_order():
    state = {"executions": [
        _ex("exec_1", "buy_shares", "2026-09-01T10:00:00Z", qty=100, price_per_share=60.0),
        _ex("exec_2", "sell_shares", "2026-09-01T10:00:00Z", qty=100, execution_total=5500.0,
            cost_basis_per_share=60.0, realized_pnl=-500.0),
        _ex("exec_3", "buy_shares", "2026-09-01T10:00:00Z", qty=100, price_per_share=56.0),
    ]}
    assert _by_id(state)["exec_2"]["realized_pnl"] == -500.0


def test_the_derived_figures_reach_the_cycle_log_and_theta_ledger():
    state = {"executions": [
        _ex("exec_9", "sell_shares", "2026-09-21T18:19:52Z", ticker="IBIT", qty=100,
            execution_total=4876.01, cost_basis_per_share=0.0, realized_pnl=4876.01),
        _ex("exec_1", "buy_shares", "2026-09-08T18:47:02Z", ticker="IBIT", qty=100,
            price_per_share=44.51, execution_total=4451.0),
    ], "positions": []}
    log.recompute_derived(state)
    cyc = [c for c in state.get("cycles") or [] if c.get("ticker") == "IBIT"]
    assert cyc and cyc[0]["leap_pnl"] == 425.01
