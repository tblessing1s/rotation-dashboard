"""pnl_breakdown — underlying vs short-call P/L, per ticker and overall."""
import pnl_breakdown


def _e(i, action, t, px, **kw):
    return {"id": f"e{i}", "ticker": "IBIT", "action": action,
            "date": f"2026-09-21T{t}:00Z", "stock_price": px, **kw}


def test_closed_cycle_splits_shares_and_calls():
    ex = [
        _e(1, "buy_shares", "14:00", 50.0, qty=100, price_per_share=50.0),
        _e(2, "sell_short", "14:05", 49.9, strike=43, contracts=1, premium_total=120.0),
        _e(3, "close_short", "15:00", 50.2, strike=43, contracts=1, close_total=70.0),
        _e(4, "sell_shares", "18:10", 50.26, qty=100,
           price_per_share=49.70, execution_total=4970.0),
    ]
    r = pnl_breakdown.build({"executions": ex, "positions": []})
    t = r["tickers"][0]
    assert t["calls_realized"] == 50.0 and t["shares_realized"] == -30.0
    assert t["total"] == 20.0 and r["totals"]["total"] == 20.0
    assert t["uncovered_gap"] == round(t["uncovered_gap"], 2)
    assert t["shares_while_covered"] == round(-30.0 - t["uncovered_gap"], 2)


def test_open_position_marks_both_legs():
    ex = [
        _e(1, "buy_shares", "14:00", 50.0, qty=100, price_per_share=50.0),
        _e(2, "sell_short", "14:00", 50.0, strike=55, contracts=1, premium_total=100.0),
    ]
    view = {"ticker": "IBIT", "status": "active", "stock_price": 48.0,
            "shares": {"count": 100, "cost_basis_per_share": 50.0},
            "short_calls": [{"contracts": 1, "current_bid": 0.4,
                             "entry_premium_total": 100.0}]}
    t = pnl_breakdown.build({"executions": ex, "positions": []}, [view])["tickers"][0]
    assert t["shares_unrealized"] == -200.0
    assert t["calls_realized"] == 0.0 and t["calls_unrealized"] == 60.0
    assert t["total"] == -140.0
