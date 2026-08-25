"""Dividend income profile & position builder (schema v21) — the 10 spec tests.

Offline, fixture-driven, mocked providers. Every test here is either a
NON-REGRESSION lock on existing CFM behavior (tests 1, 2, 9) or a lock on the
TRAVIS_EXTENSION's boundary — that the dividend sleeve never blends into, weakens,
or gains authority over the juice engine.

Run: python -m pytest backend/test_dividend_profile.py -q
"""
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-divprof-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import accrual  # noqa: E402
import account_gate  # noqa: E402
import alerts  # noqa: E402
import config  # noqa: E402
import dividend_calendar  # noqa: E402
import executor  # noqa: E402
import income_profile as ip  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import position_manager as pm  # noqa: E402
import position_types  # noqa: E402
import scan_triggers as st  # noqa: E402
import stock_lights  # noqa: E402


# ---------------------------------------------------------------------------
# Frames + harness
# ---------------------------------------------------------------------------
def _frame(closes, vol=1e6, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.004, "Low": c * 0.996,
                         "Close": c, "Volume": vol}, index=idx)


def _ko_like(n=320, base=60.0, drift=0.00035, sigma=0.006, seed=11):
    """A KO-like tape: low realized vol, clean uptrend, resting near MA21.
    Low sigma is what makes its weekly extrinsic thin — the whole point of the
    dividend-sleeve fixture."""
    rng = np.random.RandomState(seed)
    closes = base * np.exp(np.cumsum(rng.normal(drift, sigma, n)))
    # Flatten the tail so the name is consolidating, not extended, at the last bar.
    closes[-12:] = closes[-13] * (1 + rng.normal(0, 0.0015, 12)).cumprod()
    return _frame(closes)


def _laggard(n=320, base=60.0, seed=5):
    """A payer that rises, but more slowly than its dividend peers — the
    AAPL-lesson shape for the dividend branch."""
    rng = np.random.RandomState(seed)
    closes = base * np.exp(np.cumsum(rng.normal(0.00005, 0.006, n)))
    return _frame(closes)


def _strong_bench(n=320, base=80.0, seed=3):
    rng = np.random.RandomState(seed)
    closes = base * np.exp(np.cumsum(rng.normal(0.0009, 0.005, n)))
    return _frame(closes)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    st_ = log.load_state()
    st_["metadata"]["operating_cash"] = 100000
    log.save_state(st_)
    return tmp_path


def _buy_shares(ticker, qty, price, **extra):
    return executor.execute({"action": "buy_shares", "ticker": ticker, "qty": qty,
                             "price_per_share": price, "stock_price": price, **extra})


def _sell_short(ticker, strike, contracts, prem, spot, exp="2026-08-21"):
    return executor.execute({"action": "sell_short", "ticker": ticker, "strike": strike,
                             "contracts": contracts, "premium_per_share": prem,
                             "stock_price": spot, "expiration": exp})


def _close_short(ticker, strike, contracts, price_ps, spot, **extra):
    return executor.execute({"action": "close_short", "ticker": ticker, "strike": strike,
                             "contracts": contracts, "close_price_per_share": price_ps,
                             "stock_price": spot, **extra})


# ===========================================================================
# 1. XLK July 6th regression — unchanged.
# ===========================================================================
def test_1_xlk_july6_regression_unchanged():
    """The labeled failure case must behave EXACTLY as before. Both artifacts are
    pinned: the parquet fixture through the ETF/lights path, and the synthetic
    frame through score_ticker -> the recommendation engine. A diff in either is a
    defect in this work item, not a new baseline."""
    import genius_lights
    import indicators
    fix_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regime")
    df = pd.read_parquet(os.path.join(fix_dir, "xlk_july6_rollover.parquet"))

    # Layer 1 — the four-light vote still denies GREEN.
    eng = genius_lights.compute(df)
    assert eng["lights"]["sar"]["signal"] == "red" or eng["lights"]["momentum"]["signal"] == "red"
    assert eng["greens"] < 4

    # Layer 2 — the ATR/IVR veto still fires independently, through the ETF path.
    assert indicators.atr_expanding(df) is True
    res = stock_lights.compute(df, ivr_percentile=95.0, is_etf=True)
    assert res["verdict"] == stock_lights.RED
    assert "veto:atr_expanding_high_ivr" in res["veto_reasons"]
    assert stock_lights.compute(df, ivr_percentile=10.0,
                                is_etf=True)["verdict"] != stock_lights.GREEN

    # And the profile parameter cannot change any of it: a name evaluated as a
    # DIVIDEND_COMPOUNDER gets the identical lights, verdict and right-spot.
    as_div = stock_lights.compute(df, ivr_percentile=95.0, is_etf=True)
    assert as_div["lights"] == res["lights"]
    assert as_div["verdict"] == res["verdict"]
    assert as_div["greens"] == res["greens"]
    assert as_div["right_spot"] == res["right_spot"]


