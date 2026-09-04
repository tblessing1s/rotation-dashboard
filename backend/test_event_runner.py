"""Event-driven recommendation passes: signal detection off a fresh quote, edge
+ cooldown gating, and the poller hook. Offline — the engine itself is faked."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import event_runner as er

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 7, 8, 11, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    er.reset()
    monkeypatch.setattr(config, "demo_enabled", lambda: False)
    monkeypatch.delenv("CFM_EVENT_RUNS", raising=False)
    yield
    er.reset()


def _short(strike=100.0, dte=5, sold=2.0, entry_extrinsic=2.0, current_bid=1.0):
    return {"strike": strike, "dte": dte, "contracts": 1, "expiration": "2026-07-17",
            "entry_premium_total": sold * 100, "entry_extrinsic_per_share": entry_extrinsic,
            "current_bid": current_bid}


def _state(shorts, ticker="AAPL"):
    return {"positions": [{"ticker": ticker, "status": "active",
                           "position_type": "shares", "short_calls": shorts}]}


# --- detection -------------------------------------------------------------
def test_extrinsic_captured_flips_true_on_the_fresh_print():
    # Sold $2 extrinsic; bid $1 (all extrinsic while OTM at 90) -> 50% captured.
    st = _state([_short(strike=100.0, sold=2.0, entry_extrinsic=2.0, current_bid=1.0)])
    assert er.detect_signals(st, {"AAPL": {"price": 90.0}}) == {"AAPL": set()}
    # The stock runs to 100.7: intrinsic 0.70 eats the bid -> extrinsic 0.30
    # -> 85% captured, over the 80% threshold.
    sig = er.detect_signals(st, {"AAPL": {"price": 100.7}})
    assert er.EXTRINSIC_CAPTURED in sig["AAPL"]


def test_roll_75_reads_the_buyback_rule():
    st = _state([_short(sold=2.0, entry_extrinsic=2.0, current_bid=0.4, dte=5)])  # 80% decayed
    assert er.ROLL_75 in er.detect_signals(st, {"AAPL": {"price": 90.0}})["AAPL"]
    # ...but not inside the DTE floor the rule carries.
    st2 = _state([_short(sold=2.0, entry_extrinsic=2.0, current_bid=0.4, dte=1)])
    assert er.ROLL_75 not in er.detect_signals(st2, {"AAPL": {"price": 90.0}})["AAPL"]


def test_unpolled_and_closed_positions_are_silent():
    st = _state([_short(current_bid=0.1)])
    st["positions"].append({"ticker": "MSFT", "status": "closed", "short_calls": [_short(current_bid=0.1)]})
    assert er.detect_signals(st, {}) == {}
    assert set(er.detect_signals(st, {"AAPL": {"price": 90.0}, "MSFT": {"price": 90.0}})) == {"AAPL"}


# --- gating ------------------------------------------------------------------
def test_first_cycle_primes_and_runs_nothing():
    g = er.EventGate()
    assert g.observe({"AAPL": {er.ROLL_75}}, now=T0) == []
    # still true next cycle -> not an edge
    assert g.observe({"AAPL": {er.ROLL_75}}, now=T0 + timedelta(minutes=2)) == []
    # a NEW signal on the same name is an edge
    assert g.observe({"AAPL": {er.ROLL_75, er.EXTRINSIC_CAPTURED}},
                     now=T0 + timedelta(minutes=4)) == ["AAPL:extrinsic_captured"]


def test_signal_that_clears_then_returns_is_a_fresh_edge():
    g = er.EventGate()
    g.observe({"AAPL": set()}, now=T0)
    assert g.observe({"AAPL": {er.ROLL_75}}, now=T0 + timedelta(minutes=2)) == ["AAPL:roll_75"]
    g.mark_ran(["AAPL:roll_75"], T0 + timedelta(minutes=2))
    g.observe({"AAPL": set()}, now=T0 + timedelta(minutes=20))
    assert g.observe({"AAPL": {er.ROLL_75}}, now=T0 + timedelta(minutes=22)) == ["AAPL:roll_75"]


def test_a_name_not_polled_this_cycle_keeps_its_read():
    g = er.EventGate()
    g.observe({"AAPL": {er.ROLL_75}}, now=T0)
    g.observe({}, now=T0 + timedelta(minutes=2))          # AAPL not in the batch
    assert g.observe({"AAPL": {er.ROLL_75}}, now=T0 + timedelta(minutes=4)) == []


def test_per_ticker_cooldown_and_global_gap(monkeypatch):
    monkeypatch.setattr(config, "EVENT_RUN_COOLDOWN_SECONDS", 900)
    monkeypatch.setattr(config, "EVENT_RUN_MIN_GAP_SECONDS", 120)
    g = er.EventGate()
    g.observe({"AAPL": set(), "MSFT": set()}, now=T0)
    t1 = T0 + timedelta(minutes=2)
    assert g.observe({"AAPL": {er.ROLL_75}}, now=t1) == ["AAPL:roll_75"]
    g.mark_ran(["AAPL:roll_75"], t1)
    # 1 min later MSFT flips: inside the global gap -> nothing
    assert g.observe({"MSFT": {er.ROLL_75}}, now=t1 + timedelta(minutes=1)) == []
    # the edge was consumed by the read above; MSFT flipping again 5 min later is not new
    assert g.observe({"MSFT": {er.ROLL_75}}, now=t1 + timedelta(minutes=5)) == []
    # AAPL gets a second signal 10 min later: inside its 15-min cooldown -> nothing
    assert g.observe({"AAPL": {er.ROLL_75, er.ASSIGNMENT_RISK}}, now=t1 + timedelta(minutes=10)) == []
    # a defense escalation on a cool name runs at once
    assert g.observe({}, escalation_symbols=["NVDA"], now=t1 + timedelta(minutes=11)) == ["NVDA:defense"]
    g.mark_ran(["NVDA:defense"], t1 + timedelta(minutes=11))
    # market escalation is its own key
    assert g.observe({}, market_escalation=True, now=t1 + timedelta(minutes=14)) == ["MARKET:market"]


# --- the hook ------------------------------------------------------------------
def test_maybe_run_calls_the_engine_with_the_reasons_and_primes_the_quote(monkeypatch):
    import data_handler
    import recommendation_runner as runner
    calls = []
    monkeypatch.setattr(runner, "run", lambda **kw: calls.append(kw) or {"emitted": 1, "trigger": kw["trigger"]})
    remembered = {}
    monkeypatch.setattr(data_handler, "_remember_quote", lambda sym, q: remembered.__setitem__(sym, q))
    st = _state([_short(sold=2.0, entry_extrinsic=2.0, current_bid=1.0)])
    quotes = {"AAPL": {"price": 90.0, "source": "schwab"}}
    assert er.maybe_run(st, quotes, now=T0) is None                      # primes
    assert er.maybe_run(st, quotes, now=T0 + timedelta(minutes=2)) is None  # nothing flipped
    quotes = {"AAPL": {"price": 100.7, "source": "schwab"}}              # 85% captured
    out = er.maybe_run(st, quotes, now=T0 + timedelta(minutes=4))
    assert out and out["emitted"] == 1
    assert calls[0]["trigger"] == {"kind": "event", "reasons": ["AAPL:extrinsic_captured"]}
    assert remembered["AAPL"]["price"] == 100.7
    # still true on the next print: no second run
    assert er.maybe_run(st, quotes, now=T0 + timedelta(minutes=6)) is None
    assert len(calls) == 1
    assert er.status()["last_run_at"] and er.status()["watching"]["AAPL"] == ["extrinsic_captured"]


def test_disabled_by_env_and_in_demo(monkeypatch):
    import recommendation_runner as runner
    monkeypatch.setattr(runner, "run", lambda **kw: pytest.fail("engine must not run"))
    st = _state([_short(current_bid=1.0)])
    monkeypatch.setenv("CFM_EVENT_RUNS", "0")
    er.maybe_run(st, {"AAPL": {"price": 90.0}}, now=T0)
    assert er.maybe_run(st, {"AAPL": {"price": 100.7}}, now=T0 + timedelta(minutes=2)) is None
    monkeypatch.delenv("CFM_EVENT_RUNS")
    monkeypatch.setattr(config, "demo_enabled", lambda: True)
    assert er.maybe_run(st, {"AAPL": {"price": 100.7}}, now=T0 + timedelta(minutes=4)) is None


def test_engine_failure_never_reaches_the_poller(monkeypatch):
    import recommendation_runner as runner
    monkeypatch.setattr(runner, "run", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    st = _state([_short(sold=2.0, entry_extrinsic=2.0, current_bid=1.0)])
    er.maybe_run(st, {"AAPL": {"price": 90.0}}, now=T0)
    assert er.maybe_run(st, {"AAPL": {"price": 100.7}}, now=T0 + timedelta(minutes=2)) is None


def test_runner_stamps_the_trigger_on_its_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "active_state_path", lambda: str(tmp_path / "state.json"))
    import logging_handler as log
    import reconcile
    import recommendation_runner as runner
    log.save_state(log.load_state())
    monkeypatch.setattr(reconcile, "freeze_status",
                        lambda st: {"frozen": True, "tickers": ["PG"], "reason": "diverges"})
    monkeypatch.setattr(runner, "release_pending", lambda **kw: {})
    trig = {"kind": "event", "reasons": ["PG:roll_75"]}
    assert runner.run(notify=False, trigger=trig)["trigger"] == trig
    assert runner.run(notify=False)["trigger"] == "scheduled"
    assert runner.last_run()["trigger"] == "scheduled"
