"""LEAP capital-preservation tests — long-leg lifecycle, juice-vs-burn, delta
velocity, atomic exits, payback continuity across a LEAP roll.

Offline, no provider keys. Run with: python -m pytest backend -q
"""
import glob
import json
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import alerts            # noqa: E402
import backups           # noqa: E402
import config            # noqa: E402
import data_handler      # noqa: E402
import executor          # noqa: E402
import indicators        # noqa: E402
import leap_policy       # noqa: E402
import logging_handler as log  # noqa: E402
import migrations        # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _frame(level=100.0, n=80, seed=1):
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.RandomState(seed)
    prices = level + np.cumsum(rng.normal(0, 0.4, n))
    c = pd.Series(prices, index=idx)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e6}, index=idx)


# ---------------------------------------------------------------------------
# 1. Lifecycle — DTE, extrinsic weeks, roll policy, roll-cost estimator
# ---------------------------------------------------------------------------
def test_roll_policy_triggers_independently_and_together():
    # DTE below floor only.
    r = leap_policy.roll_policy(leap_dte=80, extrinsic_weeks_remaining=10)
    assert r["roll_due"] and len(r["reasons"]) == 1 and "DTE" in r["reasons"][0]
    # Extrinsic runway too short only.
    r = leap_policy.roll_policy(leap_dte=150, extrinsic_weeks_remaining=3)
    assert r["roll_due"] and len(r["reasons"]) == 1 and "runway" in r["reasons"][0]
    # Both.
    r = leap_policy.roll_policy(leap_dte=80, extrinsic_weeks_remaining=3)
    assert r["roll_due"] and len(r["reasons"]) == 2
    # Neither.
    r = leap_policy.roll_policy(leap_dte=150, extrinsic_weeks_remaining=10)
    assert not r["roll_due"] and r["reasons"] == []


def test_leap_health_dte_and_extrinsic_weeks():
    # Stock 100, strike 80, LEAP worth 3000/contract*... current_bid is per-position
    # total. intrinsic = (100-80)*5*100 = 10000; value 26000 -> extrinsic 16000.
    position = {
        "ticker": "NVDA", "status": "active",
        "leap": {"strike": 80, "contracts": 5, "current_bid": 26000.0, "dte": 150},
        "leap_dte": 150, "trailing_avg_weekly_juice": 400.0, "delta_history": [],
    }
    h = leap_policy.leap_health(position, df=_frame(100), stock_price=100.0)
    assert h["leap_dte"] == 150
    assert h["leap_extrinsic_remaining"] == pytest.approx(16000.0)
    # 16000 / 400 = 40 weeks of runway
    assert h["leap_extrinsic_weeks_remaining"] == pytest.approx(40.0, abs=0.1)
    assert h["leap_extrinsic_below_intrinsic"] is False


def test_roll_cost_estimate_with_and_without_chain(store, monkeypatch):
    monkeypatch.setattr(data_handler, "get_daily", lambda *a, **k: _frame(100, seed=3))
    state = log.load_state()
    state["positions"] = [{
        "ticker": "NVDA", "sector": "XLK", "status": "active",
        "leap": {"strike": 70, "contracts": 5, "current_bid": 16000.0, "dte": 120,
                 "cost_basis": 15000.0}, "delta_history": [],
    }]
    state["metadata"]["operating_cash"] = 50000
    log.save_state(state)

    est = leap_policy.roll_cost_estimate("NVDA", state=state)
    assert est.get("error") is None
    assert est["new_leap"]["strike"] is not None
    assert est["net_debit"] is not None
    assert isinstance(est["reserve_ok"], bool)
    # bid-side of the current LEAP came from the stored mark (16000).
    assert est["current_leap"]["sell_to_close_value"] == pytest.approx(16000.0)


# ---------------------------------------------------------------------------
# 2. Payback continuity across a LEAP roll vs a true exit+re-entry
# ---------------------------------------------------------------------------
def _exec(action, ticker, date, **kw):
    return {"action": action, "ticker": ticker, "date": date, **kw}