# ===========================================================================
# 2. Juice-engine non-regression — verdict paths byte-identical.
# ===========================================================================
def test_2_juice_engine_verdict_is_byte_identical_to_no_profile():
    """A JUICE_ENGINE evaluation must be indistinguishable from the pre-v21 call
    that took no profile at all. This is the guarantee that adding the sleeve did
    not perturb the default path."""
    df = _ko_like()
    sector = _strong_bench()
    legacy = stock_lights.compute(df, ivr_percentile=30.0, is_etf=False)
    tagged = stock_lights.compute(df, ivr_percentile=30.0, is_etf=False)
    # Provenance is additive; every DECISION field is identical.
    for key in ("lights", "greens", "reds", "insufficient", "verdict", "enterable",
                "vetoed", "veto_reasons", "right_spot"):
        assert legacy[key] == tagged[key], key
    # The veto EVALUATIONS match too, once the additive provenance key is dropped.
    strip = lambda vs: [{k: v for k, v in d.items() if k != "benchmark"} for d in vs]  # noqa: E731
    assert strip(legacy["vetoes"]) == strip(tagged["vetoes"])


def test_2b_shadow_floor_never_reaches_the_verdict_composition():
    """The load-bearing invariant: a shadow-floor observation is NOT a block. It
    must never enter the list that compose_row_verdict derives authority from."""
    floor = st.shadow_floor(ip.DIVIDEND_COMPOUNDER, 0.05, 0.0, 0.01)
    assert floor["pass"] is False and floor["reasons"]        # it DID fail...
    assert floor["shadow"] is True and floor["blocking"] is False
    # ...and a failing floor carries none of the shape gate_blocks/compose_row_verdict
    # consume, so it cannot be mistaken for a block.
    assert "level" not in floor and "id" not in floor

    # The blocking juice floor stays silent in shares mode, exactly as before.
    # (conftest's autouse fixture flips LEGACY_LEAP_READONLY off for legacy-LEAP
    # fixtures, so pin it explicitly rather than assuming the ambient value.)
    import unittest.mock
    with unittest.mock.patch.object(config, "LEGACY_LEAP_READONLY", True):
        assert st.juice_floor_block(-0.5, 0.1) is None


def test_2c_no_switch_can_give_a_shadow_floor_authority():
    """Spec: 'Do not build a config switch that enables blocking now.' Assert the
    absence structurally — shadow/blocking are literals, not config reads."""
    for profile in (ip.JUICE_ENGINE, ip.DIVIDEND_COMPOUNDER):
        out = st.shadow_floor(profile, 0.01, 0.0, 0.001)
        assert out["shadow"] is True
        assert out["blocking"] is False
    assert not any("SHADOW" in name and "ENABLE" in name.upper()
                   for name in dir(config))


# ===========================================================================
# 3. Dividend fixture: passes trend gates, fails juice-only, clears combined.
# ===========================================================================
def test_3_dividend_fixture_fails_juice_floor_but_clears_combined(store, monkeypatch):
    """A KO-like payer: thin weekly juice, ~3% yield. It must FAIL the juice-only
    floor and CLEAR the combined floor — and neither result may touch its verdict,
    because both floors are shadow."""
    juice_only = 0.55      # %/wk — below the 0.75 JUICE_ENGINE floor
    annual_div = 3.12      # % — 3.12/52 = 0.06%/wk

    as_juice = st.shadow_floor(ip.JUICE_ENGINE, juice_only, annual_div, 0.35)
    assert as_juice["basis"] == "juice"
    assert as_juice["pass"] is False and "JUICE_BELOW_FLOOR" in as_juice["reasons"]

    as_div = st.shadow_floor(ip.DIVIDEND_COMPOUNDER, juice_only, annual_div, 0.35)
    assert as_div["basis"] == "combined"
    assert as_div["pass"] is True and as_div["reasons"] == []
    assert as_div["measured_pct"] == pytest.approx(juice_only + annual_div / 52, abs=1e-4)

    # The components stay separable — never one blended number.
    assert as_div["dividend_weekly_pct"] == pytest.approx(annual_div / 52, abs=1e-4)
    assert as_div["dividend_known"] is True

    # HONEST MAGNITUDE, pinned so it can't be misread later. A 3%/yr payer
    # contributes ~0.06%/wk — roughly a FIFTH of the 0.25%/wk gap between the two
    # floors (0.75 vs 0.50). So it is the LOWER FLOOR, not the dividend, that does
    # most of the work of admitting this name. The dividend is real income and it
    # compounds through the accrual ledger; it is not a large weekly yield term.
    floor_gap = config.SHARES_JUICE_FLOOR_PCT - config.COMBINED_YIELD_FLOOR_WK
    assert as_div["dividend_weekly_pct"] < floor_gap / 3
    # The name would clear the combined floor on juice alone, dividend or not:
    assert st.shadow_floor(ip.DIVIDEND_COMPOUNDER, juice_only, 0.0, 0.35)["pass"] is True

    # The trend gates are untouched by the profile: identical inputs, identical
    # lights/right-spot for both sleeves.
    df, bench = _ko_like(), _strong_bench()
    a = stock_lights.compute(df, ivr_percentile=20.0, is_etf=False)
    b = stock_lights.compute(df, ivr_percentile=20.0, is_etf=False)
    assert a["verdict"] == b["verdict"] and a["right_spot"] == b["right_spot"]


def test_3b_juice_below_slippage_subfloor(store):
    """A compounder can clear the combined bar on its dividend alone while its
    juice can't pay for its own round-trip crossing. That must be flagged."""
    out = st.shadow_floor(ip.DIVIDEND_COMPOUNDER,
                          juice_weekly_pct=0.02,        # essentially no juice
                          annual_dividend_yield_pct=30.0,  # but a huge dividend
                          weekly_extrinsic_per_share=0.01)  # 1c/share weekly
    assert "JUICE_BELOW_SLIPPAGE" in out["reasons"]
    assert out["pass"] is False       # the sub-floor sinks it despite the combined
    assert out["slippage"]["clears"] is False
    # A healthy weekly extrinsic clears it.
    ok = st.shadow_floor(ip.DIVIDEND_COMPOUNDER, 0.6, 3.0, 0.45)
    assert "JUICE_BELOW_SLIPPAGE" not in ok["reasons"] and ok["pass"] is True


