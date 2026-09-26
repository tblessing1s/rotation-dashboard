"""coverage_gaps — uncovered windows and the raw stock P&L they carry."""
from datetime import datetime, timezone

import alerts
import coverage_gaps
import migrations


def _e(i, action, t, px, **kw):
    return {"id": f"e{i}", "ticker": "IBIT", "action": action,
            "date": f"2026-09-21T{t}:00Z", "stock_price": px, **kw}


def _ibit_cycle():
    """Buy shares, leg into a call, leg a roll, buy back, then sell the shares —
    three uncovered stretches totalling -$39 on 100 shares."""
    return [
        _e(1, "buy_shares", "14:00", 50.00, qty=100, price_per_share=50.00),
        _e(2, "sell_short", "14:05", 49.90, strike=43, contracts=1),   # entry gap -10
        _e(3, "close_short", "15:00", 50.20, strike=43, contracts=1),
        _e(4, "sell_short", "15:03", 49.95, strike=44, contracts=1),   # roll gap  -25
        _e(5, "close_short", "18:00", 50.30, strike=44, contracts=1),
        _e(6, "sell_shares", "18:10", 50.26, qty=100),                 # exit gap  -4
    ]


def test_ibit_three_gaps_sum_to_minus_39():
    ws = coverage_gaps.windows_for(_ibit_cycle())
    assert [w["kind"] for w in ws] == ["entry", "roll", "exit"]
    assert [w["gap_pnl"] for w in ws] == [-10.0, -25.0, -4.0]
    assert [w["seconds"] for w in ws] == [300, 180, 600]
    s = coverage_gaps.summarize(ws)
    assert s["gap_pnl"] == -39.0 and s["count"] == 3 and s["open"] is None
    assert s["authority"] == "none"


def test_atomic_roll_leaves_no_window():
    ex = [
        _e(1, "buy_shares", "14:00", 50.0, qty=100),
        _e(2, "sell_short", "14:00", 50.0, strike=43, contracts=1),
        _e(3, "close_short", "15:00", 51.0, strike=43, contracts=1, roll_id="roll_001"),
        _e(4, "sell_short", "15:00", 51.0, strike=44, contracts=1, roll_id="roll_001"),
    ]
    assert coverage_gaps.windows_for(ex) == []


def test_fragment_shares_are_never_a_gap():
    ex = [_e(1, "buy_shares", "14:00", 50.0, qty=150),
          _e(2, "sell_short", "14:00", 50.0, strike=43, contracts=1)]
    assert coverage_gaps.windows_for(ex) == []


def test_open_window_marks_to_live_price():
    ex = _ibit_cycle()[:3]   # bought back the 43C, nothing sold since
    now = datetime(2026, 9, 21, 15, 30, tzinfo=timezone.utc)
    ws = coverage_gaps.windows_for(ex, now=now, live_price=49.70)
    last = ws[-1]
    assert last["open"] and last["kind"] == "roll"
    assert last["gap_pnl"] == -50.0 and last["seconds"] == 1800
    # No live price -> kept, but unpriced (never guessed).
    assert coverage_gaps.windows_for(ex, now=now)[-1]["gap_pnl"] is None


def test_expiry_window_kind():
    ex = [_e(1, "buy_shares", "14:00", 50.0, qty=100),
          _e(2, "sell_short", "14:00", 50.0, strike=48, contracts=1),
          _e(3, "close_short", "20:00", 52.0, strike=48, contracts=1, reason="expired_worthless"),
          {**_e(4, "sell_short", "13:31", 53.0, strike=50, contracts=1),
           "date": "2026-09-24T13:31:00Z"}]
    ws = coverage_gaps.windows_for(ex)
    assert [(w["kind"], w["gap_pnl"]) for w in ws] == [("expiry", 100.0)]


def test_fill_time_wins_over_booked_date():
    ex = _ibit_cycle()[:2]
    ex[1] = {**ex[1], "fill_time": "2026-09-21T14:01:00+0000"}
    assert coverage_gaps.windows_for(ex)[0]["seconds"] == 60


def test_shares_uncovered_alert_fires_once_per_gap(monkeypatch):
    monkeypatch.setattr(alerts, "_last_close", lambda t: 49.70)
    pos = {"ticker": "IBIT", "status": "active", "position_type": "SHARES",
           "shares": {"count": 100}, "short_calls": []}
    state = {"metadata": {}, "positions": [pos], "executions": _ibit_cycle()[:3],
             "alerts": migrations.default_alert_state()}
    out = alerts.check_shares_uncovered(state)
    assert len(out) == 1
    a = out[0]
    assert a["type"] == "SHARES_UNCOVERED"
    assert a["data"]["kind"] == "roll" and a["data"]["gap_pnl"] == -50.0
    assert "2026-09-21T15:00:00" in a["fingerprint"]

    covered = {**pos, "short_calls": [{"strike": 44, "contracts": 1}]}
    assert alerts.check_shares_uncovered({**state, "positions": [covered]}) == []
