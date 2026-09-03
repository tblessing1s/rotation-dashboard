"""Runner integration test — the impure shell end-to-end against a temp store
with mocked providers: snapshot build, engine pass, append, dedup on re-run
(crash-recovery: no duplicate claims within a validity window), dismissal."""
import os
import tempfile

import pandas as pd

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-runner-test-"))

import config  # noqa: E402


def _frame(values):
    idx = pd.bdate_range("2026-03-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c,
                         "Volume": 1e6}, index=idx)


def test_runner_emits_persists_dedups_and_dismisses(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "active_state_path",
                        lambda: str(tmp_path / "state.json"))
    import data_handler
    import dividends
    import earnings
    import logging_handler as log
    import recommendation_runner as runner
    import screening
    import sector_data

    # a laggard position: declining vs flat SPY/sector -> kill switch red
    weak = _frame([200 - i * 0.5 for i in range(90)])
    flat = _frame([100.0] * 90)
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: flat if s.upper() in ("SPY", "XLK") else weak)
    monkeypatch.setattr(data_handler, "live_price", lambda t: 155.0, raising=False)
    monkeypatch.setattr(data_handler, "latest_quote", lambda t: {"price": 155.0})
    monkeypatch.setattr(sector_data, "sector_for", lambda t: "XLK")
    monkeypatch.setattr(screening, "regime", lambda: {"status": "green",
                                                      "published_regime": "green"})
    monkeypatch.setattr(dividends, "q_with_source", lambda t: (0.0, "none"))
    monkeypatch.setattr(earnings, "cached_earnings",
                        lambda t: {"date": None, "warning": False})

    state = log.load_state()
    state["positions"] = [{
        "ticker": "AAPL", "status": "active", "entry_date": "2026-06-01",
        "leap_dte": 170, "planned_exit_dte": 135,
        "leap": {"strike": 130.0, "contracts": 1, "dte": 340,
                 "expiration": "2027-01-15", "current_bid": 3000.0},
        "leap_legs": [{"strike": 130.0, "contracts": 1, "dte": 340,
                       "expiration": "2027-01-15", "current_bid": 3000.0}],
        "short_calls": [{"strike": 150.0, "contracts": 1, "dte": 4,
                         "expiration": "2026-07-17", "current_bid": 6.5,
                         "entry_premium_total": 700.0}],
        "dividend": None,
    }]
    log.save_state(state)

    first = runner.run(notify=False, include_entry=False)
    assert first["emitted"] == 1
    state = log.load_state()
    rec = state["recommendations"][0]
    assert rec["action_type"] == "EXIT"
    # Was KILL_RS_SECTOR — that trigger was removed 2026-08-21
    # (docs/decision-2026-08-21-remove-sector-rs.md), so this fixture's declining
    # bars now bind on the circuit breaker instead. The runner behaviour under
    # test (emit / dedup / dismiss / re-emit) is unchanged.
    assert rec["trigger_rule"] == "CIRCUIT_BREAKER"
    assert rec["rec_id"] == "rec_00001"
    assert state["trust_scoreboard"]["open_actionable"] == 1

    # Re-run (as after a crash/restart): the open record is the claim — no dup.
    second = runner.run(notify=False, include_entry=False)
    assert second["emitted"] == 0
    assert len(log.load_state()["recommendations"]) == 1

    # Dismiss with a coded reason -> resolution derived, nothing mutated.
    log.append_recommendation_override(
        {"rec_id": "rec_00001", "reason": "EXTERNAL_INFO", "note": "spinoff news"})
    state = log.load_state()
    assert state["recommendations"][0]["trigger_rule"] == "CIRCUIT_BREAKER"  # immutable
    res = state["recommendation_resolutions"]
    assert [r for r in res if r["rec_id"] == "rec_00001"
            and r["status"] == "OVERRIDDEN" and r["reason"] == "EXTERNAL_INFO"]
    assert state["trust_scoreboard"]["open_actionable"] == 0

    # The next pass re-emits a FRESH claim (the old one is resolved, the
    # condition still true) rather than resurrecting the dismissed record.
    third = runner.run(notify=False, include_entry=False)
    assert third["emitted"] == 1
    assert log.load_state()["recommendations"][1]["rec_id"] == "rec_00002"