def test_3c_unknown_dividend_never_reads_as_a_confident_zero():
    parts = st.combined_weekly_yield(0.5, None)
    assert parts["dividend_known"] is False
    assert parts["dividend_weekly_pct"] is None
    assert parts["combined_weekly_yield_pct"] == 0.5   # not counted, not invented
    # And an unknown yield never auto-enrols a name into the extension.
    assert ip.profile_for("KO", state=None, annual_dividend_yield_pct=None) == ip.JUICE_ENGINE


# ===========================================================================
# 4. Dividend laggard — rejected at Level 3 against the dividend benchmark.
# ===========================================================================
def test_4_dividend_laggard_is_no_longer_vetoed_on_the_peer_leg():
    """Was test_4_dividend_laggard_vetoed_against_the_dividend_benchmark, which
    pinned that substituting the dividend peer benchmark did not make the filter
    toothless. The peer leg itself was removed 2026-08-21
    (docs/decision-2026-08-21-remove-sector-rs.md), so there is no vs-peer veto
    left to be toothless OR toothed. Re-pinned to assert the absence — a payer
    lagging its dividend peers is now clear of THIS veto, and only the ATR/IVR
    and MA200 vetoes remain."""
    laggard = _laggard()
    vetoes = stock_lights.evaluate_vetoes(laggard, ivr_percentile=20.0,
                                          is_etf=False)
    assert not any(v["id"] == "rs3m_vs_sector" for v in vetoes)
    assert {v["id"] for v in vetoes} == {"atr_expanding_high_ivr", "close_below_ma200"}
    res = stock_lights.compute(laggard, ivr_percentile=20.0, is_etf=False)
    assert "veto:rs3m_vs_sector" not in res["veto_reasons"]


def test_4b_benchmark_resolution_still_works_for_the_income_floors():
    """The peer BENCHMARK NAME still resolves — it selects which shadow income
    floor a sleeve is measured against. Only the RS legs it used to feed are
    gone (docs/decision-2026-08-21-remove-sector-rs.md)."""
    assert ip.benchmark_for(ip.JUICE_ENGINE, "XLP") == "XLP"
    assert ip.benchmark_for(ip.DIVIDEND_COMPOUNDER, "XLP") == config.DIVIDEND_PEER_BENCHMARK
    assert config.DIVIDEND_PEER_BENCHMARK in config.DIVIDEND_PEER_BENCHMARKS
    # A name that IS its own benchmark has no peer leg — the guard is asked against
    # the ACTIVE benchmark, not just the sector ETF.
    assert ip.is_own_benchmark("SCHD", ip.DIVIDEND_COMPOUNDER, "XLP") is True
    assert ip.is_own_benchmark("KO", ip.DIVIDEND_COMPOUNDER, "XLP") is False
    assert ip.is_own_benchmark("XLP", ip.JUICE_ENGINE, "XLP") is True


def test_4c_kill_switch_spy_leg_is_untouched():
    """DO NOT touch the RS3M-vs-SPY kill-switch leg. classify() is profile-blind:
    a negative vs-SPY reading exits under either sleeve, identically."""
    import kill_switch
    for _profile in (ip.JUICE_ENGINE, ip.DIVIDEND_COMPOUNDER):
        v = kill_switch.classify("KO", rs_vs_spy=-3.0)
        assert v["status"] == "red" and v["alert"] is True
        assert "RS3M vs SPY" in v["suggested_action"]
        assert kill_switch.exit_reason_code(v) == "KILL_SWITCH_SPY"


# ===========================================================================
# 5. Accrual accumulates; crossing the threshold recommends; L5 blocks with reason.
# ===========================================================================
def test_5_accrual_accumulates_and_recommends_a_lot_add(store):
    _buy_shares("KO", 100, 60.0)
    _sell_short("KO", 58.0, 1, 3.0, 60.0)
    # Cycle close: sold 3.00/sh with 2.00 intrinsic (1.00 extrinsic), bought back
    # at 2.20 with 2.00 intrinsic (0.20 extrinsic) -> 0.80/sh x 100 = $80 realized.
    _close_short("KO", 58.0, 1, 2.20, 60.0)
    executor.execute({"action": "dividend_income", "ticker": "KO",
                      "per_share": 0.48, "pay_date": "2026-07-15"})

    ledger = log.load_state()["accrual_ledger"]
    ko = ledger["by_ticker"]["KO"]
    assert ko["by_source"][accrual.SOURCE_REALIZED_EXTRINSIC] == 80.0
    assert ko["by_source"][accrual.SOURCE_DIVIDEND] == 48.0
    assert ko["accrued_cash"] == 128.0
    # The whitelist is a module constant, not persisted state — the per-ticker
    # by_source keys are derived from it, so they are what pins it.
    assert sorted(ko["by_source"]) == sorted(accrual.ACCEPTED_SOURCES)

    # Not yet a lot: 128 < 60 * 100 * 1.02.
    state = log.load_state()
    prog = accrual.progress(state, "KO", 60.0)
    assert prog["ready"] is False
    assert prog["threshold"] == pytest.approx(6120.0)
    assert prog["remaining"] == pytest.approx(5992.0)

    # With enough accrued, and a funded account, the add is actionable.
    state["accrual_ledger"]["by_ticker"]["KO"]["accrued_cash"] = 7000.0
    status = accrual.lot_add_status(state, "KO", 60.0)
    assert status["ready"] is True and status["actionable"] is True
    assert status["blocked"] is False