def test_payback_carries_across_leap_roll(store):
    state = log.load_state()
    state["positions"] = [{"ticker": "NVDA", "status": "active",
                           "leap": {"strike": 80, "contracts": 5, "extrinsic_at_entry": 22500},
                           "delta_history": []}]
    state["executions"] = [
        _exec("buy_leap", "NVDA", "2025-01-06T00:00:00Z", extrinsic_captured=2000),
        _exec("close_short", "NVDA", "2025-01-10T00:00:00Z", net_juice_total=350,
              extrinsic_sold=1.0, extrinsic_paid_back=0.3, contracts=5),
        _exec("close_short", "NVDA", "2025-01-17T00:00:00Z", net_juice_total=350,
              extrinsic_sold=1.0, extrinsic_paid_back=0.3, contracts=5),
        # LEAP roll: linked close_leap + buy_leap (shared leap_roll_id).
        _exec("close_leap", "NVDA", "2025-01-20T00:00:00Z", leap_roll_id="lr1", realized_pnl=0),
        _exec("buy_leap", "NVDA", "2025-01-20T00:00:00Z", leap_roll_id="lr1", extrinsic_captured=22500),
    ]
    log.recompute_derived(state)
    pb = state["extrinsic_payback"]["NVDA"]
    # Juice carries (700) and the new LEAP extrinsic is ADDED to the target.
    assert pb["collected_to_date"] == pytest.approx(700.0)
    assert pb["leap_extrinsic_at_entry"] == pytest.approx(24500.0)  # 2000 + 22500


def _full_cycle_execs():
    """One realistic full cycle from the execution log (R5):
    multi-tranche LEAP entry -> weekly short closes -> defensive short roll ->
    LEAP roll (leap_roll_id) -> more shorts -> partial close -> true exit."""
    D = "2025-01-%02dT00:00:00Z"
    return [
        _exec("buy_leap", "ABC", D % 6, extrinsic_captured=2000),                    # 0 fresh cycle
        _exec("buy_leap", "ABC", D % 6, extrinsic_captured=1500, leap_add="add"),    # 1 multi-tranche add
        _exec("close_short", "ABC", D % 10, net_juice_total=300, contracts=5),       # 2 weekly close
        _exec("close_short", "ABC", D % 17, net_juice_total=300, contracts=5),       # 3 weekly close
        # 4-5 defensive short roll (close_short net of buyback + sell_short), shares a roll_id.
        _exec("close_short", "ABC", D % 21, net_juice_total=150, contracts=5,
              roll_id="roll1", roll_reason="defend", close_total=500, strike=120),
        _exec("sell_short", "ABC", D % 21, premium_total=650, roll_id="roll1", strike=118),
        # 6-7 LEAP roll: linked close_leap + buy_leap (shared leap_roll_id).
        _exec("close_leap", "ABC", D % 24, leap_roll_id="lr1", realized_pnl=0),      # 6 roll close (latch)
        _exec("buy_leap", "ABC", D % 24, leap_roll_id="lr1", extrinsic_captured=3000),  # 7 roll buy (carry)
        _exec("close_short", "ABC", D % 28, net_juice_total=400, contracts=5),       # 8 more shorts
        _exec("close_leap", "ABC", D % 30, legs_remaining=1),                        # 9 partial close (carries)
        _exec("close_leap", "ABC", D % 31, legs_remaining=0),                        # 10 true exit (reset)
    ]


def _cycle_state():
    state = log.load_state()
    # extrinsic_at_entry=0 so the post-true-exit fallback reads a clean 0 target;
    # while a cycle is live, cycle_target wins and this fallback is ignored.
    state["positions"] = [{"ticker": "ABC", "status": "active",
                           "leap": {"strike": 120, "contracts": 5, "extrinsic_at_entry": 0},
                           "delta_history": []}]
    return state


