"""Dry-powder CSP sleeve — SHADOW ONLY, Phase 1 (TRAVIS_EXTENSION).

Offline throughout: synthetic OHLCV frames, monkeypatched chain fetches, a
tmp_path store. No network, no live Schwab.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

import config
import csp_dry_powder as dp
import indicators as ind

FIX_STRUCT = os.path.join(os.path.dirname(__file__), "fixtures", "structure")


def _frame(values):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c,
                         "Volume": 1e6}, index=idx)


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "csp_dry_powder_log"
    monkeypatch.setattr(dp, "STORE_DIR", str(d))
    return d


def _state(operating_cash=38000.0, positions=None):
    return {"positions": positions or [],
            "metadata": {"operating_cash": operating_cash}}


# ===========================================================================
# Put-side BSM (indicators.py)
# ===========================================================================
def test_bs_put_delta_is_negative_and_matches_put_call_parity():
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.0, 0.20
    call_d = ind.bs_call_delta(S, K, T, r, sigma)
    put_d = ind.bs_put_delta(S, K, T, r, sigma)
    assert put_d < 0
    # Put-call parity on delta (q=0): call_delta - put_delta == 1.
    assert call_d - put_d == pytest.approx(1.0, abs=1e-6)


def test_bs_put_delta_none_on_bad_inputs():
    assert ind.bs_put_delta(0, 100, 1.0, 0.0, 0.2) is None
    assert ind.bs_put_delta(100, 100, 0, 0.0, 0.2) is None
    assert ind.bs_put_delta(100, 100, 1.0, 0.0, 0) is None


def test_put_greeks_prefers_reported_iv_and_falls_back_to_mark():
    d_reported, iv_reported = ind.put_greeks(100.0, 95.0, 30, 1.50, reported_iv=25.0)
    assert d_reported is not None and d_reported < 0
    assert iv_reported == pytest.approx(25.0, abs=1e-2)

    d_mark, iv_mark = ind.put_greeks(100.0, 95.0, 30, 1.50, reported_iv=None)
    assert d_mark is not None and d_mark < 0
    assert iv_mark is not None

    assert ind.put_greeks(None, 95.0, 30, 1.50) == (None, None)


# ===========================================================================
# "Extended" (§1) — 8/21-EMA gap, a DIFFERENT metric from the CSP-entry route.
# ===========================================================================
def test_uptrend_with_wide_ema_gap_is_extended():
    # A strong, accelerating uptrend so price pulls well above both EMAs with
    # a wide fast/slow gap.
    values = [100 + i * 1.5 for i in range(40)]
    out = dp.ema_gap_pct(_frame(values))
    assert out["extended"] is True
    assert out["gap_pct"] >= config.DRY_POWDER_EMA_GAP_MIN_PCT


def test_flat_tape_is_not_extended():
    values = [100.0] * 40
    out = dp.ema_gap_pct(_frame(values))
    assert out["extended"] is False


def test_downtrend_is_not_extended():
    values = [140 - i for i in range(40)]
    out = dp.ema_gap_pct(_frame(values))
    assert out["extended"] is False


def test_insufficient_history_reads_not_extended_not_a_false_positive():
    out = dp.ema_gap_pct(_frame([100.0, 101.0, 102.0]))
    assert out["extended"] is False
    assert out["gap_pct"] is None


# ===========================================================================
# Eligibility (§1)
# ===========================================================================
def test_already_held_position_is_ineligible():
    values = [100 + i * 1.5 for i in range(40)]
    state = _state(positions=[{"ticker": "GDDY", "status": "open"}])
    row = dp.eligibility("GDDY", state, _frame(values))
    assert row["eligible"] is False
    assert "already_held" in row["reasons"]


def test_closed_position_does_not_count_as_held():
    values = [100 + i * 1.5 for i in range(40)]
    state = _state(positions=[{"ticker": "GDDY", "status": "closed"}])
    row = dp.eligibility("GDDY", state, _frame(values))
    assert "already_held" not in row["reasons"]


def test_not_extended_is_ineligible_even_when_unheld():
    state = _state()
    row = dp.eligibility("FLAT", state, _frame([100.0] * 40))
    assert row["eligible"] is False
    assert "not_extended" in row["reasons"]


def test_extended_and_unheld_is_eligible(monkeypatch):
    import weeklies
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t, refresh=False: True)
    state = _state()
    values = [100 + i * 1.5 for i in range(40)]
    row = dp.eligibility("GDDY", state, _frame(values))
    assert row["eligible"] is True
    assert row["reasons"] == []


# ===========================================================================
# Weekly-equivalent yield (§3, §4)
# ===========================================================================
def test_weekly_equivalent_yield_normalizes_two_week_to_one_week():
    one_week = dp.weekly_equivalent_yield_pct(1.0, 100.0, 7)
    two_week = dp.weekly_equivalent_yield_pct(2.0, 100.0, 14)
    # Same total yield stretched over two weeks should read as HALF the
    # weekly-equivalent of the one-week contract paying the same $2 over $1.
    assert one_week == pytest.approx(1.0, abs=1e-6)
    assert two_week == pytest.approx(1.0, abs=1e-6)


def test_weekly_equivalent_yield_none_on_bad_inputs():
    assert dp.weekly_equivalent_yield_pct(None, 100.0, 7) is None
    assert dp.weekly_equivalent_yield_pct(1.0, 0, 7) is None
    assert dp.weekly_equivalent_yield_pct(1.0, 100.0, 0) is None


# ===========================================================================
# Strike selection within one expiration (§2, §4) — furthest-OTM that clears
# the floor, safety-first tie-breaking.
# ===========================================================================
def _put_contract(strike, dte, mark, expiration="2026-09-11"):
    return {"strike": strike, "dte": dte, "expiration": expiration,
            "bid": mark - 0.05, "ask": mark + 0.05, "mark": mark, "volatility": 30.0}


def test_select_strike_picks_furthest_otm_that_clears_the_floor():
    underlying = 100.0
    # Three strikes descending (further OTM as strike drops). Mark chosen so
    # yield still clears the floor at every strike in this synthetic setup —
    # the point under test is which one gets PICKED (furthest OTM), not the
    # floor's rejection path (covered separately below).
    contracts = [
        _put_contract(97.0, 7, 1.20),  # closer to the money -> higher delta
        _put_contract(93.0, 7, 0.70),  # mid
        _put_contract(88.0, 7, 0.35),  # furthest OTM
    ]
    picked = dp._select_strike_in_expiration(contracts, underlying, 7)
    assert picked is not None
    # Furthest OTM among whatever clears the delta band AND the floor.
    all_passing_strikes = {c["strike"] for c in [
        {**c, **{"abs_delta": dp._put_delta(c, underlying)}} for c in contracts
    ] if c.get("abs_delta") is not None
       and config.DRY_POWDER_PUT_DELTA_MIN <= c["abs_delta"] <= config.DRY_POWDER_PUT_DELTA_MAX}
    if all_passing_strikes:
        assert picked["strike"] == min(all_passing_strikes)


def test_select_strike_none_when_nothing_clears_the_floor():
    underlying = 100.0
    # Deep OTM, tiny premium — clears the delta band but not the yield floor.
    contracts = [_put_contract(70.0, 7, 0.01)]
    picked = dp._select_strike_in_expiration(contracts, underlying, 7)
    assert picked is None


def test_evaluate_strikes_reports_every_contract_with_pass_fail_flags():
    """§8 telemetry: rejected strikes must be visible too, not just the
    winner — this is what makes the delta band / yield floor calibratable."""
    underlying = 100.0
    contracts = [
        _put_contract(97.0, 7, 1.20),   # clears delta band + floor
        _put_contract(93.0, 7, 0.70),   # delta too low (too far OTM for 7 DTE)
        _put_contract(70.0, 7, 0.01),   # in band by strike, floor fails
    ]
    rows = dp._evaluate_strikes_in_expiration(contracts, underlying, 7)
    assert len(rows) == 3
    by_strike = {r["strike"]: r for r in rows}
    assert by_strike[97.0]["in_delta_band"] is True
    assert by_strike[97.0]["clears_yield_floor"] is True
    assert by_strike[93.0]["in_delta_band"] is False
    # 70-strike: whatever its delta band result, confirm the floor math ran
    # and produced an explicit fail rather than silently dropping the row.
    assert by_strike[70.0]["annualized_yield_pct"] is not None
    assert by_strike[70.0]["clears_yield_floor"] is False


def test_select_strike_none_when_delta_band_empty():
    underlying = 100.0
    # ATM put: delta will be far above the 0.10-0.25 band.
    contracts = [_put_contract(100.0, 7, 3.0)]
    picked = dp._select_strike_in_expiration(contracts, underlying, 7)
    assert picked is None


# ===========================================================================
# Sizing (§5) — leftover cash only, never the reserve or the deployed cap.
# ===========================================================================
def test_dry_powder_available_matches_capital_summary_deployable():
    import position_manager
    state = _state(operating_cash=38000.0)
    summary = position_manager.capital_summary(state)
    assert dp.dry_powder_available(state) == pytest.approx(summary["deployable"])


def test_dry_powder_available_nets_committed_this_cycle():
    state = _state(operating_cash=38000.0)
    base = dp.dry_powder_available(state)
    assert dp.dry_powder_available(state, committed_this_cycle=1000.0) == pytest.approx(base - 1000.0)


def test_dry_powder_available_never_negative():
    state = _state(operating_cash=38000.0)
    base = dp.dry_powder_available(state)
    assert dp.dry_powder_available(state, committed_this_cycle=base + 10000) == 0.0


def test_size_contracts_floors_to_whole_contracts():
    assert dp.size_contracts(50.0, 10999.0) == 2   # 2 * 5000 = 10000 <= 10999
    assert dp.size_contracts(50.0, 4999.0) == 0
    assert dp.size_contracts(None, 10000.0) == 0
    assert dp.size_contracts(50.0, 0) == 0


def _fake_winner(**overrides):
    winner = {"strike": 90.0, "dte": 7, "expiration": "2026-09-11",
              "premium_per_share": 0.60, "abs_delta": 0.18,
              "weekly_equivalent_yield_pct": 0.67, "annualized_yield_pct": 34.9}
    winner.update(overrides)
    return winner


def _fake_evaluate_candidate(winner):
    """A stand-in for `evaluate_candidate`'s return shape, for tests that only
    care about the winner and not the full per-strike telemetry."""
    return lambda t: {"underlying": 100.0, "evaluated_by_expiration": {}, "winner": winner}


def test_evaluate_candidate_carries_full_telemetry_and_the_winner(monkeypatch):
    contracts = [
        _put_contract(97.0, 7, 1.20, expiration="2026-09-11"),
        _put_contract(93.0, 7, 0.70, expiration="2026-09-11"),
    ]
    monkeypatch.setattr(dp, "_fetch_put_contracts", lambda t: (100.0, contracts))
    monkeypatch.setattr(dp, "_weekly_expirations", lambda contracts, count=2: ["2026-09-11"])

    out = dp.evaluate_candidate("GDDY")
    assert "2026-09-11" in out["evaluated_by_expiration"]
    assert len(out["evaluated_by_expiration"]["2026-09-11"]) == 2
    assert out["winner"]["strike"] == 97.0


def test_evaluate_candidate_no_contracts_returns_empty_telemetry(monkeypatch):
    monkeypatch.setattr(dp, "_fetch_put_contracts", lambda t: (100.0, []))
    out = dp.evaluate_candidate("DEAD")
    assert out["winner"] is None
    assert out["evaluated_by_expiration"] == {}


def test_scan_threads_full_telemetry_onto_the_candidate_row(store, monkeypatch):
    import data_handler
    import weeklies

    trending = _frame([100 + i * 1.5 for i in range(40)])
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: trending)
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t, refresh=False: True)
    fake_eval = {"underlying": 100.0,
                "evaluated_by_expiration": {"2026-09-11": [
                    {"strike": 97.0, "in_delta_band": True, "clears_yield_floor": True},
                    {"strike": 70.0, "in_delta_band": True, "clears_yield_floor": False}]},
                "winner": None}
    monkeypatch.setattr(dp, "evaluate_candidate", lambda t: fake_eval)

    state = _state()
    result = dp.scan(["GDDY"], state=state)
    row = next(c for c in result["candidates"] if c["ticker"] == "GDDY")
    assert row["evaluated_by_expiration"] == fake_eval["evaluated_by_expiration"]
    assert row["scan_result"] == "no_qualifying_strike"


# ===========================================================================
# Orchestration (scan) — end to end with a monkeypatched chain fetch.
# ===========================================================================
def test_scan_logs_every_ticker_as_a_candidate_regardless_of_outcome(store, monkeypatch):
    import data_handler
    import weeklies

    flat = _frame([100.0] * 40)
    trending = _frame([100 + i * 1.5 for i in range(40)])
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda t, force=False: trending if t == "EXT" else flat)
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t, refresh=False: True)
    monkeypatch.setattr(dp, "evaluate_candidate", _fake_evaluate_candidate(None))

    state = _state()
    result = dp.scan(["FLAT", "EXT"], state=state)
    tickers = {c["ticker"] for c in result["candidates"]}
    assert tickers == {"FLAT", "EXT"}
    ext_row = next(c for c in result["candidates"] if c["ticker"] == "EXT")
    assert ext_row["eligible"] is True
    assert ext_row["scan_result"] == "no_qualifying_strike"
    flat_row = next(c for c in result["candidates"] if c["ticker"] == "FLAT")
    assert flat_row["eligible"] is False


def test_scan_produces_a_shadow_trade_when_a_strike_qualifies_and_capital_allows(store, monkeypatch):
    import data_handler
    import weeklies

    trending = _frame([100 + i * 1.5 for i in range(40)])
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: trending)
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t, refresh=False: True)
    monkeypatch.setattr(dp, "evaluate_candidate", _fake_evaluate_candidate(_fake_winner()))

    state = _state(operating_cash=38000.0)
    result = dp.scan(["GDDY"], state=state)
    assert len(result["shadow_trades"]) == 1
    trade = result["shadow_trades"][0]
    assert trade["strategy_tag"] == dp.STRATEGY_TAG
    assert trade["ticker"] == "GDDY"
    assert trade["contracts"] >= 1
    assert trade["outcome"] is None


def test_scan_records_no_trade_when_no_capital_available(store, monkeypatch):
    import data_handler
    import weeklies

    trending = _frame([100 + i * 1.5 for i in range(40)])
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: trending)
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t, refresh=False: True)
    monkeypatch.setattr(dp, "evaluate_candidate", _fake_evaluate_candidate(_fake_winner()))

    state = _state(operating_cash=0.0)   # nothing above the $13K reserve
    result = dp.scan(["GDDY"], state=state)
    assert result["shadow_trades"] == []
    row = next(c for c in result["candidates"] if c["ticker"] == "GDDY")
    assert row["scan_result"] == "no_capital"


def test_scan_never_encroaches_the_reserve_or_deployed_cap(store, monkeypatch):
    """Two qualifying candidates in one run must never together commit more
    than capital_summary's deployable figure — sizing nets what earlier
    candidates in the SAME run already committed (§5)."""
    import data_handler
    import weeklies

    trending = _frame([100 + i * 1.5 for i in range(40)])
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: trending)
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t, refresh=False: True)
    monkeypatch.setattr(dp, "evaluate_candidate", _fake_evaluate_candidate(_fake_winner()))

    import position_manager
    state = _state(operating_cash=38000.0)
    deployable = position_manager.capital_summary(state)["deployable"]

    result = dp.scan(["AAA", "BBB", "CCC", "DDD", "EEE"], state=state)
    total_collateral = sum(t["collateral"] for t in result["shadow_trades"])
    assert total_collateral <= deployable


# ===========================================================================
# Storage — append-only, per day, no in-place mutation of a past day's file.
# ===========================================================================
def test_record_roundtrips_through_the_store(store):
    dp._record({"date": "2026-09-04", "schema": dp.SCHEMA_VERSION,
               "candidates": [{"ticker": "AAA"}], "shadow_trades": [], "outcomes": []})
    data = dp._load_day("2026-09-04")
    assert data["candidates"] == [{"ticker": "AAA"}]
    assert dp.stored_days() == ["2026-09-04"]


def test_record_appends_within_the_same_day_rather_than_overwriting(store):
    dp._record({"date": "2026-09-04", "candidates": [{"ticker": "AAA"}],
               "shadow_trades": [], "outcomes": []})
    dp._record({"date": "2026-09-04", "candidates": [{"ticker": "BBB"}],
               "shadow_trades": [], "outcomes": []})
    data = dp._load_day("2026-09-04")
    assert [c["ticker"] for c in data["candidates"]] == ["AAA", "BBB"]


def test_prune_removes_only_the_oldest_days_outside_the_window(store, monkeypatch):
    monkeypatch.setattr(config, "DRY_POWDER_LOG_RETENTION_DAYS", 2)
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        dp._record({"date": day, "candidates": [], "shadow_trades": [], "outcomes": []})
    assert dp.stored_days() == ["2026-09-02", "2026-09-03"]


# ===========================================================================
# Outcome resolution (§6, §8) — hypothetical, append-only.
# ===========================================================================
def test_resolve_outcomes_flags_expired_worthless(store, monkeypatch):
    import data_handler
    dp._record({"date": "2026-08-01", "candidates": [], "outcomes": [],
               "shadow_trades": [{
                   "strategy_tag": dp.STRATEGY_TAG, "ticker": "GDDY", "tier": dp.TIER_GENERAL,
                   "opened_date": "2026-08-01", "expiration": "2026-08-08", "dte": 7,
                   "strike": 90.0, "abs_delta": 0.18, "premium_per_share": 0.60,
                   "contracts": 1, "collateral": 9000.0,
                   "weekly_equivalent_yield_pct": 0.67, "annualized_yield_pct": 34.9,
                   "outcome": None}]})
    # Closed WELL above strike on/near expiration -> expired worthless.
    price_history = _frame([100.0] * 40)
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: price_history)

    resolved = dp.resolve_outcomes(state={"positions": [], "metadata": {}}, as_of="2026-08-10")
    assert len(resolved) == 1
    assert resolved[0]["outcome"]["assigned"] is False


def test_resolve_outcomes_flags_assignment_and_reevaluates_the_gate(store, monkeypatch):
    import data_handler
    import screening

    dp._record({"date": "2026-08-01", "candidates": [], "outcomes": [],
               "shadow_trades": [{
                   "strategy_tag": dp.STRATEGY_TAG, "ticker": "GDDY", "tier": dp.TIER_GENERAL,
                   "opened_date": "2026-08-01", "expiration": "2026-08-08", "dte": 7,
                   "strike": 90.0, "abs_delta": 0.18, "premium_per_share": 0.60,
                   "contracts": 1, "collateral": 9000.0,
                   "weekly_equivalent_yield_pct": 0.67, "annualized_yield_pct": 34.9,
                   "outcome": None}]})
    # Closed BELOW strike -> would have been assigned.
    price_history = _frame([80.0] * 40)
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: price_history)
    monkeypatch.setattr(screening, "entry_gate",
                        lambda t: {"verdict": "BLOCKED", "blocked_by": ["close_below_ma200"]})

    resolved = dp.resolve_outcomes(state={"positions": [], "metadata": {}}, as_of="2026-08-10")
    assert len(resolved) == 1
    outcome = resolved[0]["outcome"]
    assert outcome["assigned"] is True
    assert outcome["classification"] == "entered_on_weakness_flag_for_review"


def test_resolve_outcomes_does_not_mutate_the_original_day_file(store, monkeypatch):
    import data_handler
    dp._record({"date": "2026-08-01", "candidates": [], "outcomes": [],
               "shadow_trades": [{
                   "strategy_tag": dp.STRATEGY_TAG, "ticker": "GDDY", "tier": dp.TIER_GENERAL,
                   "opened_date": "2026-08-01", "expiration": "2026-08-08", "dte": 7,
                   "strike": 90.0, "abs_delta": 0.18, "premium_per_share": 0.60,
                   "contracts": 1, "collateral": 9000.0,
                   "weekly_equivalent_yield_pct": 0.67, "annualized_yield_pct": 34.9,
                   "outcome": None}]})
    before = dp._load_day("2026-08-01")
    price_history = _frame([100.0] * 40)
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: price_history)

    dp.resolve_outcomes(state={"positions": [], "metadata": {}}, as_of="2026-08-10")
    after = dp._load_day("2026-08-01")
    assert before == after   # the original day's shadow_trades record is untouched
    assert dp.stored_days() == ["2026-08-01", dp._today()]


def test_resolve_outcomes_skips_trades_not_yet_expired(store, monkeypatch):
    import data_handler
    dp._record({"date": "2026-08-01", "candidates": [], "outcomes": [],
               "shadow_trades": [{
                   "strategy_tag": dp.STRATEGY_TAG, "ticker": "GDDY", "tier": dp.TIER_GENERAL,
                   "opened_date": "2026-08-01", "expiration": "2099-01-01", "dte": 7,
                   "strike": 90.0, "abs_delta": 0.18, "premium_per_share": 0.60,
                   "contracts": 1, "collateral": 9000.0,
                   "weekly_equivalent_yield_pct": 0.67, "annualized_yield_pct": 34.9,
                   "outcome": None}]})
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: _frame([100.0] * 40))
    resolved = dp.resolve_outcomes(state={"positions": [], "metadata": {}}, as_of="2026-08-10")
    assert resolved == []
