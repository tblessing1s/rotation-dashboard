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
    assert rec["trigger_rule"] == "KILL_RS_SECTOR"
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
    assert state["recommendations"][0]["trigger_rule"] == "KILL_RS_SECTOR"  # immutable
    res = state["recommendation_resolutions"]
    assert [r for r in res if r["rec_id"] == "rec_00001"
            and r["status"] == "OVERRIDDEN" and r["reason"] == "EXTERNAL_INFO"]
    assert state["trust_scoreboard"]["open_actionable"] == 0

    # The next pass re-emits a FRESH claim (the old one is resolved, the
    # condition still true) rather than resurrecting the dismissed record.
    third = runner.run(notify=False, include_entry=False)
    assert third["emitted"] == 1
    assert log.load_state()["recommendations"][1]["rec_id"] == "rec_00002"