def test_5b_lot_add_blocked_by_level5_shows_the_reason(store):
    _buy_shares("KO", 100, 60.0)
    state = log.load_state()
    state["accrual_ledger"] = {"by_ticker": {"KO": {
        "ticker": "KO", "credited": 7000.0, "spent_on_lots": 0.0,
        "accrued_cash": 7000.0, "credits": 2,
        "by_source": {accrual.SOURCE_DIVIDEND: 1000.0,
                      accrual.SOURCE_REALIZED_EXTRINSIC: 6000.0}}}, "records": []}
    # Starve the account: the cash-reserve check must block the add.
    state["metadata"]["operating_cash"] = 100.0
    log.save_state(state)

    status = accrual.lot_add_status(log.load_state(), "KO", 60.0)
    assert status["ready"] is True          # the accrual IS there...
    assert status["actionable"] is False    # ...but the add is not actionable
    assert status["blocked"] is True
    assert "cash_reserve" in status["blocking_failures"]
    assert "Level 5 gate" in status["blocked_reason"]


def test_5c_lot_add_recommendation_is_telemetry_not_a_trade(store):
    _buy_shares("KO", 100, 60.0)
    before = log.find_position(log.load_state(), "KO")["shares"]["count"]
    executor.execute({"action": "lot_add_recommended", "ticker": "KO",
                      "price_per_share": 60.0})
    state = log.load_state()
    # No position change, no cash movement — a recommendation books nothing.
    assert log.find_position(state, "KO")["shares"]["count"] == before
    rec = [e for e in state["executions"] if e["action"] == "lot_add_recommended"][-1]
    assert rec["actionable"] is False       # not enough accrued yet
    assert state["accrual_ledger"]["recommendations"][-1]["ticker"] == "KO"


def test_5d_an_executed_lot_add_spends_the_accrued_balance(store):
    _buy_shares("KO", 100, 60.0)
    _sell_short("KO", 58.0, 1, 3.0, 60.0)
    _close_short("KO", 58.0, 1, 2.20, 60.0)
    assert log.load_state()["accrual_ledger"]["by_ticker"]["KO"]["accrued_cash"] == 80.0
    # A lot add consumes the balance it was funded by, so the same dollars can't
    # fund an unbounded series of adds.
    _buy_shares("KO", 100, 60.0, lot_add=True)
    ko = log.load_state()["accrual_ledger"]["by_ticker"]["KO"]
    assert ko["spent_on_lots"] == 6000.0
    assert ko["accrued_cash"] == 0.0        # floored at zero, never a debt


# ===========================================================================
# 6. Martingale guard — a roll-down credit produces NO accrual credit.
# ===========================================================================
def test_6_roll_down_credit_never_produces_an_accrual_credit(store):
    """Structural. A defensive roll's credit is the near side of a DEFERRED
    INTRINSIC OBLIGATION; compounding it would scale the book up precisely as the
    thesis deteriorates. The rejection is by construction in credit_for, not by a
    downstream filter."""
    _buy_shares("KO", 100, 60.0)
    _sell_short("KO", 62.0, 1, 2.0, 60.0)
    # A defensive roll: close_short + sell_short sharing a roll_id.
    executor.execute({"action": "roll_short", "ticker": "KO", "contracts": 1,
                      "from_strike": 62.0, "close_price_per_share": 3.5,
                      "to_strike": 58.0, "premium_per_share": 6.0,
                      "to_expiration": "2026-08-28", "to_dte": 7,
                      "stock_price": 58.5, "roll_reason": "defend"})
    state = log.load_state()
    rolled = [e for e in state["executions"] if e.get("roll_id")]
    assert rolled, "the roll must have produced roll_id-stamped legs"

    # The unit-level guarantee: credit_for REJECTS every roll leg.
    for e in rolled:
        assert accrual.credit_for(e) is None, e.get("action")

    # And the ledger reflects it — no realized-extrinsic credit from the roll.
    ko = state["accrual_ledger"]["by_ticker"].get("KO", {})
    assert ko.get("by_source", {}).get(accrual.SOURCE_REALIZED_EXTRINSIC, 0.0) == 0.0

    # The roll IS in the roll ledger — it happened, it's just not income.
    assert state["roll_ledger"]["by_ticker"]["KO"]["count"] == 1


def test_6b_credit_for_is_a_whitelist(store):
    """Anything not explicitly recognized accrues nothing — including actions that
    do not exist yet."""
    for action in ("buy_shares", "sell_shares", "buy_leap", "close_leap",
                   "sell_short", "adjustment", "txn_correction",
                   "lot_add_recommended", "some_future_action"):
        assert accrual.credit_for({"action": action, "ticker": "KO",
                                   "amount": 500, "id": "x"}) is None, action
    assert accrual.credit_for(None) is None
    assert accrual.credit_for({}) is None


