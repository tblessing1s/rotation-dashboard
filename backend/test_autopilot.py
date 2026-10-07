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


# ---- operator-adjustable parameters ------------------------------------------
import pytest  # noqa: E402

import autopilot_params as ap  # noqa: E402


@pytest.fixture()
def book(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def test_params_default_to_the_config_values(book):
    import config
    p = ap.resolve()
    assert p["extrinsic_capture_pct"] == config.ROLL_EXTRINSIC_CAPTURED_PCT
    assert p["near_strike_band_pct"] == config.SHORT_ATM_APPROACH_PCT
    assert p["cb_drop_pct"] == round(config.CIRCUIT_BREAKER_DROP_PCT * 100, 4)
    assert (p["juice_low_pct"], p["juice_high_pct"]) == (0.8, 1.0)


def test_saved_params_override_and_reset(book):
    ap.set_params({"extrinsic_capture_pct": 85, "cb_ma_fast_closes": 2})
    p = ap.resolve()
    assert p["extrinsic_capture_pct"] == 85 and p["cb_ma_fast_closes"] == 2
    assert ap.reset()["extrinsic_capture_pct"] == 80.0


@pytest.mark.parametrize("bad", [
    {"nope": 1}, {"extrinsic_capture_pct": 10}, {"cb_ma_fast_closes": 2.5},
    {"cb_live_first_level": "yes"}, {"juice_low_pct": 1.5},      # low must stay below high (1.0)
])
def test_bad_params_are_refused_and_nothing_is_saved(book, bad):
    with pytest.raises(ValueError):
        ap.set_params(bad)
    assert ap.resolve() == ap.defaults()


def test_describe_lists_every_parameter_posture_and_fixed_rules(book):
    d = ap.describe()
    assert {p["key"] for p in d["params"]} == {s[0] for s in ap.SPEC}
    assert d["posture"]["value"] in d["posture"]["options"]
    assert d["fixed"] and d["enabled"] is True


def test_engine_honours_a_saved_extrinsic_threshold(book):
    from test_recommendation_engine import _captured_case, _market, _state
    p, tk = _captured_case()                      # 84% of the sold extrinsic is captured
    tk["bars"] = _flat_bars(170.0)                # above its 50-day MA: no breaker

    def rule(saved):
        st = _state([p])
        # a narrow near-strike band keeps the (1.6% above strike) defend trigger out of the way
        st["metadata"] = {"autopilot_params": {"near_strike_band_pct": 0.5, **saved}}
        return engine.evaluate(_market({"AAPL": tk}), st, NOW, [])[0]["trigger_rule"]

    assert rule({}) == TriggerRule.ROLL_EXTRINSIC_CAPTURED          # default 80%
    assert rule({"extrinsic_capture_pct": 90.0}) != TriggerRule.ROLL_EXTRINSIC_CAPTURED


def test_saved_band_and_closes_reach_the_circuit_breaker(book):
    import circuit_breaker as cb
    from test_circuit_breaker import _frame, _pos
    closes = [100.0] * 100 + [90.0, 90.0]                 # only 2 closes below the 50-day MA
    assert not cb.evaluate(_pos(), df=_frame(closes))["tripped"]
    assert cb.evaluate(_pos(), df=_frame(closes), ma_fast_closes=2)["tripped"]


def test_params_route_roundtrip(book, monkeypatch):
    import app as app_module
    c = app_module.create_app().test_client()
    assert c.get("/api/autopilot/params").status_code == 200
    r = c.post("/api/autopilot/params", json={"values": {"near_strike_band_pct": 4}, "posture": "aggressive"})
    body = r.get_json()
    assert r.status_code == 200
    assert next(p for p in body["params"] if p["key"] == "near_strike_band_pct")["value"] == 4
    assert body["posture"]["value"] == "aggressive"
    assert c.post("/api/autopilot/params", json={"values": {"cb_drop_pct": 99}}).status_code == 400


# ---- decision log + diagnosis ("why no trades?") -----------------------------
import autopilot_log as alog  # noqa: E402


def test_decision_log_dedupes_repeats_and_caps(book):
    alog.record("KO", "ROLL_75PCT", "rec_1", "skipped", "not granted")
    alog.record("KO", "ROLL_75PCT", "rec_1", "skipped", "not granted")      # same outcome: not re-logged
    alog.record("KO", "ROLL_75PCT", "rec_1", "placed", "roll working", order_id="9")
    rows = alog.recent()
    assert [r["result"] for r in rows] == ["placed", "skipped"]            # newest first
    for i in range(150):
        alog.record("KO", "X", f"r{i}", "skipped", "x")
    assert len(alog.recent(limit=500)) == 100


def test_diagnose_flags_no_grants_paper_mode_and_a_quiet_book(book, monkeypatch):
    import executor
    monkeypatch.setattr(executor, "live_transmit", lambda: False)
    d = alog.diagnose()
    by = {c["id"]: c for c in d["checks"]}
    assert by["granted"]["ok"] is False and "never acts" in by["granted"]["detail"]
    assert by["mode"]["level"] == "warn" and "PAPER" in by["mode"]["detail"]
    assert by["open"]["level"] == "info" and d["mode"] == "paper"


def test_diagnose_reports_the_reconciliation_freeze(book, monkeypatch):
    import recommendation_runner as rr
    monkeypatch.setattr(rr, "last_run", lambda: {"at": "2026-07-10T14:00:00Z", "reconcile_frozen": True,
                                                  "frozen_tickers": ["KO"], "freeze_reason": "drift"})
    by = {c["id"]: c for c in alog.diagnose()["checks"]}
    assert by["pass"]["ok"] is False and "freeze" in by["pass"]["detail"] and "KO" in by["pass"]["detail"]


def test_runner_logs_why_an_ungranted_roll_was_left_alone(book, monkeypatch):
    import executor
    import recommendation_auto_execute as ae
    monkeypatch.setattr(executor, "live_transmit", lambda: False)
    ae.set_permission(TriggerRule.ROLL_75PCT, True)                          # something is granted ...
    st = _state([_position("AAPL")])
    rec = {"rec_id": "rec_9", "ticker": "AAPL", "action_type": ActionType.ROLL_OUT,
           "trigger_rule": TriggerRule.ROLL_EXTRINSIC_CAPTURED,               # ... but not THIS trigger
           "emitted_at": "2026-07-10T13:00:00Z", "valid_until": "2099-01-01T00:00:00Z",
           "proposed_ticket": None, "input_snapshot": {}}
    import logging_handler as log
    log.mutate_state(lambda s: s.setdefault("recommendations", []).append(rec))
    runner._check_roll_defend_auto_execute(NOW, True)
    row = alog.recent()[0]
    assert row["result"] == "skipped" and "isn't granted" in row["reason"]


def test_diagnostics_route(book):
    import app as app_module
    r = app_module.create_app().test_client().get("/api/autopilot/diagnostics")
    body = r.get_json()
    assert r.status_code == 200 and {"checks", "open", "watch", "decisions"} <= set(body)


# ---- a fully-captured short must not be masked by the assignment-risk flag ----
def _tqqq_like(dte=2):
    from test_recommendation_engine import _frame, _healthy_tk, _market, _shares_position, _state
    # 200 sh / 2 contracts of a 76.5C, stock 83.25, mark == intrinsic: 0 extrinsic, 100% captured.
    p = _shares_position("TQQQ", shares=200, short_strike=76.5, short_dte=dte,
                         current_bid=6.75, contracts=2)
    p["short_calls"][0]["entry_extrinsic_per_share"] = 0.39
    p["circuit_breaker"] = {"price": 60.0, "source": "manual", "entry_price": 80.0}
    tk = _healthy_tk(price=83.25)
    tk["bars"] = _frame([83.25 - (89 - i) * 0.1 for i in range(90)])
    return p, _market({"TQQQ": tk}), _state([p])


def test_zero_extrinsic_short_gets_the_capture_roll_not_the_assignment_flag(book):
    p, market, st = _tqqq_like()
    ev = engine._evaluate_position(p, market, NOW)
    assert {TriggerRule.DIVIDEND_ASSIGNMENT_RISK, TriggerRule.ROLL_EXTRINSIC_CAPTURED} <= set(ev["triggers"])
    rec = engine.evaluate(market, st, NOW, [])[0]
    assert rec["trigger_rule"] == TriggerRule.ROLL_EXTRINSIC_CAPTURED       # autopilot CAN act on this
    assert rec["action_type"] == ActionType.ROLL_OUT
    assert rec["proposed_ticket"]["roll_direction"] == "ROLL_UP_AND_OUT"     # 2 DTE: out to next week


def test_capture_roll_still_ranks_below_defend_and_a_real_dividend_risk():
    cap = {"short": {}, "dte": 2}
    div = {"assignment_risk": {"trigger": "dividend"}}
    ext = {"assignment_risk": {"trigger": "extrinsic"}}
    both = {TriggerRule.DIVIDEND_ASSIGNMENT_RISK: div, TriggerRule.ROLL_EXTRINSIC_CAPTURED: cap}
    assert engine._dominant(both)[0] == TriggerRule.DIVIDEND_ASSIGNMENT_RISK          # ex-div risk wins
    both[TriggerRule.DIVIDEND_ASSIGNMENT_RISK] = ext
    assert engine._dominant(both)[0] == TriggerRule.ROLL_EXTRINSIC_CAPTURED           # extrinsic-only: capture
    both[TriggerRule.DEFEND_BELOW_STRIKE] = {"short": {}}
    assert engine._dominant(both)[0] == TriggerRule.DEFEND_BELOW_STRIKE               # defend still leads


def test_diagnostics_names_the_account(book):
    assert "account" in alog.diagnose()