def test_payback_full_cycle_state_at_every_transition(store):
    """R5: replay the full cycle and assert the payback target AND collected state
    at EVERY transition, not just the end state."""
    execs = _full_cycle_execs()
    # (prefix length, expected leap_extrinsic_at_entry (target), expected collected)
    checkpoints = [
        (1,  2000.0, 0.0),      # fresh cycle: target = first LEAP extrinsic
        (2,  3500.0, 0.0),      # multi-tranche add: target grows, collected carries (0)
        (3,  3500.0, 300.0),    # first weekly close
        (4,  3500.0, 600.0),    # second weekly close
        (5,  3500.0, 750.0),    # defensive roll's close_short (net of buyback) adds 150
        (6,  3500.0, 750.0),    # the roll's sell_short leg does not touch payback
        (7,  3500.0, 750.0),    # LEAP roll close latches; target/collected unchanged
        (8,  6500.0, 750.0),    # roll buy: +3000 to target, juice carries
        (9,  6500.0, 1150.0),   # more shorts
        (10, 6500.0, 1150.0),   # partial close (legs_remaining=1): cycle carries
        (11, 0.0,    0.0),      # true exit (legs_remaining=0): cycle resets
    ]
    for n, exp_target, exp_collected in checkpoints:
        state = _cycle_state()
        state["executions"] = execs[:n]
        log.recompute_derived(state)
        pb = state["extrinsic_payback"]["ABC"]
        assert pb["leap_extrinsic_at_entry"] == pytest.approx(exp_target), f"target at prefix {n}"
        assert pb["collected_to_date"] == pytest.approx(exp_collected), f"collected at prefix {n}"

    # The complete, well-formed log validates clean.
    state = _cycle_state()
    state["executions"] = execs
    log.recompute_derived(state)
    assert state["payback_reconciliation"]["ok"] is True
    assert log.validate_payback(execs) == []


def test_payback_validation_flags_missing_leap_roll_id(store):
    """R5 negative: strip the leap_roll_id off the roll's BUY leg. The replay would
    SILENTLY demote it to a fresh cycle (dropping the carried juice and resetting
    the target) — the state machine must instead flag reconciliation, loudly."""
    execs = _full_cycle_execs()
    execs[7] = dict(execs[7]); execs[7].pop("leap_roll_id")   # buy leg mislabeled
    issues = log.validate_payback(execs)
    assert any(i["type"] == "dangling_leap_roll" and i["ticker"] == "ABC" for i in issues)

    state = _cycle_state()
    state["executions"] = execs
    log.recompute_derived(state)
    assert state["payback_reconciliation"]["ok"] is False


def test_payback_validation_ignores_in_progress_roll(store):
    """R5: the executor appends a LEAP roll's close_leap and buy_leap as two
    separate recompute-triggering appends. In the one-append window the close is
    latched with no buy yet — a legitimately IN-PROGRESS roll, NOT corruption. The
    validator must stay clean when that dangling close is the ticker's LAST
    execution, and only flag once later activity proves the buy never came."""
    execs = _full_cycle_execs()
    # Truncate right after the roll's close_leap (index 6) — buy leg not yet
    # appended, close is the last execution: in-progress, must NOT flag.
    in_progress = execs[:7]
    assert log.validate_payback(in_progress) == []
    state = _cycle_state()
    state["executions"] = in_progress
    log.recompute_derived(state)
    assert state["payback_reconciliation"]["ok"] is True

    # But a close that never gets its buy AND has later same-ticker activity is a
    # genuine orphan -> flagged.
    orphaned = execs[:7] + [_exec("close_short", "ABC", "2025-01-27T00:00:00Z",
                                  net_juice_total=100, contracts=5)]
    issues = log.validate_payback(orphaned)
    assert any(i["type"] == "dangling_leap_roll" for i in issues)


def test_payback_validation_never_raises_on_bad_legs_remaining(store):
    """R5: validate_payback must survive a corrupt (non-numeric) legs_remaining
    stamp — it exists to flag corruption, not choke on it. And recompute_derived
    guards the whole validation call so a pathological execution can never break
    the recompute (the reconciliation degrades to unvalidated, never raises)."""
    # validate_payback does not raise on a non-numeric legs_remaining; the
    # un-parseable stamp is simply not count-checked (junk, not a real
    # disagreement), so no mismatch is emitted.
    corrupt = _full_cycle_execs()
    corrupt[9] = dict(corrupt[9], legs_remaining="oops")
    issues = log.validate_payback(corrupt)
    assert not any(i["type"] == "legs_remaining_mismatch" for i in issues)

    # And the recompute guards the whole validation call: even if validate_payback
    # blew up on some future input, recompute_derived degrades to unvalidated
    # rather than raising. Use a CLEAN log (the replay's own int() would choke on
    # 'oops' first) and force validate_payback to raise.
    state = _cycle_state()
    state["executions"] = _full_cycle_execs()
    import logging_handler as _lh
    orig = _lh.validate_payback
    try:
        _lh.validate_payback = lambda _e: (_ for _ in ()).throw(RuntimeError("boom"))
        _lh.recompute_derived(state)   # must not raise despite validation blowing up
    finally:
        _lh.validate_payback = orig
    assert state["payback_reconciliation"] == {"ok": True, "issues": []}