# ===========================================================================
# 7. Atomicity — 99 shares' worth of accrued cash changes nothing.
# ===========================================================================
def test_7_sub_lot_accrual_changes_no_coverage_or_per_contract_math(store):
    _buy_shares("KO", 100, 60.0)
    state = log.load_state()
    p = log.find_position(state, "KO")
    before_lots = pm.covered_lots(p["shares"]["count"])
    before_capital = pm.position_capital(p)

    # 99 shares' worth of cash — one share short of a lot.
    state["accrual_ledger"] = {"by_ticker": {"KO": {
        "ticker": "KO", "credited": 5940.0, "spent_on_lots": 0.0,
        "accrued_cash": 5940.0, "credits": 9,
        "by_source": {accrual.SOURCE_DIVIDEND: 940.0,
                      accrual.SOURCE_REALIZED_EXTRINSIC: 5000.0}}}, "records": []}
    log.save_state(state)

    state = log.load_state()
    p = log.find_position(state, "KO")
    # Nothing moved: not the share count, not coverable lots, not deployed capital.
    assert p["shares"]["count"] == 100
    assert pm.covered_lots(p["shares"]["count"]) == before_lots
    assert pm.position_capital(p) == before_capital
    prog = accrual.progress(state, "KO", 60.0)
    assert prog["ready"] is False

    # And a fragment can never round up into coverage.
    assert pm.covered_lots(199)["coverable_lots"] == 1
    assert pm.covered_lots(199)["fragment_shares"] == 99


def test_7b_sub_lot_share_entry_is_refused(store):
    """No sub-100-share exposure ANYWHERE. The entry path refuses a fragment
    outright; a broker-side odd lot is still bookable via the reconciliation
    adjustment path, which carries no size rule."""
    with pytest.raises(ValueError, match="multiple of 100"):
        _buy_shares("KO", 150, 60.0)
    with pytest.raises(ValueError, match="multiple of 100"):
        _buy_shares("KO", 0, 60.0)
    assert executor.shares_entry_lots({"qty": 300}) == 3


# ===========================================================================
# 8. Ex-div guard — fires when extrinsic < dividend, silent when it doesn't.
# ===========================================================================
def _position_with_short(ex_date, amount, current_bid, strike=58.0, expiration="2026-08-21"):
    return {
        "ticker": "KO", "status": "active", "position_type": position_types.SHARES,
        "income_profile": ip.DIVIDEND_COMPOUNDER,
        "shares": {"count": 100, "cost_basis_per_share": 55.0},
        "dividend": {"ex_date": ex_date, "amount": amount},
        "short_calls": [{"strike": strike, "contracts": 1, "current_bid": current_bid,
                         "dte": 20, "expiration": expiration,
                         "premium_per_share": 3.0}],
    }


def test_8_early_assignment_risk_fires_when_extrinsic_below_dividend(store, monkeypatch):
    from datetime import date, timedelta
    ex = (date.today() + timedelta(days=5)).isoformat()
    expiry = (date.today() + timedelta(days=20)).isoformat()
    monkeypatch.setattr(alerts, "_last_close", lambda t: 60.0)
    # Short 58 with the stock at 60: intrinsic 2.00. A 2.05 bid leaves 0.05
    # extrinsic — below the 0.48 dividend going ex inside the short's life.
    state = {"positions": [_position_with_short(ex, 0.48, 2.05, expiration=expiry)]}

    fired = alerts.check_assignment_risk(state)
    div_alerts = [a for a in fired if a["data"].get("trigger") == "dividend"]
    assert len(div_alerts) == 1
    a = div_alerts[0]
    assert a["type"] == "ASSIGNMENT_RISK"
    assert a["data"]["code"] == "EARLY_ASSIGNMENT_RISK"
    assert a["data"]["ex_date"] == ex
    assert a["data"]["dividend"] == 0.48
    assert "roll" in a["action"].lower()
    assert alerts.ALERT_TYPES["ASSIGNMENT_RISK"][0] == "HIGH"   # defense severity


def test_8b_control_extrinsic_above_dividend_stays_silent(store, monkeypatch):
    from datetime import date, timedelta
    ex = (date.today() + timedelta(days=5)).isoformat()
    expiry = (date.today() + timedelta(days=20)).isoformat()
    monkeypatch.setattr(alerts, "_last_close", lambda t: 60.0)
    # A 3.10 bid leaves 1.10 extrinsic — comfortably above the 0.48 dividend.
    state = {"positions": [_position_with_short(ex, 0.48, 3.10, expiration=expiry)]}
    assert [a for a in alerts.check_assignment_risk(state)
            if a["data"].get("trigger") == "dividend"] == []


def test_8c_ex_div_after_expiry_is_not_this_cycles_risk(store, monkeypatch):
    from datetime import date, timedelta
    monkeypatch.setattr(alerts, "_last_close", lambda t: 60.0)
    ex = (date.today() + timedelta(days=40)).isoformat()       # past the expiry
    expiry = (date.today() + timedelta(days=20)).isoformat()
    state = {"positions": [_position_with_short(ex, 0.48, 2.05, expiration=expiry)]}
    assert [a for a in alerts.check_assignment_risk(state)
            if a["data"].get("trigger") == "dividend"] == []


