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


# ---- weekly-extrinsic band (0.8-1.0 %/wk) ------------------------------------
def _chain(spot=100.0, dte=7):
    # Weekly extrinsic % = extrinsic / spot * (7/dte) * 100; with dte=7 it is extrinsic % of spot.
    rows = []
    for strike, mark in [(98.0, 2.9), (97.0, 3.9), (96.0, 4.85), (95.0, 5.7), (94.0, 6.4)]:
        rows.append({"expiration": "2026-07-17", "dte": dte, "strike": strike,
                     "bid": mark - 0.05, "ask": mark + 0.05})
    return rows


def test_band_pick_takes_the_strike_inside_the_band():
    import recommendation_auto_execute as ae
    # extrinsics: 98->0.9, 97->0.9, 96->0.85 (in band), 95->0.7, 94->0.4  (% of spot, dte 7)
    sel = ae.pick_strike_in_juice_band(_chain(), "2026-07-17", 100.0, 98.0)
    assert sel["in_band"] and 0.8 <= sel["juice_per_week_pct"] <= 1.0
    assert sel["strike"] == 96.0                     # 98/97/96 are all in band -> the deepest


def test_band_pick_never_goes_shallower_than_the_regime_strike():
    import recommendation_auto_execute as ae
    sel = ae.pick_strike_in_juice_band(_chain(), "2026-07-17", 100.0, 96.0)
    assert sel["strike"] <= 96.0 and sel["strike"] == 96.0 and sel["in_band"]


def test_band_pick_takes_the_closest_when_nothing_is_in_band():
    import recommendation_auto_execute as ae
    sel = ae.pick_strike_in_juice_band(_chain(), "2026-07-17", 100.0, 95.0)
    assert sel["strike"] == 95.0 and not sel["in_band"]       # 0.7 beats 94's 0.4
    assert sel["distance_from_band"] == 0.1


def test_defend_pick_must_land_below_the_strike_being_closed():
    import recommendation_auto_execute as ae
    sel = ae.pick_strike_in_juice_band(_chain(), "2026-07-17", 100.0, 98.0, below_strike=97.0)
    assert sel["strike"] < 97.0


def test_band_pick_skips_unquoted_strikes_and_returns_none_when_empty():
    import recommendation_auto_execute as ae
    rows = [{**r, "bid": 0.0} for r in _chain()]
    assert ae.pick_strike_in_juice_band(rows, "2026-07-17", 100.0, 98.0) is None


def test_live_drop_through_the_strike_before_the_close_still_defends():
    p = _position("AAPL", short_strike=180.0, short_dte=4)
    tk = _healthy_tk(price=178.0)               # live BELOW the 180 strike ...
    tk["last_close"] = 181.0                    # ... but the last close is still above it
    tk["bars"] = _flat_bars(170.0)
    rec = engine.evaluate(_market({"AAPL": tk}), _state([p]), NOW, [])[0]
    assert rec["trigger_rule"] == TriggerRule.DEFEND_APPROACHING_STRIKE
    assert rec["proposed_ticket"]["legs"][1]["strike"] < 180.0
