"""position_manager.whipsaw_status — the cumulative DEFENSIVE-whipsaw guard.

Both trip conditions (too-many-rolls, cumulative drag) must be scoped to
reason=="defend" rolls only. A debit paid on a routine roll-up-for-more-
extrinsic (reason="scheduled"/"75%-rule") is an ordinary cashflow-management
cost, not the defensive "roll-down after roll-down" grind this guard exists to
catch, and must never trip it — even though it's a real, negative-net roll."""
from datetime import date, timedelta

import config
import position_manager as pm


def _pos(ticker="SPCX", entry_date=None, cost_basis_per_share=140.32, count=100):
    return {
        "ticker": ticker, "entry_date": entry_date,
        "shares": {"count": count, "cost_basis_per_share": cost_basis_per_share},
        "leap": None, "short_puts": [],
    }


def _roll(ticker, days_ago, reason, net):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    return {"roll_id": f"{ticker}-{days_ago}-{reason}", "ticker": ticker, "date": d,
            "reason": reason, "net": net}


def test_scheduled_debit_roll_never_trips_drag():
    """The exact scenario reported: rolling because extrinsic is running out
    (reason="scheduled"), not defense — a real debit, but must not count."""
    pos = _pos()
    # -895 on a $14,032 position is 6.4% — well past the 5% WHIPSAW_DRAG_PCT
    # floor if it counted. It must not.
    rolls = [_roll("SPCX", 2, "scheduled", -895.0)]
    out = pm.whipsaw_status(pos, rolls)
    assert out["roll_drag"] == 0.0
    assert out["drag_pct"] == 0.0
    assert out["drag_trip"] is False
    assert out["tripped"] is False


def test_75pct_rule_debit_roll_never_trips_drag():
    pos = _pos()
    rolls = [_roll("SPCX", 2, "75%-rule", -895.0)]
    out = pm.whipsaw_status(pos, rolls)
    assert out["roll_drag"] == 0.0
    assert out["drag_trip"] is False


def test_defend_debit_roll_still_trips_drag():
    """The guard must still work for its actual purpose: a real defensive
    (rescue) debit still counts and can still trip EXIT."""
    pos = _pos()
    rolls = [_roll("SPCX", 2, "defend", -895.0)]
    out = pm.whipsaw_status(pos, rolls)
    assert out["roll_drag"] == -895.0
    assert out["drag_pct"] == 6.4
    assert out["drag_trip"] is True
    assert out["tripped"] is True
    assert "cumulative roll drag $895" in out["reasons"][0]


def test_mixed_rolls_only_defend_debits_count_toward_drag():
    pos = _pos()
    rolls = [
        _roll("SPCX", 1, "scheduled", -400.0),   # excluded
        _roll("SPCX", 3, "75%-rule", -300.0),    # excluded
        _roll("SPCX", 5, "defend", -200.0),      # included
        _roll("SPCX", 7, "defend", 50.0),        # included but a credit, not summed
    ]
    out = pm.whipsaw_status(pos, rolls)
    assert out["roll_drag"] == -200.0


def test_rolls_trip_was_already_defend_scoped_and_stays_so():
    # Regression: the too-many-rolls leg was already reason=="defend"-scoped
    # before this fix; confirm it's unaffected — 3 scheduled rolls must not
    # trip it, 3 defend rolls in the window must.
    pos = _pos()
    scheduled_rolls = [_roll("SPCX", d, "scheduled", -10.0) for d in (1, 8, 15)]
    assert pm.whipsaw_status(pos, scheduled_rolls)["rolls_trip"] is False

    defend_rolls = [_roll("SPCX", d, "defend", -10.0) for d in (1, 8, 15)]
    out = pm.whipsaw_status(pos, defend_rolls)
    assert out["rolls_trip"] is True
    assert out["defensive_rolls"] == 3


def test_other_tickers_and_prior_cycle_rolls_are_still_excluded():
    # Ticker scoping and entry-date cycle scoping are unaffected by this fix.
    pos = _pos(entry_date=(date.today() - timedelta(days=10)).isoformat())
    rolls = [
        _roll("OTHER", 2, "defend", -895.0),                              # wrong ticker
        {**_roll("SPCX", 30, "defend", -895.0)},                           # before entry (30d ago > 10d entry)
    ]
    out = pm.whipsaw_status(pos, rolls)
    assert out["roll_drag"] == 0.0
    assert out["tripped"] is False
