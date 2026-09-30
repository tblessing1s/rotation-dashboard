"""Autopilot master switch + the near-strike defend trigger."""
import os
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import autopilot  # noqa: E402
import recommendation_engine as engine  # noqa: E402
import recommendation_runner as runner  # noqa: E402
from rec_types import ActionType, TriggerRule  # noqa: E402
from test_recommendation_engine import _healthy_tk, _market, _position, _state  # noqa: E402

NOW = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)


def test_autopilot_defaults_on_and_toggles(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    assert autopilot.enabled() is True          # never set == pre-existing behaviour
    assert autopilot.set_enabled(False)["enabled"] is False
    assert autopilot.enabled() is False
    assert autopilot.set_enabled(True)["enabled"] is True


def test_runner_checks_do_nothing_while_autopilot_is_off(monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda state=None: False)
    assert runner._check_circuit_breaker_auto_exit(NOW, True) == []
    assert runner._check_roll_defend_auto_execute(NOW, True) == []


def _flat_bars(level):
    from test_recommendation_engine import _frame
    return _frame([level] * 90)


def _near_strike(dte):
    p = _position("AAPL", short_strike=180.0, short_dte=dte)
    tk = _healthy_tk(price=181.0)               # 0.55% above the strike
    tk["bars"] = _flat_bars(170.0)              # above its 50-day MA: no breaker
    return engine.evaluate(_market({"AAPL": tk}), _state([p]), NOW, [])[0]


def test_approaching_strike_rolls_down_in_place_with_runway():
    rec = _near_strike(4)
    assert rec["trigger_rule"] == TriggerRule.DEFEND_APPROACHING_STRIKE
    assert rec["action_type"] == ActionType.DEFEND
    t = rec["proposed_ticket"]
    assert t["roll_reason"] == "defend" and t["roll_direction"] == "ROLL_DOWN"
    sto = [l for l in t["legs"] if l["instruction"] == "SELL_TO_OPEN"][0]
    assert sto["strike"] < 180.0 and sto["dte"] == 4


def test_approaching_strike_rolls_down_and_out_when_the_week_is_nearly_over():
    rec = _near_strike(2)
    assert rec["trigger_rule"] == TriggerRule.DEFEND_APPROACHING_STRIKE
    t = rec["proposed_ticket"]
    assert t["roll_direction"] == "ROLL_DOWN_AND_OUT"
    assert [l for l in t["legs"] if l["instruction"] == "SELL_TO_OPEN"][0]["dte"] == 9


def test_far_from_the_strike_does_not_fire():
    p = _position("AAPL", short_strike=170.0, short_dte=4)
    tk = _healthy_tk(price=182.0)
    tk["bars"] = _flat_bars(170.0)
    recs = engine.evaluate(_market({"AAPL": tk}), _state([p]), NOW, [])
    assert recs[0]["trigger_rule"] != TriggerRule.DEFEND_APPROACHING_STRIKE


def test_new_defend_trigger_is_auto_executable():
    import recommendation_auto_execute as ae
    assert TriggerRule.DEFEND_APPROACHING_STRIKE in ae.AUTO_EXECUTE_TRIGGERS