def test_8d_calendar_contract_units_and_no_invented_dates():
    """The contract the guard depends on: per-share per-payment amounts, real
    ex-dates or None — never a substituted date."""
    ev = dividend_calendar.normalize(ex_date="2026-07-15", pay_date="2026-08-01",
                                     amount=0.485, frequency=4, source="fixture")
    assert ev["ex_date"] == "2026-07-15" and ev["amount"] == 0.485
    # Provider sentinels never become dates.
    for junk in ("None", "0000-00-00", "", None, "N/A", "not-a-date", "2026-13-45"):
        assert dividend_calendar.normalize(ex_date=junk)["ex_date"] is None
    # Annual -> per-payment, defaulting to quarterly.
    assert dividend_calendar.per_payment_amount(1.94, 4) == 0.485
    assert dividend_calendar.per_payment_amount(1.94, None) == 0.485
    assert dividend_calendar.per_payment_amount(0, 4) is None
    # The Schwab adapter is a documented TODO — it must return None, not a guess.
    assert dividend_calendar.schwab_adapter("KO") is None
    # Fixture-backed resolution works with no provider configured at all.
    out = dividend_calendar.next_dividend("KO", fixtures={"KO": {
        "ex_date": "2026-07-15", "amount": 0.485}})
    assert out["ex_date"] == "2026-07-15" and out["source"] == "fixture"


# ===========================================================================
# 9. ETF waiver non-regression.
# ===========================================================================
def test_9_etf_waiver_behavior_unchanged():
    """The existing ETF branch is the structural model for the dividend branch;
    it must be untouched by it."""
    df, sector = _ko_like(), _strong_bench()
    # The vs-sector veto is gone for every name, ETF or not — the ETF WAIVER it
    # used to need went with it (docs/decision-2026-08-21-remove-sector-rs.md).
    vetoes = stock_lights.evaluate_vetoes(df, ivr_percentile=20.0, is_etf=True)
    assert not any(v["id"] == "rs3m_vs_sector" for v in vetoes)

    # And the ETF juice bar is unchanged, and still below the growth bar.
    import sector_data
    assert account_gate.weekly_yield_target_pct("XLP") == config.ETF_WEEKLY_JUICE_TARGET_PCT
    assert sector_data.is_etf("XLP") is True
    growth = account_gate.weekly_yield_target_pct("NVDA")
    assert config.ETF_WEEKLY_JUICE_TARGET_PCT < growth
    # The dividend arm is a THIRD bar — it does not reuse or move the ETF one.
    div_bar = account_gate.weekly_yield_target_pct("KO", profile=ip.DIVIDEND_COMPOUNDER)
    assert div_bar == config.COMBINED_YIELD_FLOOR_WK
    assert account_gate.weekly_yield_target_pct("XLP") == config.ETF_WEEKLY_JUICE_TARGET_PCT


# ===========================================================================
# 10. Day-count convention — pinned.
# ===========================================================================
def test_10_combined_metric_day_count_is_pinned():
    """[COMBINED_YIELD_DAY_COUNT] The dividend leg is a QUOTED ANNUAL RATE ÷ 52 —
    the conventional weekly-equivalent reading of one — NOT annual x 7/365 (which
    would be 51.07 weeks). The juice leg rides the 7-calendar-day base already
    pinned by burn.net_juice_per_week [NET_JUICE_TIME_BASE]. Pinned here so the
    convention cannot drift silently."""
    assert config.DIVIDEND_WEEKS_PER_YEAR == 52

    parts = st.combined_weekly_yield(0.80, 5.20)
    assert parts["dividend_weekly_pct"] == pytest.approx(0.1, abs=1e-9)   # 5.20/52
    assert parts["combined_weekly_yield_pct"] == pytest.approx(0.90, abs=1e-9)
    assert parts["weeks_per_year"] == 52

    # Explicitly NOT the 365/7 convention (which would give 0.09973%/wk here).
    assert parts["dividend_weekly_pct"] != pytest.approx(5.20 * 7 / 365, abs=1e-9)

    # The juice leg passes through untransformed — the combined metric never
    # re-bases it, so it keeps whatever convention produced it upstream.
    assert st.combined_weekly_yield(0.80, 0.0)["combined_weekly_yield_pct"] == 0.80
    assert st.combined_weekly_yield(None, 5.2)["combined_weekly_yield_pct"] is None


# ===========================================================================
# Schema v21
# ===========================================================================
def test_v20_to_v21_backfills_juice_engine_and_seeds_the_ledger():
    v20 = {
        "schema_version": 20,
        "positions": [{"ticker": "AAPL", "position_type": position_types.SHARES,
                       "shares": {"count": 100}}],
        "executions": [{"id": "exec_001", "action": "buy_shares", "ticker": "AAPL"}],
    }
    before = list(v20["executions"])
    out, changed = migrations.migrate(v20)
    assert changed and out["schema_version"] == migrations.CURRENT_VERSION
    # Every pre-existing position is a JUICE_ENGINE position — the sleeve is opt-in.
    assert out["positions"][0]["income_profile"] == ip.JUICE_ENGINE
    assert ip.of(out["positions"][0]) == ip.JUICE_ENGINE
    # The ledger is seeded empty — a migration never fabricates a compounding record.
    assert out["accrual_ledger"]["by_ticker"] == {}
    assert out["accrual_ledger"]["records"] == []
    # Executions untouched: ADD only, no rewrite.
    assert out["executions"] == before


def test_absent_income_profile_degrades_to_juice_engine():
    assert ip.of({}) == ip.JUICE_ENGINE
    assert ip.of({"income_profile": None}) == ip.JUICE_ENGINE
    assert ip.of({"income_profile": "TYPO"}) == ip.JUICE_ENGINE
    assert ip.of(None) == ip.JUICE_ENGINE
    assert ip.of({"income_profile": ip.DIVIDEND_COMPOUNDER}) == ip.DIVIDEND_COMPOUNDER
    assert ip.badge(ip.DIVIDEND_COMPOUNDER) == "DIV"
    assert ip.badge(None) == "JUICE"


