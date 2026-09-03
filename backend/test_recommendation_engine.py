"""Recommendation engine tests — fully offline, pure snapshots, mocked clock.

The engine is the same code path a future automation switch would call, so
these tests are the trust layer's own trust layer: the AAPL laggard case and
the XLK July-6th labeled failure case are regression-locked here.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-rec-test-"))

import config  # noqa: E402
import recommendation_engine as engine  # noqa: E402
from rec_types import ActionType, TriggerRule  # noqa: E402

NOW = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)


def _frame(values, start="2026-03-01", vol=1e6):
    idx = pd.bdate_range(start, periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c,
                         "Volume": vol}, index=idx)


def _position(ticker="AAPL", short_strike=180.0, short_dte=4, leap_dte=170,
              current_bid=4.5, entry_premium_total=500.0, contracts=1):
    return {
        "ticker": ticker, "status": "active", "entry_date": "2026-06-01",
        "leap_dte": leap_dte, "planned_exit_dte": 135,
        # Deep ITM (~0.9 delta) so the LEAP genuinely covers the weekly short.
        "leap": {"strike": 130.0, "contracts": contracts, "dte": leap_dte + 170,
                 "expiration": "2027-01-15", "current_bid": 5600.0,
                 "cost_basis": 5400.0},
        "leap_legs": [{"strike": 130.0, "contracts": contracts, "dte": leap_dte + 170,
                       "expiration": "2027-01-15", "current_bid": 5600.0,
                       "cost_basis": 5400.0}],
        "short_calls": [{"strike": short_strike, "contracts": contracts,
                         "dte": short_dte, "expiration": "2026-07-17",
                         "current_bid": current_bid,
                         "entry_premium_total": entry_premium_total,
                         "open_date": "2026-07-06"}],
        "circuit_breaker": {"price": 100.0, "source": "manual",
                            "entry_price": 185.0},
        "dividend": None,
    }


def _healthy_tk(price=182.0):
    # Slightly ITM vs the default 180 short strike, with the short's mark (4.5)
    # comfortably above intrinsic (2.0) so no extrinsic-collapse trigger fires.
    bars = _frame([170 + i * 0.25 for i in range(90)])
    return {
        "price": price, "last_close": price - 0.3, "atr": 3.0, "hist_vol": 0.30,
        "rs3m_vs_spy": 8.0, "rs3m_vs_sector": 4.0, "q": 0.0,
        "bars": bars, "spy_bars": _frame([100.0] * 90),
        "earnings": {"date": None, "warning": False}, "juice": {"inadequate": False},
    }


def _market(tickers, regime="green", posture="conservative", candidates=None):
    return {"as_of": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "regime": {"status": regime}, "posture": posture,
            "tickers": {k.upper(): v for k, v in tickers.items()},
            "entry_candidates": candidates or [], "roll_ledger": []}


def _state(positions):
    return {"positions": positions, "roll_ledger": {"rolls": []}}


# ---------------------------------------------------------------------------
# AAPL laggard: RS3M-vs-SPY negative -> EXIT with KILL_RS_SPY_CONFIRMED.
#
# Was test_aapl_laggard_emits_exit_kill_rs_sector_on_first_pass, driven by a
# negative rs3m_vs_sector. That trigger was removed 2026-08-21
# (docs/decision-2026-08-21-remove-sector-rs.md), so the same laggard shape is
# re-pinned to the SPY leg — the only relative-strength exit that remains. The
# assertions are otherwise unchanged and equally strict.
# ---------------------------------------------------------------------------
def test_aapl_laggard_emits_exit_kill_rs_spy_on_first_pass():
    tk = _healthy_tk()
    tk["rs3m_vs_spy"] = -1.5
    # RELATIVE weakness without absolute damage: the name still rises, SPY rises
    # faster. That keeps the circuit breaker (drawdown / MA breaches) out of the
    # picture, so the SPY kill-switch rule is unambiguously the dominant trigger
    # — _EXIT_PRIORITY puts CIRCUIT_BREAKER above it.
    tk["bars"] = _frame([200 + i * 0.05 for i in range(90)])
    tk["spy_bars"] = _frame([100 + i * 0.9 for i in range(90)])
    recs = engine.evaluate(_market({"AAPL": tk}), _state([_position("AAPL")]), NOW, [])
    exits = [r for r in recs if r["action_type"] == ActionType.EXIT]
    assert len(exits) == 1, f"expected one EXIT, got {recs}"
    rec = exits[0]
    assert rec["trigger_rule"] == TriggerRule.KILL_RS_SPY_CONFIRMED
    assert rec["position_id"] == "AAPL"
    assert rec["proposed_ticket"]["action"] == "close_position"
    assert rec["proposed_ticket"]["exit_reason_code"] == "KILL_SWITCH_SPY"
    legs = rec["proposed_ticket"]["legs"]
    assert {(l["instruction"], l["role"]) for l in legs} == {
        ("SELL_TO_CLOSE", "leap"), ("BUY_TO_CLOSE", "short")}
    assert rec["proposed_ticket"]["max_slippage_pct_of_mid"] == config.REC_MAX_SLIPPAGE_PCT_OF_MID
    assert rec["valid_until"] > rec["emitted_at"]
    # the declining fixture's first negative-RS day is recorded for timeliness
    assert rec["input_snapshot"]["condition_first_true_at"] is not None
    # no ALL_CLEAR alongside an action recommendation for the same position
    assert not [r for r in recs if r["action_type"] == ActionType.NO_ACTION]


def test_kill_rs_spy_confirmed_for_an_etf_position():
    tk = _healthy_tk()
    tk["rs3m_vs_spy"] = -0.5
    recs = engine.evaluate(_market({"XLK": tk}), _state([_position("XLK")]), NOW, [])
    assert recs[0]["trigger_rule"] == TriggerRule.KILL_RS_SPY_CONFIRMED
    assert recs[0]["action_type"] == ActionType.EXIT


# ---------------------------------------------------------------------------
# ALL_CLEAR: a healthy position gets an explicit no-action record
# ---------------------------------------------------------------------------
def test_all_clear_emitted_for_healthy_position():
    recs = engine.evaluate(_market({"AAPL": _healthy_tk()}),
                           _state([_position("AAPL")]), NOW, [])
    assert len(recs) == 1
    rec = recs[0]
    assert rec["action_type"] == ActionType.NO_ACTION
    assert rec["trigger_rule"] == TriggerRule.ALL_CLEAR
    assert rec["proposed_ticket"] is None
    assert rec["position_id"] == "AAPL"


def test_no_duplicate_within_validity_window():
    market = _market({"AAPL": _healthy_tk()})
    state = _state([_position("AAPL")])
    first = engine.evaluate(market, state, NOW, [])
    assert len(first) == 1
    first[0]["rec_id"] = "rec_00001"  # as the writer would assign
    again = engine.evaluate(market, state, NOW + timedelta(hours=1), first)
    assert again == []   # the open record is the claim — crash-recovery safe


# ---------------------------------------------------------------------------
# Defend / roll triggers
# ---------------------------------------------------------------------------
def test_defend_below_strike_uses_strike_policy_and_records_first_true():
    tk = _healthy_tk(price=170.0)
    tk["last_close"] = 175.5   # below the 180 short strike, live price confirms
    # A gentle uptrend that never reaches the 180 strike: below-strike fires,
    # while the closes stay ABOVE the 50-day MA so the circuit breaker's
    # 3-closes rule does not — this pins DEFEND as the dominant action.
    closes = [168 + i * 0.1 for i in range(87)] + [176.2, 175.9, 175.5]
    tk["bars"] = _frame(closes)
    recs = engine.evaluate(_market({"AAPL": tk}), _state([_position("AAPL")]), NOW, [])
    assert len(recs) == 1
    rec = recs[0]
    assert rec["action_type"] == ActionType.DEFEND
    assert rec["trigger_rule"] == TriggerRule.DEFEND_BELOW_STRIKE
    ticket = rec["proposed_ticket"]
    assert ticket["action"] == "roll_short"
    assert ticket["roll_reason"] == "defend"
    import strike_policy
    pol = strike_policy.suggest_strike(170.0, 3.0, "green", "conservative")
    sto = [l for l in ticket["legs"] if l["instruction"] == "SELL_TO_OPEN"][0]
    assert sto["strike"] == pol["strike"]   # single source of proposed strikes
    assert rec["input_snapshot"]["condition_first_true_at"] is not None
    assert ticket["min_acceptable_net_credit"] is not None


def test_roll_75pct_emits_roll_out():
    # sold 5.00/sh, now 1.15/sh -> 77% decayed with 4 DTE (>2); price 181 keeps
    # extrinsic (1.15 - 1.00 intrinsic) above the assignment floor so the 75%
    # rule is the dominant trigger, not extrinsic collapse.
    p = _position("AAPL", current_bid=1.15, entry_premium_total=500.0, short_dte=4)
    tk = _healthy_tk(price=181.0)
    recs = engine.evaluate(_market({"AAPL": tk}), _state([p]), NOW, [])
    assert recs[0]["action_type"] == ActionType.ROLL_OUT
    assert recs[0]["trigger_rule"] == TriggerRule.ROLL_75PCT
    assert recs[0]["proposed_ticket"]["roll_reason"] == "75%-rule"


def test_scheduled_weekly_roll_when_expiry_imminent():
    p = _position("AAPL", short_dte=1)
    recs = engine.evaluate(_market({"AAPL": _healthy_tk()}), _state([p]), NOW, [])
    assert recs[0]["trigger_rule"] == TriggerRule.ROLL_SCHEDULED_WEEKLY
    assert recs[0]["action_type"] == ActionType.ROLL_OUT


def test_exit_dominates_roll_triggers():
    tk = _healthy_tk()
    tk["rs3m_vs_spy"] = -2.0             # was rs3m_vs_sector; re-pointed to the SPY leg
    p = _position("AAPL", short_dte=1)   # scheduled roll ALSO fired
    recs = engine.evaluate(_market({"AAPL": tk}), _state([p]), NOW, [])
    assert len(recs) == 1
    assert recs[0]["action_type"] == ActionType.EXIT
    assert TriggerRule.ROLL_SCHEDULED_WEEKLY in recs[0]["input_snapshot"]["secondary_triggers"]


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------
def test_reevaluation_supersedes_open_recommendation():
    tk_kill = _healthy_tk()
    tk_kill["rs3m_vs_spy"] = -2.0        # was rs3m_vs_sector; re-pointed to the SPY leg
    market1 = _market({"AAPL": tk_kill})
    state = _state([_position("AAPL")])
    first = engine.evaluate(market1, state, NOW, [])
    first[0]["rec_id"] = "rec_00001"
    # next pass: kill cleared, but the stock closed below the short strike
    tk_defend = _healthy_tk(price=170.0)
    tk_defend["last_close"] = 171.0
    later = NOW + timedelta(hours=3)
    second = engine.evaluate(_market({"AAPL": tk_defend}), state, later, first)
    assert len(second) == 1
    assert second[0]["action_type"] == ActionType.DEFEND
    assert second[0]["supersedes"] == "rec_00001"


def test_condition_cleared_supersedes_with_all_clear():
    tk_kill = _healthy_tk()
    tk_kill["rs3m_vs_spy"] = -2.0        # was rs3m_vs_sector; re-pointed to the SPY leg
    state = _state([_position("AAPL")])
    first = engine.evaluate(_market({"AAPL": tk_kill}), state, NOW, [])
    first[0]["rec_id"] = "rec_00001"
    second = engine.evaluate(_market({"AAPL": _healthy_tk()}), state,
                             NOW + timedelta(hours=3), first)
    assert len(second) == 1
    assert second[0]["action_type"] == ActionType.NO_ACTION
    assert second[0]["supersedes"] == "rec_00001"


# ---------------------------------------------------------------------------
# ENTER — worst-signal-wins over the frozen candidate list
# ---------------------------------------------------------------------------
def _candidate(ticker="MSFT", verdict="GO", l5_pass=True, blockers=None):
    return {"ticker": ticker, "verdict": verdict,
            "level5": {"pass": l5_pass,
                       "blocking_failures": [] if l5_pass else [{"id": "cash_reserve"}]},
            "juice_weekly_pct": 2.1, "blockers": blockers or []}


def test_enter_emitted_when_every_gate_clear():
    tk = _healthy_tk()
    recs = engine.evaluate(_market({"MSFT": tk}, candidates=[_candidate()]),
                           _state([]), NOW, [])
    enters = [r for r in recs if r["action_type"] == ActionType.ENTER]
    assert len(enters) == 1
    assert enters[0]["trigger_rule"] == TriggerRule.GATE_ALL_PASS
    assert enters[0]["position_id"] is None
    # Shares-primary entry (schema v20): the base leg is real shares, not a LEAP.
    ticket = enters[0]["proposed_ticket"]
    assert ticket["action"] == "buy_shares"
    assert ticket["qty"] == ticket["contracts"] * config.SHARES_PER_LOT
    roles = {(l["instruction"], l["role"]) for l in ticket["legs"]}
    assert roles == {("BUY", "shares"), ("SELL_TO_OPEN", "short")}
    assert not any(l["role"] == "leap" for l in ticket["legs"])
    assert ticket["covering_short"]["action"] == "sell_short"


@pytest.mark.parametrize("candidate,regime", [
    (_candidate(verdict="CAUTION"), "green"),      # scorecard worst signal
    (_candidate(verdict="AVOID"), "green"),
    (_candidate(l5_pass=False), "green"),          # Level-5 blocking failure
    (_candidate(), "yellow"),                      # Level-1 regime not green
    (_candidate(), "red"),
])
def test_enter_blocked_by_any_worst_signal(candidate, regime):
    recs = engine.evaluate(_market({"MSFT": _healthy_tk()}, regime=regime,
                                   candidates=[candidate]), _state([]), NOW, [])
    assert not [r for r in recs if r["action_type"] == ActionType.ENTER]


def test_enter_not_emitted_for_already_open_position():
    recs = engine.evaluate(
        _market({"AAPL": _healthy_tk()}, candidates=[_candidate("AAPL")]),
        _state([_position("AAPL")]), NOW, [])
    assert not [r for r in recs if r["action_type"] == ActionType.ENTER]


# ---------------------------------------------------------------------------
# XLK July 6th snapshot — the labeled failure case, regression-locked.
#
# The repo has no cached real-market frame for 2026-07-06; this fixture is a
# labeled synthetic reconstruction of the failure shape (a sector ETF whose
# tape broke: extended run, then a collapse through the 50/200-day MAs into
# 2026-07-06). The lock is on the BEHAVIOR: the real scorecard scoring path
# (metrics.scorecard.score_ticker + compute_verdict, no fork) must produce a
# blocking verdict via worst-signal-wins, and the engine must emit NO ENTER.
# Replace the bars with the real cached frame if/when it is exported.
# ---------------------------------------------------------------------------
def _xlk_july6_frames():
    n_up, n_down = 220, 30
    up = [80 + i * 0.45 for i in range(n_up)]            # long uptrend
    down = [up[-1] - (i + 1) * 2.2 for i in range(n_down)]  # hard breakdown
    closes = up + down
    start = pd.bdate_range(end="2026-07-06", periods=len(closes))[0]
    xlk = _frame(closes, start=str(start.date()))
    spy = _frame([400 + i * 0.3 for i in range(len(closes))], start=str(start.date()))
    assert str(xlk.index[-1].date()) == "2026-07-06"
    return xlk, spy


def test_xlk_july6_snapshot_blocking_verdict_and_no_enter(monkeypatch):
    import data_handler
    import sector_data
    from metrics import scorecard as sc
    xlk, spy = _xlk_july6_frames()
    frames = {"XLK": xlk, "SPY": spy}
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: frames.get(s.upper()))
    monkeypatch.setattr(sector_data, "sector_for", lambda t: "XLK")

    # The REAL scoring path (no fork): sector_etf == ticker waives the sector
    # leg; has_weeklies pinned True so no provider probe runs offline.
    row = sc.score_ticker("XLK", spy, "XLK", xlk, has_weeklies=True)
    # The recommendation candidate keys off the CFM-suitability lens (the engine
    # layers its own Level-1 regime check on top); on this broken tape it blocks.
    verdict = row.get("suitability")
    assert verdict in ("AVOID", "CAUTION"), f"expected blocking suitability, got {row}"

    as_of = datetime(2026, 7, 6, 20, 15, tzinfo=timezone.utc)
    tk = {"price": float(xlk["Close"].iloc[-1]), "last_close": float(xlk["Close"].iloc[-1]),
          "atr": 4.0, "hist_vol": 0.35, "rs3m_vs_spy": None, "rs3m_vs_sector": None,
          "q": 0.0, "bars": xlk, "spy_bars": spy}
    candidate = {"ticker": "XLK", "verdict": verdict,
                 "level5": {"pass": True, "blocking_failures": []},
                 "juice_weekly_pct": 1.4, "blockers": []}
    recs = engine.evaluate(_market({"XLK": tk}, regime="green",
                                   candidates=[candidate]), _state([]), as_of, [])
    assert not [r for r in recs if r["action_type"] == ActionType.ENTER], \
        "XLK July 6th: the engine must NOT recommend entry on the labeled failure case"
    blockers = engine._entry_blocked(candidate, _market({"XLK": tk}, regime="green"))
    assert blockers, "worst-signal-wins must name at least one blocker"


# ---------------------------------------------------------------------------
# Shares-primary EXIT ticket. The base leg is 100 real shares, so the exit has
# to SELL them — a ticket that only buys back the short leaves the whole
# position standing and prices the exit as a small debit instead of a large
# credit.
# ---------------------------------------------------------------------------
def _shares_position(ticker="AAPL", shares=100, short_strike=180.0, short_dte=4,
                     current_bid=4.5, contracts=1):
    return {
        "ticker": ticker, "status": "active", "entry_date": "2026-06-01",
        "position_type": "SHARES",
        "shares": {"count": shares, "cost_basis_per_share": 170.0,
                   "cap": 1000, "pct_to_cap": 10},
        "short_calls": [{"strike": short_strike, "contracts": contracts,
                         "dte": short_dte, "expiration": "2026-07-17",
                         "current_bid": current_bid,
                         "entry_premium_total": 500.0,
                         "open_date": "2026-07-06"}],
        "circuit_breaker": {"price": 100.0, "source": "manual", "entry_price": 185.0},
        "dividend": None,
    }


def test_shares_exit_ticket_sells_the_share_base():
    tk = _healthy_tk(price=182.0)
    ticket = engine._exit_ticket(_shares_position(), tk, 0.0, "KILL_SWITCH_SPY")
    assert ticket["action"] == "sell_shares"
    assert ticket["order_type"] == "EQUITY"
    assert ticket["qty"] == 100
    # Cover FIRST, then release the shares — the reverse order is a naked short.
    assert [(l["instruction"], l["role"]) for l in ticket["legs"]] == [
        ("BUY_TO_CLOSE", "short"), ("SELL", "shares")]
    sale = ticket["legs"][-1]
    assert sale["quantity"] == 100 and sale["target_delta"] == 1.0


def test_shares_exit_ticket_nets_share_proceeds_against_the_buyback():
    tk = _healthy_tk(price=182.0)
    ticket = engine._exit_ticket(_shares_position(), tk, 0.0, "KILL_SWITCH_SPY")
    est = ticket["estimates"]
    assert est["shares_proceeds_per_share"] == 182.0
    assert est["shares_notional"] == pytest.approx(18200.0)
    # One contract at a 4.50 mark = $450 to cover (units.total_dollars).
    assert est["short_buyback_per_share"] == pytest.approx(4.5)
    assert ticket["short_cover"]["buyback_total"] == pytest.approx(450.0)
    assert est["net_credit"] == pytest.approx(17750.0)
    assert est["net_credit_per_share"] == pytest.approx(177.5)
    # An equity sale and an option cover are two orders; there is no single net
    # limit across them (the LEAP shape below keeps its bounds).
    assert "limit_price" not in ticket
    assert "min_acceptable_net_credit" not in ticket
    assert ticket["max_slippage_pct_of_mid"] == config.REC_MAX_SLIPPAGE_PCT_OF_MID


def test_shares_exit_ticket_unpriced_when_the_snapshot_has_no_price():
    tk = dict(_healthy_tk(), price=None, last_close=None)
    ticket = engine._exit_ticket(_shares_position(), tk, 0.0, None)
    assert ticket["price_source"] == "unpriced"
    assert ticket["estimates"]["net_credit"] is None
    assert ticket["legs"][-1]["role"] == "shares"   # the sale is still proposed


def test_legacy_leap_exit_ticket_is_unchanged():
    """The LEAP shape is byte-for-byte what it was — old positions still price as
    one NET ticket, and the append-only log keeps needing that forever."""
    tk = _healthy_tk(price=182.0)
    ticket = engine._exit_ticket(_position(), tk, 0.0, "KILL_SWITCH_SPY")
    assert ticket["action"] == "close_position"
    assert [(l["instruction"], l["role"]) for l in ticket["legs"]] == [
        ("SELL_TO_CLOSE", "leap"), ("BUY_TO_CLOSE", "short")]
    # LEAP mark 5600/contract = 56.00/share, less the 4.50 short buyback.
    assert ticket["estimates"]["net_per_share"] == pytest.approx(51.5)
    assert ticket["limit_price"] == pytest.approx(51.5)
    assert ticket["order_type"] == "NET_CREDIT"


def test_shares_position_emits_an_exit_with_the_share_sale_end_to_end():
    """Through evaluate(), not just the ticket builder: a shares position whose
    RS3M-vs-SPY has gone negative exits by selling the shares."""
    p = _shares_position()
    tk = dict(_healthy_tk(price=182.0), rs3m_vs_spy=-6.0)
    recs = engine.evaluate(_market({"AAPL": tk}), _state([p]), NOW)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["action_type"] == ActionType.EXIT
    assert rec["trigger_rule"] == TriggerRule.KILL_RS_SPY_CONFIRMED
    ticket = rec["proposed_ticket"]
    assert ticket["action"] == "sell_shares" and ticket["qty"] == 100
    assert ticket["legs"][-1] == {"instruction": "SELL", "role": "shares",
                                  "quantity": 100, "target_delta": 1.0}


def test_naked_short_on_a_shares_position_exits_via_delta_coverage():
    """2 shorts against 1 coverable lot is the shares floor breach; the exit that
    follows still proposes the share sale."""
    p = _shares_position(contracts=1)
    p["short_calls"] = p["short_calls"] * 2
    recs = engine.evaluate(_market({"AAPL": _healthy_tk(price=182.0)}), _state([p]), NOW)
    assert len(recs) == 1
    assert recs[0]["trigger_rule"] == TriggerRule.DELTA_COVERAGE_FLOOR
    assert recs[0]["proposed_ticket"]["action"] == "sell_shares"