def test_payback_validation_flags_wrong_legs_remaining(store):
    """R5 negative: mis-stamp legs_remaining on the partial close (1 -> 0). The
    replay would treat it as a TRUE exit and wipe a live cycle. The count-based
    validation catches the disagreement with the execution history."""
    execs = _full_cycle_execs()
    execs[9] = dict(execs[9], legs_remaining=0)   # was 1 (one leg still open)
    issues = log.validate_payback(execs)
    assert any(i["type"] == "legs_remaining_mismatch" and i["ticker"] == "ABC" for i in issues)

    state = _cycle_state()
    state["executions"] = execs
    log.recompute_derived(state)
    assert state["payback_reconciliation"]["ok"] is False


def test_payback_resets_on_true_exit_and_reentry(store):
    state = log.load_state()
    state["positions"] = [{"ticker": "NVDA", "status": "active",
                           "leap": {"strike": 80, "contracts": 5, "extrinsic_at_entry": 5000},
                           "delta_history": []}]
    state["executions"] = [
        _exec("buy_leap", "NVDA", "2025-01-06T00:00:00Z", extrinsic_captured=2000),
        _exec("close_short", "NVDA", "2025-01-10T00:00:00Z", net_juice_total=350, contracts=5),
        # TRUE exit (no leap_roll_id), then a fresh entry days later.
        _exec("close_leap", "NVDA", "2025-01-20T00:00:00Z", realized_pnl=100),
        _exec("buy_leap", "NVDA", "2025-02-03T00:00:00Z", extrinsic_captured=5000),
    ]
    log.recompute_derived(state)
    pb = state["extrinsic_payback"]["NVDA"]
    # New cycle: prior juice does NOT carry, target is only the new LEAP extrinsic.
    assert pb["collected_to_date"] == pytest.approx(0.0)
    assert pb["leap_extrinsic_at_entry"] == pytest.approx(5000.0)


# ---------------------------------------------------------------------------
# 3. Migration v5 -> v6
# ---------------------------------------------------------------------------
def test_v5_fixture_migrates_and_seeds_delta_history(store):
    v5 = {
        "schema_version": 5,
        "metadata": {"last_updated": "2025-01-01T00:00:00Z"},
        "positions": [{"ticker": "NVDA", "status": "active",
                       "leap": {"strike": 80, "contracts": 5}}],
        "executions": [], "theta_ledger": {"weeks": [], "totals": {}},
        "extrinsic_payback": {}, "roll_ledger": {"rolls": [], "by_ticker": {}},
        "cycles": [], "pending_orders": {},
    }
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(v5, fh)

    state = log.load_state()
    assert state["schema_version"] == migrations.CURRENT_VERSION
    assert state["positions"][0]["delta_history"] == []  # v5->v6 seeding still applies
    # Pre-migration snapshot was taken (v5 bytes) before the migrated save; the
    # chain now runs v5 all the way to CURRENT_VERSION in one pass.
    snaps = glob.glob(os.path.join(backups.backups_dir(),
                                   f"pre-migration-v5-to-v{migrations.CURRENT_VERSION}-*.json"))
    assert len(snaps) == 1
    assert json.load(open(snaps[0], encoding="utf-8"))["schema_version"] == 5


def test_v13_fixture_gains_planned_exit_dte(store):
    """v14: existing positions gain planned_exit_dte = config.PLANNED_EXIT_DTE;
    schema version bumps; the old-state fixture loads cleanly."""
    v13 = {
        "schema_version": 13,
        "metadata": {"last_updated": "2025-01-01T00:00:00Z"},
        "positions": [
            {"ticker": "XLK", "status": "active", "planned_exit_dte": 120,
             "leap": {"strike": 205, "contracts": 1}, "delta_history": []},
            {"ticker": "NVDA", "status": "active",
             "leap": {"strike": 80, "contracts": 5}, "delta_history": []},
        ],
        "executions": [], "theta_ledger": {"weeks": [], "totals": {}},
        "extrinsic_payback": {}, "roll_ledger": {"rolls": [], "by_ticker": {}},
        "cycles": [], "pending_orders": {},
    }
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(v13, fh)

    state = log.load_state()
    assert state["schema_version"] == migrations.CURRENT_VERSION >= 14
    # Position without the field gets the default; an explicit value is preserved.
    by_ticker = {p["ticker"]: p for p in state["positions"]}
    assert by_ticker["NVDA"]["planned_exit_dte"] == config.PLANNED_EXIT_DTE
    assert by_ticker["XLK"]["planned_exit_dte"] == 120  # setdefault leaves it alone


