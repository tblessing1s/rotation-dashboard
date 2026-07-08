"""Tiered polling runtime: a cycle issues one batched quote for the due Tier 0/1
set, wires defense + market escalation, and gates the kill-switch refresh. Offline
— state, providers and the clock are all injected/mocked."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import config
import data_budget
import data_cache
import data_transport as transport
import market_scheduler as ms
import tier_poll
from market_scheduler import Tier

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 7, 8, 11, 0, tzinfo=ET)   # Wednesday, market open


def _bars(last=100.0):
    closes = list(np.linspace(last + 10, last, 30))
    idx = pd.date_range("2026-01-01", periods=30, freq="D")
    return pd.DataFrame({"Open": closes, "High": [c + 2 for c in closes],
                         "Low": [c - 2 for c in closes], "Close": closes,
                         "Volume": [1e6] * 30}, index=idx)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(data_budget, "PATH", str(tmp_path / "b.json"))
    data_budget.reset(day="2026-07-08")
    data_cache.reset()
    tier_poll.reset()
    monkeypatch.setattr(config, "demo_enabled", lambda: False)
    # Providers "configured"; batch call is faked per test.
    monkeypatch.setattr(transport, "_schwab_configured", lambda: True)
    monkeypatch.setattr(transport, "_av_configured", lambda: False)
    # cached bars for every symbol
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _bars())
    # never actually recompute the kill switch in a unit test
    monkeypatch.setattr(tier_poll, "_maybe_killswitch_refresh", lambda state, now: False)
    yield
    tier_poll.reset()
    data_budget.reset(day="2026-07-08")
    data_cache.reset()


def _state(open_syms=("AAPL",)):
    return {"positions": [{"ticker": s, "status": "active", "sector": "XLK",
                           "short_calls": [{"strike": 90.0}],
                           "circuit_breaker": {"price": 70.0}} for s in open_syms]}


def _wire(monkeypatch, state, tiers, batch):
    monkeypatch.setattr(tier_poll, "_load_state", lambda: state)
    monkeypatch.setattr(tier_poll.queue_state, "build",
                        lambda s=None: (ms.PortfolioState(), ms.QueueState()))
    monkeypatch.setattr(ms, "assign_tiers", lambda ps, qs, now: tiers)
    calls = {"n": 0, "syms": None}

    def fake_batch(syms):
        calls["n"] += 1
        calls["syms"] = list(syms)
        return {s: {"last": batch.get(s, 100.0)} for s in syms}

    monkeypatch.setattr(transport, "_schwab_batch", fake_batch)
    return calls


def test_cycle_one_batch_for_due_t0_t1(monkeypatch):
    state = _state(("AAPL",))
    tiers = {"AAPL": Tier.T0, "MSFT": Tier.T1, "ZZZ": Tier.T2}
    calls = _wire(monkeypatch, state, tiers, {"AAPL": 100.0, "MSFT": 50.0})
    res = tier_poll.run_cycle(OPEN)
    assert calls["n"] == 1                                   # ONE batched request
    # includes due T0 + T1 + market names (SPY, XLK); excludes T2
    assert "AAPL" in calls["syms"] and "MSFT" in calls["syms"]
    assert "ZZZ" not in calls["syms"]
    assert config.BENCHMARK in calls["syms"] and "XLK" in calls["syms"]
    assert set(res["due"]) >= {"AAPL", "MSFT"}


def test_cycle_respects_cadence_second_call_skips(monkeypatch):
    state = _state(("AAPL",))
    tiers = {"AAPL": Tier.T0}
    calls = _wire(monkeypatch, state, tiers, {"AAPL": 100.0})
    tier_poll.run_cycle(OPEN)
    n_after_first = calls["n"]
    # a second cycle 10s later: T0 cadence (120s) not elapsed -> no new quote for AAPL
    tier_poll.run_cycle(OPEN + timedelta(seconds=10))
    # SPY/XLK market names ride T1 (900s) and also aren't due again -> no new batch
    assert calls["n"] == n_after_first


def test_defense_escalation_fires_and_records(monkeypatch):
    # price 85 breaches the short strike (90) -> defense escalation + alert
    state = _state(("AAPL",))
    tiers = {"AAPL": Tier.T0}
    _wire(monkeypatch, state, tiers, {"AAPL": 85.0})
    res = tier_poll.run_cycle(OPEN)
    assert res["escalations"]                                # at least one level breached
    assert tier_poll._tracker.is_escalated("AAPL", OPEN) is True
    # the fresh quote is recorded with provider identity
    assert data_cache.record("AAPL", ms.QUOTE)["provider"] == "schwab"


def test_market_escalation_on_spy_move(monkeypatch):
    state = _state(("AAPL",))
    tiers = {"AAPL": Tier.T0}
    # SPY prints far from its prior close (~100) -> a market escalation
    _wire(monkeypatch, state, tiers, {config.BENCHMARK: 100.0 * (1 + (config.ESCALATION_INDEX_MOVE_PCT + 1) / 100)})
    res = tier_poll.run_cycle(OPEN)
    assert res["market_escalation"] is not None
    assert tier_poll._tracker.market_active(OPEN) is True


def test_offhours_no_quotes(monkeypatch):
    state = _state(("AAPL",))
    tiers = {"AAPL": Tier.T0}
    calls = _wire(monkeypatch, state, tiers, {"AAPL": 100.0})
    closed = datetime(2026, 7, 8, 20, 0, tzinfo=ET)          # after close
    res = tier_poll.run_cycle(closed)
    assert res["market_open"] is False and res["due"] == []
    assert calls["n"] == 0


def test_demo_mode_noop(monkeypatch):
    monkeypatch.setattr(config, "demo_enabled", lambda: True)
    assert tier_poll.run_cycle(OPEN) is None


def test_status_shape():
    s = tier_poll.status(OPEN)
    assert set(s) >= {"escalated_symbols", "market_escalation_active",
                      "killswitch_runs_today", "killswitch_target"}


# ---- scheduler tick wiring -------------------------------------------------

def test_scheduler_wrapper_gates_on_market_hours(monkeypatch):
    import alert_scheduler
    ran = {"n": 0}
    monkeypatch.setattr(tier_poll, "run_cycle", lambda now: ran.__setitem__("n", ran["n"] + 1) or {"due": []})
    monkeypatch.setattr(alert_scheduler, "tier_poll_enabled", lambda: True)
    # market open -> runs
    alert_scheduler._maybe_tier_poll(OPEN)
    assert ran["n"] == 1
    # off-hours -> skipped
    alert_scheduler._maybe_tier_poll(datetime(2026, 7, 8, 20, 0, tzinfo=ET))
    assert ran["n"] == 1
    # disabled -> skipped
    monkeypatch.setattr(alert_scheduler, "tier_poll_enabled", lambda: False)
    alert_scheduler._maybe_tier_poll(OPEN)
    assert ran["n"] == 1


def test_scheduler_wrapper_swallows_errors(monkeypatch):
    import alert_scheduler
    monkeypatch.setattr(alert_scheduler, "tier_poll_enabled", lambda: True)
    monkeypatch.setattr(tier_poll, "run_cycle",
                        lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
    # must not raise — a poll failure can never break the tick
    alert_scheduler._maybe_tier_poll(OPEN)
