"""Tiered market-data scheduler — pure logic.

Every test uses a mocked clock (explicit ``datetime``); no wall-clock, no sleep,
no provider calls. Covers tier assignment transitions, fetch_due cadence per tier
(market open/closed + the once-daily EOD batch), defense + market escalation, the
batching helper, and the queue adapter.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import market_scheduler as ms
from market_scheduler import (
    BARS, CHAIN, QUOTE, EscalationTracker, ListAlertSink, PortfolioState,
    QueueCandidate, QueueState, Tier, assign_tiers, fetch_due, max_age_seconds,
)

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 7, 8, 12, 0, tzinfo=ET)   # a Wednesday, market open


def _q(*cands):
    return QueueState(candidates=tuple(cands))


# ---- 1. Tier assignment transitions ---------------------------------------

def test_tier_assignment_layers():
    ps = PortfolioState(open_symbols=("AAPL",),
                        tracked_universe=("AAPL", "MSFT", "NVDA", "TSLA", "AMD"),
                        sector_etfs=("XLK", "SPY"))
    qs = _q(QueueCandidate("MSFT", 1, True, 0.0),      # on-deck (slot free)
            QueueCandidate("NVDA", 2, True, float("inf")))  # gated-in, no slot
    tiers = assign_tiers(ps, qs, NOON)
    assert tiers["AAPL"] == Tier.T0     # open position
    assert tiers["MSFT"] == Tier.T1     # on-deck
    assert tiers["NVDA"] == Tier.T2     # passes gates, no imminent slot
    assert tiers["TSLA"] == Tier.T3     # tracked tail
    assert tiers["XLK"] == Tier.T3      # sector ETF


def test_transition_gates_pass_promotes_t3_to_t2():
    ps = PortfolioState(tracked_universe=("MSFT",), sector_etfs=())
    # gated-out: not a GO candidate -> only the tracked universe places it at T3
    assert assign_tiers(ps, _q(), NOON)["MSFT"] == Tier.T3
    # gates pass -> T2
    qs = _q(QueueCandidate("MSFT", 1, True, float("inf")))
    assert assign_tiers(ps, qs, NOON)["MSFT"] == Tier.T2


def test_transition_slot_opens_promotes_t2_to_t1():
    ps = PortfolioState(tracked_universe=("MSFT",))
    gated = QueueCandidate("MSFT", 1, True, float("inf"))
    assert assign_tiers(ps, _q(gated), NOON)["MSFT"] == Tier.T2
    ondeck = QueueCandidate("MSFT", 1, True, 0.0)  # slot opens within horizon
    assert assign_tiers(ps, _q(ondeck), NOON)["MSFT"] == Tier.T1


def test_transition_entry_promotes_to_t0_then_exit_demotes():
    # Entered: same name is now an open position -> T0 (beats its queue tier).
    ps_open = PortfolioState(open_symbols=("MSFT",), tracked_universe=("MSFT",))
    qs = _q(QueueCandidate("MSFT", 1, True, 0.0))
    assert assign_tiers(ps_open, qs, NOON)["MSFT"] == Tier.T0
    # Exited: no longer an open position, no longer a candidate -> back to T3/T2.
    ps_closed = PortfolioState(open_symbols=(), tracked_universe=("MSFT",))
    assert assign_tiers(ps_closed, _q(), NOON)["MSFT"] == Tier.T3


def test_ondeck_count_capped(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_ONDECK_COUNT", 2)
    ps = PortfolioState(tracked_universe=("A", "B", "C", "D"))
    qs = _q(*[QueueCandidate(s, i + 1, True, 0.0) for i, s in enumerate("ABCD")])
    tiers = assign_tiers(ps, qs, NOON)
    ondeck = [s for s, t in tiers.items() if t == Tier.T1]
    assert ondeck == ["A", "B"]                      # only top-2 by rank
    assert tiers["C"] == Tier.T2 and tiers["D"] == Tier.T2


def test_slot_horizon_gates_ondeck(monkeypatch):
    monkeypatch.setattr(config, "SLOT_HORIZON_DAYS", 14)
    ps = PortfolioState(tracked_universe=("A", "B"))
    qs = _q(QueueCandidate("A", 1, True, 10.0),     # within horizon -> T1
            QueueCandidate("B", 2, True, 30.0))     # beyond horizon -> T2
    tiers = assign_tiers(ps, qs, NOON)
    assert tiers["A"] == Tier.T1 and tiers["B"] == Tier.T2


# ---- 2. fetch_due cadence --------------------------------------------------

def test_quote_cadence_tier0():
    last = NOON - timedelta(seconds=config.POLL_T0_SECONDS - 1)
    assert fetch_due("AAPL", Tier.T0, QUOTE, True, last, None, NOON) is False   # too soon
    last = NOON - timedelta(seconds=config.POLL_T0_SECONDS + 1)
    assert fetch_due("AAPL", Tier.T0, QUOTE, True, last, None, NOON) is True


def test_quote_cadence_tier1_slower_than_tier0():
    last = NOON - timedelta(seconds=config.POLL_T0_SECONDS + 1)
    # elapsed clears T0's interval but not T1's larger one.
    assert fetch_due("AAPL", Tier.T1, QUOTE, True, last, None, NOON) is False
    last = NOON - timedelta(seconds=config.POLL_T1_SECONDS + 1)
    assert fetch_due("AAPL", Tier.T1, QUOTE, True, last, None, NOON) is True


def test_quotes_never_poll_tier2_tier3():
    old = NOON - timedelta(hours=5)
    assert fetch_due("X", Tier.T2, QUOTE, True, old, None, NOON) is False
    assert fetch_due("X", Tier.T3, QUOTE, True, old, None, NOON) is False


def test_quotes_zero_off_hours():
    old = NOON - timedelta(hours=1)
    assert fetch_due("AAPL", Tier.T0, QUOTE, False, old, None, NOON) is False
    assert fetch_due("AAPL", Tier.T1, QUOTE, False, old, None, NOON) is False


def test_first_ever_quote_is_due():
    assert fetch_due("AAPL", Tier.T0, QUOTE, True, None, None, NOON) is True


def test_eod_batch_fires_exactly_once():
    after_close = datetime(2026, 7, 8, 16, 45, tzinfo=ET)
    # never fetched today -> due
    assert fetch_due("AAPL", Tier.T0, BARS, False, None, None, after_close) is True
    # after the batch runs (last_fetch = now), a later check the same evening is not due
    later = datetime(2026, 7, 8, 17, 30, tzinfo=ET)
    assert fetch_due("AAPL", Tier.T0, BARS, False, after_close, None, later) is False


def test_eod_batch_not_due_before_batch_time():
    before = datetime(2026, 7, 8, 15, 0, tzinfo=ET)   # market still open, pre-16:30
    assert fetch_due("AAPL", Tier.T0, BARS, True, None, None, before) is False


def test_eod_batch_due_next_day_again():
    # fetched yesterday evening; today after close it is due again (once).
    yesterday = datetime(2026, 7, 7, 16, 45, tzinfo=ET)
    today_after = datetime(2026, 7, 8, 16, 45, tzinfo=ET)
    assert fetch_due("AAPL", Tier.T3, BARS, False, yesterday, None, today_after) is True


def test_chains_never_scheduled():
    old = NOON - timedelta(days=2)
    for tier in (Tier.T0, Tier.T1, Tier.T2, Tier.T3):
        assert fetch_due("AAPL", tier, CHAIN, True, old, None, NOON) is False


# ---- max-age derivation ----------------------------------------------------

def test_max_age_derived_from_poll():
    assert max_age_seconds(Tier.T0, QUOTE) == config.POLL_T0_SECONDS * config.MAX_AGE_POLL_MULT
    assert max_age_seconds(Tier.T1, QUOTE) == config.POLL_T1_SECONDS * config.MAX_AGE_POLL_MULT
    # bars / EOD tiers reuse the daily-bar tolerance
    assert max_age_seconds(Tier.T0, BARS) == config.EOD_MAX_AGE_HOURS * 3600.0
    assert max_age_seconds(Tier.T2, QUOTE) == config.EOD_MAX_AGE_HOURS * 3600.0


# ---- 3. Defense escalation -------------------------------------------------

def test_defense_escalation_promotes_and_emits():
    sink = ListAlertSink()
    tr = EscalationTracker(sink=sink)
    levels = {"short_strike": 100.0, "trailing_stop": 95.0}
    # price crosses the short strike (<=100) -> escalate + alert
    alerts = tr.observe_defense("AAPL", levels, 99.0, NOON)
    assert len(alerts) == 1 and alerts[0].level == "short_strike"
    assert sink.alerts[0].kind == "defense"
    assert tr.is_escalated("AAPL", NOON) is True
    # escalated cadence now applies to this symbol
    interval = ms.quote_poll_seconds(Tier.T0, escalated=True)
    assert interval == config.POLL_ESCALATED_SECONDS
    assert fetch_due("AAPL", Tier.T0, QUOTE, True,
                     NOON - timedelta(seconds=config.POLL_ESCALATED_SECONDS + 1),
                     tr.escalated_symbols(NOON), NOON) is True


def test_defense_escalation_edge_triggered_no_spam():
    sink = ListAlertSink()
    tr = EscalationTracker(sink=sink)
    levels = {"short_strike": 100.0}
    tr.observe_defense("AAPL", levels, 99.0, NOON)                  # 1 alert
    tr.observe_defense("AAPL", levels, 98.0, NOON + timedelta(seconds=30))  # still breached, no new alert
    assert len(sink.alerts) == 1
    # recover above, then breach again -> a fresh alert
    tr.observe_defense("AAPL", levels, 101.0, NOON + timedelta(minutes=1))
    tr.observe_defense("AAPL", levels, 99.0, NOON + timedelta(minutes=2))
    assert len(sink.alerts) == 2


def test_defense_escalation_decays():
    tr = EscalationTracker(sink=ListAlertSink())
    tr.observe_defense("AAPL", {"short_strike": 100.0}, 99.0, NOON)
    assert tr.is_escalated("AAPL", NOON + timedelta(minutes=config.ESCALATION_DECAY_MINUTES - 1))
    assert not tr.is_escalated("AAPL", NOON + timedelta(minutes=config.ESCALATION_DECAY_MINUTES + 1))


def test_defense_no_breach_no_escalation():
    tr = EscalationTracker(sink=ListAlertSink())
    alerts = tr.observe_defense("AAPL", {"short_strike": 100.0, "consolidation_low": 90.0}, 105.0, NOON)
    assert alerts == [] and tr.is_escalated("AAPL", NOON) is False


# ---- Regression fixture: XLK-like snapshot ---------------------------------

def test_xlk_regression_triggers_defense():
    """Open position, elevated ATR, price sitting at the consolidation low -> a
    defense escalation must fire (spec regression fixture)."""
    sink = ListAlertSink()
    tr = EscalationTracker(sink=sink)
    price = 182.40
    atr = 6.0  # elevated
    levels = {
        "short_strike": 185.0,                    # already breached below
        "trailing_stop": price - 1.0 * atr,        # 176.4, not yet hit
        "consolidation_low": 182.50,               # price is AT/below the low
        "circuit_breaker": 170.0,
    }
    alerts = tr.observe_defense("XLK", levels, price, NOON)
    fired = {a.level for a in alerts}
    assert "consolidation_low" in fired and "short_strike" in fired
    assert tr.is_escalated("XLK", NOON) is True


# ---- 4. Market escalation --------------------------------------------------

def test_market_escalation_triggers_refresh_flag():
    sink = ListAlertSink()
    tr = EscalationTracker(sink=sink)
    move = config.ESCALATION_INDEX_MOVE_PCT + 0.5
    alert = tr.observe_market({"SPY": -move, "XLK": -0.2}, NOON)
    assert alert is not None and alert.kind == "market" and alert.level == "SPY"
    assert tr.market_active(NOON) is True
    # under a market escalation, any Tier 0/1 symbol reads as escalated -> due now
    assert tr.is_escalated("ANYPOS", NOON) is True
    assert fetch_due("ANYPOS", Tier.T1, QUOTE, True,
                     NOON - timedelta(seconds=config.POLL_ESCALATED_SECONDS + 1),
                     True, NOON) is True


def test_market_escalation_below_threshold_noop():
    tr = EscalationTracker(sink=ListAlertSink())
    small = config.ESCALATION_INDEX_MOVE_PCT - 0.1
    assert tr.observe_market({"SPY": small}, NOON) is None
    assert tr.market_active(NOON) is False


def test_market_escalation_no_respam_then_decay():
    sink = ListAlertSink()
    tr = EscalationTracker(sink=sink)
    move = config.ESCALATION_INDEX_MOVE_PCT + 1.0
    tr.observe_market({"SPY": -move}, NOON)
    tr.observe_market({"SPY": -move}, NOON + timedelta(seconds=30))  # still moving: extend, no re-alert
    assert len(sink.alerts) == 1
    assert not tr.market_active(NOON + timedelta(minutes=config.ESCALATION_DECAY_MINUTES + 2))


# ---- 6. Batching invariant (scheduler surface) -----------------------------

def test_due_quote_symbols_batch_to_one_request():
    """N Tier 0/1 symbols whose cadence is due collapse to ONE batched set — the
    scheduler issues a single quote request for the union."""
    tiers = {"AAPL": Tier.T0, "MSFT": Tier.T0, "NVDA": Tier.T1,
             "SPY": Tier.T2, "XLK": Tier.T3}
    last = {s: NOON - timedelta(hours=1) for s in tiers}  # all stale
    due = [s for s, t in tiers.items()
           if fetch_due(s, t, QUOTE, True, last[s], None, NOON)]
    assert set(due) == {"AAPL", "MSFT", "NVDA"}  # T2/T3 excluded
    # the transport batches these into one call — see test_data_transport.


# ---- Queue adapter ---------------------------------------------------------

def test_queue_adapter_ranks_and_slots(monkeypatch):
    import queue_state
    rows = [
        {"ticker": "AAA", "suitability": "GO", "juice_weekly_pct": 1.2},
        {"ticker": "BBB", "suitability": "GO", "juice_weekly_pct": 2.5},
        {"ticker": "CCC", "verdict": "CAUTION", "juice_weekly_pct": 3.0},  # not GO
    ]
    monkeypatch.setattr(queue_state, "_cached_scorecard_rows", lambda: rows)
    monkeypatch.setattr(queue_state.maintenance, "open_tickers", lambda state=None: [])
    qs = queue_state.build_queue_state(state={})
    syms = [c.symbol for c in qs.candidates]
    assert syms == ["BBB", "AAA"]            # juice desc, CAUTION dropped
    assert qs.candidates[0].rank == 1 and qs.candidates[0].gates_passed
    assert qs.candidates[0].slot_opens_within_days == 0.0   # a slot is free (0 open)


def test_queue_adapter_no_free_slot(monkeypatch):
    import queue_state
    rows = [{"ticker": "AAA", "suitability": "GO", "juice_weekly_pct": 1.0}]
    monkeypatch.setattr(queue_state, "_cached_scorecard_rows", lambda: rows)
    # book full -> no free slot -> slot_opens_within_days is inf (not on-deck)
    monkeypatch.setattr(queue_state.maintenance, "open_tickers",
                        lambda state=None: ["X"] * config.MAX_CFM_POSITIONS)
    qs = queue_state.build_queue_state(state={})
    assert qs.candidates[0].slot_opens_within_days == float("inf")