# ---------------------------------------------------------------------------
# 4. Juice-vs-burn
# ---------------------------------------------------------------------------
def test_leap_weekly_burn_is_bs_theta_not_straight_line():
    S, K, dte, contracts = 100.0, 80.0, 120, 5
    mark = indicators._bs_call_price(S, K, dte / 365.0, config.RISK_FREE_RATE, 0.30)
    burn = indicators.leap_weekly_burn(S, K, dte, mark, contracts)
    # Independent BS theta computation → must match the module, per contract*100.
    iv = indicators.implied_vol_call(mark, S, K, dte / 365.0, config.RISK_FREE_RATE)
    _, theta_day, _ = indicators.call_greeks_full(S, K, dte / 365.0, config.RISK_FREE_RATE, iv)
    assert burn == pytest.approx(-theta_day * 7 * contracts * 100, abs=0.5)
    # And NOT the naive straight-line extrinsic ÷ DTE × 7.
    extrinsic_total = (mark - max(S - K, 0)) * contracts * 100
    straight_line = extrinsic_total / dte * 7
    assert abs(burn - straight_line) > 1.0


def _burning_state(store_path, monkeypatch, juice_per_week):
    """A one-position state whose completed-week juice is `juice_per_week` and
    whose LEAP is priced so BS burn exceeds it → net-negative maintenance."""
    frame = _frame(100, seed=5)
    monkeypatch.setattr(data_handler, "get_daily", lambda *a, **k: frame)
    state = log.load_state()
    S, K, dte, n = indicators.last(frame), 80.0, 120, 5
    mark = indicators._bs_call_price(S, K, dte / 365.0, config.RISK_FREE_RATE, 0.35)
    state["positions"] = [{
        "ticker": "NVDA", "sector": "XLK", "status": "active",
        "leap": {"strike": K, "contracts": n, "current_bid": round(mark * n * 100, 2), "dte": dte},
        "leap_dte": dte, "trailing_avg_weekly_juice": juice_per_week, "delta_history": [],
    }]
    # Two completed weeks of juice below the burn.
    state["theta_ledger"] = {"weeks": [
        {"week": "2025-W01", "ticker": "NVDA", "net_juice": juice_per_week},
        {"week": "2025-W02", "ticker": "NVDA", "net_juice": juice_per_week},
    ], "totals": {}}
    log.save_state(state)
    return state


def test_capital_burn_fires_once_and_resolves(store, monkeypatch):
    _burning_state(store, monkeypatch, juice_per_week=20.0)  # tiny juice, big burn
    burn = leap_policy.leap_health(log.load_state()["positions"][0])["leap_weekly_burn"]
    assert burn and burn > 20.0  # precondition: burning

    fired = alerts.check_capital_burn(log.load_state())
    assert [a["type"] for a in fired] == ["CAPITAL_BURN"]

    # Dedup: through the run loop it fires once, then holds.
    r1 = alerts.run(notify=False)
    assert any(a["type"] == "CAPITAL_BURN" for a in r1["fired"])
    r2 = alerts.run(notify=False)
    assert not any(a["type"] == "CAPITAL_BURN" for a in r2["fired"])

    # Flip juice above the burn → condition clears (auto-resolves).
    st = log.load_state()
    high = burn + 500
    for w in st["theta_ledger"]["weeks"]:
        w["net_juice"] = high
    st["positions"][0]["trailing_avg_weekly_juice"] = high
    log.save_state(st)
    r3 = alerts.run(notify=False)
    assert any(a["type"] == "CAPITAL_BURN" for a in r3["resolved"])


def test_maintenance_positive_is_self_funding(store, monkeypatch):
    _burning_state(store, monkeypatch, juice_per_week=20.0)
    st = log.load_state()
    st["positions"][0]["trailing_avg_weekly_juice"] = 5000.0  # huge juice
    h = leap_policy.leap_health(st["positions"][0])
    assert h["net_weekly_maintenance"] > 0
    assert h["maintenance_status"] == "self_funding"