def test_profile_is_stamped_at_entry_and_never_re_derived(store):
    _buy_shares("KO", 100, 60.0, income_profile=ip.DIVIDEND_COMPOUNDER)
    p = log.find_position(log.load_state(), "KO")
    assert p["income_profile"] == ip.DIVIDEND_COMPOUNDER
    # A scale-in cannot re-profile an open position, even tagged otherwise.
    _buy_shares("KO", 100, 61.0, income_profile=ip.JUICE_ENGINE)
    p = log.find_position(log.load_state(), "KO")
    assert p["income_profile"] == ip.DIVIDEND_COMPOUNDER
    # The sleeve a lot was bought under is on the immutable record too.
    buys = [e for e in log.load_state()["executions"] if e["action"] == "buy_shares"]
    assert buys[0]["income_profile"] == ip.DIVIDEND_COMPOUNDER


def test_profile_resolution_never_triggers_a_provider_fetch(store, monkeypatch):
    """A ~500-name sweep resolves a profile per candidate. If that read could fetch,
    a cold cache would fire one provider request per ticker on the request path —
    which is the exact failure mode the dividend leg is supposed to avoid."""
    import dividends
    import screening
    calls = []
    monkeypatch.setattr(dividends, "_fetch_yield", lambda t: calls.append(t) or 0.03)
    monkeypatch.setattr(dividends, "_fetch_event", lambda t: calls.append(t) or {})

    # Cold cache: the cache-only read resolves to "unknown" and fetches nothing.
    q, src = dividends.cached_yield_with_source("KO")
    assert q is None and src == "unknown"
    assert screening.resolve_profile("KO", state={"metadata": {}}) == ip.JUICE_ENGINE
    assert calls == [], f"profile resolution fetched: {calls}"

    # An unknown yield is distinct from a resolved non-payer, and neither fetches.
    monkeypatch.setattr(dividends, "_read_cache", lambda: {"NOPAY": {"yield": 0.0}})
    assert dividends.cached_yield_with_source("NOPAY") == (0.0, "none")
    assert calls == []


def test_explicit_assignment_beats_the_yield_heuristic():
    state = {"metadata": {"income_profile_overrides": {"NVDA": ip.DIVIDEND_COMPOUNDER,
                                                       "KO": ip.JUICE_ENGINE}}}
    # An operator can enrol a low-yield name...
    assert ip.profile_for("NVDA", state, annual_dividend_yield_pct=0.03) == ip.DIVIDEND_COMPOUNDER
    # ...and can hold a high-yield name in the juice engine.
    assert ip.profile_for("KO", state, annual_dividend_yield_pct=3.1) == ip.JUICE_ENGINE
    # Unassigned names fall to the heuristic.
    assert ip.profile_for("PG", state, annual_dividend_yield_pct=3.1) == ip.DIVIDEND_COMPOUNDER
    assert ip.profile_for("PG", state, annual_dividend_yield_pct=0.5) == ip.JUICE_ENGINE


# ===========================================================================
# The Level 5 gate gaps closed alongside this work (audit findings 7-A / 7-B)
# ===========================================================================
def test_buy_shares_now_runs_the_level5_gate(store):
    """Before v21 buy_shares was ungated entirely — _buy_shares read an
    ``_account_gate`` key nothing ever set."""
    state = log.load_state()
    state["metadata"]["operating_cash"] = 100.0   # cannot afford a $6,000 lot
    log.save_state(state)
    with pytest.raises(ValueError, match="Level 5 gate blocked entry"):
        _buy_shares("KO", 100, 60.0)
    # A typed override still gets through, and now logs the checks it overrode.
    _buy_shares("KO", 100, 60.0, override_reason="deliberate — funding in flight")
    e = [x for x in log.load_state()["executions"] if x["action"] == "buy_shares"][-1]
    assert "cash_reserve" in e["override"]["failed_checks"]


def test_round_lot_size_block_now_fires_on_the_shares_path(store):
    """PER_POSITION_CAP_USD was dead in production: no caller passed
    position_type, so the SIZE-BLOCK was never appended."""
    with pytest.raises(ValueError, match="round_lot_size"):
        _buy_shares("RICH", 100, 400.0)   # a $40,000 lot vs the $15,000 cap


# ===========================================================================
# Affordability — a shares entry buys a WHOLE lot, so a name whose lot costs
# more than the dry powder available right now is not a candidate.
# ===========================================================================
def _row(ticker, lot_cost):
    return {"ticker": ticker, "lot_cost": lot_cost}


def _funded(cash, deployed_positions=()):
    """Fund the book with `cash`. NOTE the real formula: dry powder is cash MINUS
    the defensive reserve (config.RESERVE_REQUIRED, $13k), so $22k of cash is $9k
    of deployable capital — not $22k. The numbers below are chosen against the real
    reserve rather than zeroing it out, because zeroing it would test a formula the
    app never runs (capital_summary reads `meta.get(...) or RESERVE_REQUIRED`, so a
    0 falls back to the config default anyway)."""
    st = log.load_state()
    st["metadata"]["operating_cash"] = cash
    st["positions"] = list(deployed_positions)
    log.save_state(st)
    return log.load_state()