def test_entry_candidates_excludes_known_no_weeklies(monkeypatch):
    """CFM sells a weekly covered call, so a name with no weekly options must never
    become an ENTER candidate. Known-no-weeklies (has_weeklies is False) is dropped;
    unknown (None) is kept — matching the Scorecard's default filter."""
    import recommendation_runner as rr
    from metrics import scorecard as scorecard_metrics
    import account_gate

    rows = [
        {"ticker": "AAA", "suitability": "GO", "has_weeklies": True, "juice_weekly_pct": 1.0},
        {"ticker": "BBB", "suitability": "GO", "has_weeklies": False, "juice_weekly_pct": 2.0},  # excluded
        {"ticker": "CCC", "suitability": "GO", "has_weeklies": None, "juice_weekly_pct": 1.5},   # unknown -> kept
        {"ticker": "DDD", "suitability": "CAUTION", "has_weeklies": True},                       # not GO
    ]
    monkeypatch.setattr(scorecard_metrics, "scorecard", lambda names, price_overrides=None: {"results": rows})
    monkeypatch.setattr(account_gate, "evaluate_many", lambda tickers, contracts=None: {t: {"pass": True} for t in tickers})
    monkeypatch.setattr(rr.data_handler, "get_daily", lambda t, force=False: None)
    monkeypatch.setattr(rr, "_live_price", lambda t: None)
    monkeypatch.setattr(rr, "_ticker_snapshot", lambda *a, **k: {})

    out = rr._entry_candidates({"tickers": {}}, None)
    tickers = {c["ticker"] for c in out}
    assert tickers == {"AAA", "CCC"}   # BBB (no weeklies) and DDD (not GO) excluded


def test_juice_evidence_is_stamped_on_the_snapshot(monkeypatch):
    """The JUICE_HURDLE_FAIL trigger records the juice block from the snapshot as
    its evidence. The yield read a key leap_health does not return
    ("juice_yield_pct" vs "weekly_juice_yield_pct"), so an exit recommendation
    carried a null yield — the trigger firing with its own numbers blank."""
    import leap_policy
    import recommendation_runner as runner
    monkeypatch.setattr(leap_policy, "leap_health", lambda *a, **k: {
        "juice_adequate": False, "weekly_juice_yield_pct": 0.55,
        "juice_target_pct": 0.75, "juice_capital": 18200.0,
        "juice_capital_basis": "spot_x_shares", "maintenance_status": "unknown"})
    tk = runner._ticker_snapshot("AAPL", {"ticker": "AAPL"}, None, 182.0, None, None)
    assert tk["juice"]["inadequate"] is True
    assert tk["juice"]["yield_pct"] == 0.55
    assert tk["juice"]["target_pct"] == 0.75
    assert tk["juice"]["capital_basis"] == "spot_x_shares"


def test_enter_alerts_deep_link_to_the_entry_ticket():
    """An ENTER names a ticker the book does not hold, so the old focus link
    pointed at a position card that will never render."""
    import recommendation_runner as runner
    enter = runner._rec_action_url({"action_type": "ENTER", "rec_id": "rec_1"}, "AVGO")
    assert enter == "/?action=enter&ticker=AVGO&rec_id=rec_1"
    # Everything else still focuses its position card, now carrying the rec id.
    exit_url = runner._rec_action_url({"action_type": "EXIT", "rec_id": "rec_2"}, "MSFT")
    assert exit_url == "/?action=focus&ticker=MSFT&rec_id=rec_2"
    assert runner._rec_action_url({"action_type": "ENTER"}, "") is None