# ---------------------------------------------------------------------------
# 5. Delta velocity
# ---------------------------------------------------------------------------
def _velocity_state(store_path, monkeypatch, live_delta):
    monkeypatch.setattr(data_handler, "get_daily", lambda *a, **k: _frame(100, seed=7))
    monkeypatch.setattr(indicators, "call_greeks", lambda *a, **k: (live_delta, 30.0))
    state = log.load_state()
    hist = [{"date": f"2025-01-{d:02d}", "leap_delta": v}
            for d, v in zip(range(6, 12), [0.80, 0.78, 0.76, 0.74, 0.72, 0.70])]
    state["positions"] = [{
        "ticker": "NVDA", "sector": "XLK", "status": "active",
        "leap": {"strike": 80, "contracts": 5, "current_bid": 20000.0, "dte": 150},
        "leap_dte": 150, "trailing_avg_weekly_juice": 400.0, "delta_history": hist,
    }]
    log.save_state(state)
    return state


def test_delta_velocity_fires_above_floor(store, monkeypatch):
    _velocity_state(store, monkeypatch, live_delta=0.70)  # above the 0.50 floor
    types = [a["type"] for a in alerts.evaluate(log.load_state())]
    assert "DELTA_VELOCITY" in types
    assert "DELTA_UNCOVERED" not in types


def test_delta_velocity_yields_to_floor_below(store, monkeypatch):
    _velocity_state(store, monkeypatch, live_delta=0.45)  # below the floor
    types = [a["type"] for a in alerts.evaluate(log.load_state())]
    assert "DELTA_VELOCITY" not in types      # floor owns this regime
    assert "DELTA_UNCOVERED" in types


# ---------------------------------------------------------------------------
# 6. Atomic exit
# ---------------------------------------------------------------------------
def _seed_open_position(monkeypatch):
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 75, "contracts": 5,
                      "execution_price": 6000, "execution_total": 30000, "stock_price": 100,
                      "extrinsic_captured": 2000, "expiration": "2026-06-18", "dte": 180,
                      "override_reason": "test", "circuit_breaker_price": 80})
    executor.execute({"action": "sell_short", "ticker": "NVDA", "strike": 100, "contracts": 5,
                      "premium_per_share": 1.0, "stock_price": 100, "expiration": "2026-01-16", "dte": 5})


def test_atomic_exit_books_both_legs_with_shared_exit_id(store, monkeypatch):
    _seed_open_position(monkeypatch)
    res = executor.execute({"action": "close_position_atomic", "ticker": "NVDA",
                            "leap_close_price": 6500, "stock_price": 100,
                            "exit_reason": "KILL_SWITCH_SECTOR",
                            # short buyback comes from the stored short mark
                            })
    assert res["status"] == "filled"
    exits = [e for e in res["executions"]]
    assert len(exits) == 2
    assert {e["exit_id"] for e in exits} == {res["exit_id"]}
    assert {e["exit_leg"] for e in exits} == {"leap", "short"}
    # Derived P&L on the LEAP leg: sold 6500×5=32500 − cost 30000 = +2500.
    leap_leg = next(e for e in exits if e["exit_leg"] == "leap")
    assert leap_leg["realized_pnl"] == pytest.approx(2500.0)
    st = log.load_state()
    pos = log.find_position(st, "NVDA")
    assert pos["status"] == "closed" and pos["leap"] is None and pos["short_calls"] == []


def test_single_leg_close_leap_with_open_short_is_rejected(store, monkeypatch):
    _seed_open_position(monkeypatch)
    before = log.load_state()
    n_before = len(before["executions"])
    with pytest.raises(ValueError, match="naked short"):
        executor.execute({"action": "close_leap", "ticker": "NVDA", "strike": 75,
                          "contracts": 5, "close_price": 6000, "stock_price": 100})
    after = log.load_state()
    assert len(after["executions"]) == n_before            # no execution logged
    assert log.find_position(after, "NVDA")["leap"] is not None  # LEAP untouched


class _FakeClient:
    def __init__(self, order):
        self._order = order
    def primary_account_hash(self):
        return "acct"
    def place_order(self, account_hash, order):
        return {"orderId": "OID1"}
    def get_order(self, account_hash, order_id):
        return self._order