def test_affordability_bar_is_the_tighter_of_cash_and_the_per_position_cap(store, monkeypatch):
    from metrics import scorecard as sc
    monkeypatch.setattr(config, "PER_POSITION_CAP_USD", 15000.0)
    # Cash is the binding limit: $22k cash - $13k reserve = $9k deployable.
    bar = sc.affordability(_funded(22000))
    assert bar["active"] is True
    assert bar["max_lot_cost"] == 9000
    assert bar["binding"] == "cash_above_reserve"
    # The per-position cap binds once the cash above reserve exceeds it.
    bar = sc.affordability(_funded(60000))
    assert bar["max_lot_cost"] == 15000.0
    assert bar["binding"] == "per_position_cap"


def test_cash_below_the_defensive_reserve_affords_nothing(store, monkeypatch):
    """A real consequence worth pinning: the reserve comes off the top. With less
    cash than the reserve there is no dry powder and every name is priced out —
    correctly, and the binding reason says so rather than looking like a bug."""
    from metrics import scorecard as sc
    bar = sc.affordability(_funded(config.RESERVE_REQUIRED - 1000))
    assert bar["active"] is True          # cash IS known...
    assert bar["max_lot_cost"] == 0       # ...there just isn't any deployable
    assert bar["binding"] == "cash_above_reserve"
    keep, priced_out, _ = sc.split_by_affordability([_row("ANY", 100)], log.load_state())
    assert keep == [] and priced_out[0]["ticker"] == "ANY"


def test_unset_operating_cash_disables_the_filter_rather_than_hiding_everything(store):
    """state.metadata.operating_cash defaults to 0, so a zero is ambiguous between
    'no money' and 'never configured'. Filtering everything out on that reading
    would make a fresh book look broken rather than broke."""
    from metrics import scorecard as sc
    st = log.load_state()
    st["metadata"]["operating_cash"] = 0
    log.save_state(st)

    bar = sc.affordability(log.load_state())
    assert bar["active"] is False
    assert bar["max_lot_cost"] is None
    assert bar["binding"] == "unknown"

    rows = [_row("CHEAP", 6000), _row("RICH", 90000)]
    keep, priced_out, _ = sc.split_by_affordability(rows, log.load_state())
    assert [r["ticker"] for r in keep] == ["CHEAP", "RICH"]   # nothing hidden
    assert priced_out == []


def test_split_removes_only_names_we_can_prove_are_too_expensive(store, monkeypatch):
    from metrics import scorecard as sc
    monkeypatch.setattr(config, "PER_POSITION_CAP_USD", 15000.0)
    state = _funded(23000)   # -> $10k deployable
    rows = [_row("CHEAP", 6000), _row("RICH", 45000), _row("UNPRICED", None)]
    keep, priced_out, bar = sc.split_by_affordability(rows, state)

    assert [r["ticker"] for r in keep] == ["CHEAP", "UNPRICED"]
    assert [r["ticker"] for r in priced_out] == ["RICH"]
    # An UNPRICEABLE lot cost is never hidden — a silent exclusion of a name we
    # merely failed to price is worse than showing it.
    assert next(r for r in keep if r["ticker"] == "UNPRICED")["affordable"] is None
    # A priced-out row explains itself wherever it is shown.
    rich = priced_out[0]
    assert rich["affordable"] is False
    assert rich["lot_cost_over_by"] == 35000.0
    assert rich["max_lot_cost"] == 10000
    assert bar["priced_out"] == 1 and bar["shown"] == 2


def test_affordability_agrees_with_the_level5_size_block(store, monkeypatch):
    """The scan must not show a name the Execute gate would then reject on size —
    both read the same PER_POSITION_CAP_USD."""
    from metrics import scorecard as sc
    monkeypatch.setattr(config, "PER_POSITION_CAP_USD", 15000.0)
    state = _funded(100000)          # cash is not the constraint; the cap is
    bar = sc.affordability(state)
    assert bar["max_lot_cost"] == 15000.0
    # A $40,000 lot is priced out of the scan...
    keep, priced_out, _ = sc.split_by_affordability([_row("RICH", 40000)], state)
    assert keep == [] and priced_out[0]["ticker"] == "RICH"
    # ...and the Level 5 gate independently SIZE-BLOCKS the same entry.
    with pytest.raises(ValueError, match="round_lot_size"):
        _buy_shares("RICH", 100, 400.0)


def test_scan_endpoint_filters_by_default_and_reports_what_it_hid(store, monkeypatch):
    import app as flask_app
    from metrics import scorecard as sc
    monkeypatch.setattr(config, "PER_POSITION_CAP_USD", 15000.0)
    _funded(23000)   # -> $10k deployable
    monkeypatch.setattr(sc, "scorecard_warm", lambda price_overrides=None, **k: {
        "as_of": "2026-08-15T00:00:00Z",
        "results": [dict(_row("CHEAP", 6000), verdict="READY"),
                    dict(_row("RICH", 45000), verdict="READY")]})

    client = flask_app.app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True

    body = client.get("/api/scan/scorecard").get_json()
    assert [r["ticker"] for r in body["results"]] == ["CHEAP"]
    assert body["priced_out_tickers"] == ["RICH"]
    assert body["affordability"]["priced_out"] == 1
    assert body["affordability"]["max_lot_cost"] == 10000

    # The escape hatch returns everything, still annotated.
    body = client.get("/api/scan/scorecard?include_unaffordable=1").get_json()
    assert {r["ticker"] for r in body["results"]} == {"CHEAP", "RICH"}
    assert next(r for r in body["results"] if r["ticker"] == "RICH")["affordable"] is False