def test_atomic_exit_live_lifecycle_mocked(store, monkeypatch):
    _seed_open_position(monkeypatch)
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    import schwab_api
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    # Filled order with per-leg fills matched by legId->symbol.
    filled = {
        "status": "FILLED",
        "orderLegCollection": [
            {"legId": 1, "instrument": {"symbol": "NVDA_LEAP"}},
            {"legId": 2, "instrument": {"symbol": "NVDA_SHORT"}},
        ],
        "orderActivityCollection": [{"executionLegs": [
            {"legId": 1, "price": 65.0}, {"legId": 2, "price": 0.30}]}],
    }
    monkeypatch.setattr(data_handler, "client", lambda: _FakeClient(filled))

    placed = executor.execute({
        "action": "close_position_atomic", "ticker": "NVDA", "stock_price": 100,
        "exit_reason": "CB_DRAWDOWN_15",
        "leap_option_symbol": "NVDA_LEAP",
        "short_option_symbols": {"100": "NVDA_SHORT"},
        "leap_close_price": 6500,
    })
    assert placed["status"] == "working" and placed["order_id"] == "OID1"
    # Poll -> FILLED commits both legs at the real per-leg fills.
    done = executor.order_status("OID1")
    assert done["status"] == "filled"
    st = log.load_state()
    pos = log.find_position(st, "NVDA")
    assert pos["status"] == "closed" and pos["leap"] is None
    leap_leg = next(e for e in done["executions"] if e.get("exit_leg") == "leap")
    assert leap_leg["close_price"] == pytest.approx(6500.0)  # 65.0×100


# ---------------------------------------------------------------------------
# 7. Extrinsic clamp + liquidity flag
# ---------------------------------------------------------------------------
def test_leap_mark_below_intrinsic_floors_and_flags():
    # intrinsic = (100-70)*5*100 = 15000; mark quotes 14000 (below intrinsic).
    position = {
        "ticker": "NVDA", "status": "active",
        "leap": {"strike": 70, "contracts": 5, "current_bid": 14000.0, "dte": 150},
        "leap_dte": 150, "trailing_avg_weekly_juice": 400.0, "delta_history": [],
    }
    h = leap_policy.leap_health(position, df=_frame(100), stock_price=100.0)
    assert h["leap_extrinsic_remaining"] == 0.0          # floored, not negative
    assert h["leap_extrinsic_below_intrinsic"] is True   # liquidity flag set


# ---------------------------------------------------------------------------
# 8. Demo/paper mode must NEVER transmit a real order to the broker
# ---------------------------------------------------------------------------
class _ExplodingClient:
    """Any broker contact from a demo session is a test failure — a demo/paper
    trade must never resolve a live account or place a real order."""
    def primary_account_hash(self):
        raise AssertionError("demo session must not resolve a live account")

    def place_order(self, account_hash, order):
        raise AssertionError("demo session must not place a live order")


def test_demo_mode_never_transmits_even_with_live_enabled(store, monkeypatch):
    # Both live switches are ON, but the session is in demo/paper mode: the trade
    # must be committed as paper (logged), never routed to the broker.
    monkeypatch.setattr(config, "_demo_mode", True)
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    import schwab_api
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _ExplodingClient())

    assert executor.live_transmit() is False  # demo overrides the raw flag

    res = executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 75,
                            "contracts": 5, "execution_price": 6000, "execution_total": 30000,
                            "stock_price": 100, "extrinsic_captured": 2000,
                            "expiration": "2026-06-18", "dte": 180,
                            "override_reason": "test", "circuit_breaker_price": 80})
    # Committed immediately as paper: no working order, no broker contact, and the
    # execution is stamped logged / not live-transmitted.
    assert res["status"] == "filled" and res["mode"] == "logged"
    assert res["execution"]["live_transmitted"] is False
    # Position landed in the demo book (the live state.json is untouched).
    pos = log.find_position(log.load_state(), "NVDA")
    assert pos is not None and pos["leap"]["contracts"] == 5
    assert not os.path.exists(config.STATE_PATH)  # nothing written to the live store


def test_place_live_guard_blocks_demo_broker_call(store, monkeypatch):
    # Defense-in-depth: the broker-boundary guard raises if _place_live is reached
    # in demo mode, even if a caller skipped the mode check upstream.
    monkeypatch.setattr(config, "_demo_mode", True)
    import schwab_api
    with pytest.raises(schwab_api.SchwabError, match="demo/paper mode"):
        executor._place_live({"option_symbol": "NVDA_X"}, "NVDA", "buy_leap",
                             5, 75, 100.0, "supplied")
