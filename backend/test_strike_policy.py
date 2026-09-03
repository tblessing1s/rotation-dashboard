"""strike_policy tests — the regime x posture weekly-short strike table
("Genius System" reference): posture persistence/validation, table lookups
with the unknown-regime fallback, and end-to-end strike composition for a
representative cell in each regime."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
import logging_handler as log  # noqa: E402
import strike_policy  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def test_default_posture_is_conservative(isolated_state):
    assert strike_policy.get_posture() == "conservative"


def test_set_posture_persists_and_validates(isolated_state):
    assert strike_policy.set_posture("Aggressive") == {"posture": "aggressive"}  # case-insensitive
    assert strike_policy.get_posture() == "aggressive"
    # Persisted across a fresh load, not just the in-memory dict.
    assert log.load_state()["metadata"]["strike_posture"] == "aggressive"
    with pytest.raises(ValueError):
        strike_policy.set_posture("yolo")
    assert strike_policy.get_posture() == "aggressive"  # unchanged after the rejected set


def test_table_entry_unknown_regime_falls_back_to_yellow(isolated_state):
    # The numeric lookup falls back to yellow's row, but the `regime` field
    # still echoes what was actually requested (traceability).
    strike_policy.set_posture("conservative")
    yellow = strike_policy.table_entry("yellow")
    for missing in (None, "neon"):
        entry = strike_policy.table_entry(missing)
        assert entry["regime"] == missing
        assert (entry["atr_mult"], entry["itm_pct"]) == (yellow["atr_mult"], yellow["itm_pct"])


def test_table_entry_explicit_posture_overrides_persisted(isolated_state):
    strike_policy.set_posture("conservative")
    entry = strike_policy.table_entry("green", posture="aggressive")
    assert entry == {"regime": "green", "posture": "aggressive", "atr_mult": 0.0, "itm_pct": 0.0}


@pytest.mark.parametrize("regime,posture,atr_mult,itm_pct", [
    ("green", "aggressive", 0.0, 0.00),
    ("green", "conservative", 0.5, 0.01),
    ("yellow", "aggressive", 0.5, 0.02),
    ("yellow", "conservative", 1.0, 0.03),
    ("red", "aggressive", 1.0, 0.04),
    ("red", "conservative", 1.5, 0.05),
])
def test_every_table_cell_matches_the_reference_table(isolated_state, regime, posture, atr_mult, itm_pct):
    entry = strike_policy.table_entry(regime, posture)
    assert entry["atr_mult"] == atr_mult and entry["itm_pct"] == itm_pct


def test_suggest_strike_green_aggressive_is_atm(isolated_state):
    # 0 ATR / 0% ITM -> both candidates equal price -> sell at the money.
    sp = strike_policy.suggest_strike(150.0, 4.0, "green", posture="aggressive")
    assert sp["strike"] == 150.0 and sp["atr_mult"] == 0.0 and sp["itm_pct"] == 0.0


def test_suggest_strike_red_conservative_is_most_protective(isolated_state):
    # 1.5 ATR / 5% ITM: atr_strike=150-6=144; itm_strike=150*0.95=142.5 -> deeper wins.
    sp = strike_policy.suggest_strike(150.0, 4.0, "red", posture="conservative")
    assert sp["strike"] == 142.5
    assert sp["regime"] == "red" and sp["posture"] == "conservative"


def test_roll_options_supports_red_regime(isolated_state, monkeypatch):
    # option_chain.roll_options's OWN target (strike_policy.regime_target_strike,
    # config.STRIKE_ATR_MULT_GREEN/YELLOW + ROLL_REGIME_TARGET_ITM_FLOOR_PCT) is
    # regime-aware and RED-safe: rolling an open short is allowed on a red tape
    # (management, unlike fresh entry, is not blocked) but RED additionally caps
    # the target at the CURRENT strike — no roll-UP is permitted in RED.
    import data_handler
    import option_chain as oc
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "red"})
    df_frame = __import__("pandas").DataFrame(
        {"Open": [150.0] * 60, "High": [151.0] * 60, "Low": [149.0] * 60,
         "Close": [150.0] * 60, "Volume": [1e6] * 60},
        index=__import__("pandas").bdate_range("2024-01-01", periods=60))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df_frame)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 150.0, "source": "t"})
    # Current strike (142.5) sits BELOW where the RED/2.0xATR rule alone would
    # target (145.5 — see below) — the roll-up cap must bite and hold 142.5.
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "short_calls": [{"strike": 142.5, "contracts": 5, "dte": 2, "expiration": "2026-07-03"}]})
    monkeypatch.setattr(oc, "_fetch_chain", lambda t, refresh=False: {
        "status": "SUCCESS", "underlyingPrice": 150.0,
        "callExpDateMap": {"2026-07-10:8": {"142.5": [
            {"symbol": "C", "strikePrice": 142.5, "daysToExpiration": 8,
             "bid": 8.0, "ask": 9.0, "mark": 8.5, "volatility": 30.0}],
            "148.0": [
            {"symbol": "C2", "strikePrice": 148.0, "daysToExpiration": 8,
             "bid": 3.0, "ask": 4.0, "mark": 3.5, "volatility": 30.0}]}},
    })
    out = oc.roll_options("PG")
    # RED uses YELLOW's 2.0xATR (documented HARD_CFM_RULE multiple): ATR is flat
    # (High-Low=2) -> ATR=2 -> atr_strike=150-4=146; itm_strike=150*0.97=145.5 ->
    # deeper (145.5) wins -> then capped at the current strike (142.5) since RED
    # blocks rolling UP.
    assert out["regime"] == "red"
    assert out["atr_mult"] == 2.0 and out["itm_pct"] == 0.03
    assert out["roll_up_blocked"] is True
    assert out["regime_target"]["rule_strike"] == 145.5  # the UNCAPPED rule target
    assert out["regime_target"]["strike"] == 142.5        # capped at current strike
    assert out["suggested_strike"] == 142.5
    assert "posture" not in out


def test_earnings_strike_is_deeper_than_the_regime_cell(isolated_state):
    # A green/aggressive cell is the shallowest (0×ATR, 0% ITM). The earnings
    # protective strike must sit further below spot than the regular suggestion.
    price, atr = 100.0, 2.0
    normal = strike_policy.suggest_strike(price, atr, "green", "aggressive")["strike"]
    earn = strike_policy.suggest_earnings_strike(price, atr, "green", "aggressive")
    assert earn["earnings_protected"] is True
    assert earn["strike"] < normal
    # Applies the deep floors (config.EARNINGS_ROLL_*), never shallower.
    assert earn["atr_mult"] >= config.EARNINGS_ROLL_ATR_MULT
    assert earn["itm_pct"] >= config.EARNINGS_ROLL_ITM_PCT


def test_earnings_strike_never_rolls_shallower_than_a_deep_regime(isolated_state):
    # red/conservative is (1.5×ATR, 5% ITM) — still shallower than the earnings
    # floors here, so the earnings strike takes the deeper earnings values.
    e = strike_policy.suggest_earnings_strike(100.0, 2.0, "red", "conservative")
    assert e["atr_mult"] == max(1.5, config.EARNINGS_ROLL_ATR_MULT)
    assert e["itm_pct"] == max(0.05, config.EARNINGS_ROLL_ITM_PCT)


# ---------------------------------------------------------------------------
# regime_target_strike / apply_deadband — the roll dialog's OWN target-strike
# rule (roll-dialog audit §1.2/§1.8). Pure functions.
# ---------------------------------------------------------------------------

def test_regime_target_green_uses_1_5x_atr():
    out = strike_policy.regime_target_strike(150.0, 4.0, "green")
    # atr_strike = 150-6=144; itm_strike = 150*0.97=145.5 -> deeper (144) wins.
    assert out["atr_mult"] == 1.5 and out["itm_pct"] == config.ROLL_REGIME_TARGET_ITM_FLOOR_PCT
    assert out["strike"] == 144.0
    assert out["roll_up_blocked"] is False


def test_regime_target_yellow_uses_2x_atr():
    out = strike_policy.regime_target_strike(150.0, 4.0, "yellow")
    # atr_strike = 150-8=142; itm_strike=145.5 -> deeper (142) wins.
    assert out["atr_mult"] == 2.0
    assert out["strike"] == 142.0


def test_regime_target_unknown_regime_defaults_to_yellow_not_thinner():
    out = strike_policy.regime_target_strike(150.0, 4.0, None)
    assert out["atr_mult"] == config.STRIKE_ATR_MULT_YELLOW


def test_regime_target_itm_floor_can_win_over_atr_distance():
    # A tiny ATR makes the ATR-distance strike shallower than the 3% ITM floor —
    # the floor must still win (the deeper of the two).
    out = strike_policy.regime_target_strike(150.0, 0.1, "yellow")
    itm_strike = 150.0 * (1 - config.ROLL_REGIME_TARGET_ITM_FLOOR_PCT)
    assert out["strike"] == round(itm_strike * 2) / 2
    assert out["itm_strike"] < out["atr_strike"]


def test_regime_target_red_caps_at_current_strike():
    # Rule alone (2.0xATR) targets 142 (shallower than the current 138 strike) —
    # moving there would be a roll UP, so RED caps it at the current strike.
    # rule_strike still reports the uncapped number for transparency.
    out = strike_policy.regime_target_strike(150.0, 4.0, "red", current_strike=138.0)
    assert out["roll_up_blocked"] is True
    assert out["rule_strike"] == 142.0   # uncapped 2.0xATR rule target
    assert out["strike"] == 138.0        # capped at current strike (no roll-up)


def test_regime_target_red_rolling_down_is_not_capped():
    # Rule target (142) is DEEPER than the current strike (148) — that's a roll
    # DOWN, which RED always permits, so no capping applies even though
    # roll_up_blocked is still True (RED's blanket "no roll-up" stance; this
    # particular move just isn't one).
    out = strike_policy.regime_target_strike(150.0, 4.0, "red", current_strike=148.0)
    assert out["roll_up_blocked"] is True
    assert out["strike"] == out["rule_strike"] == 142.0


def test_regime_target_red_without_current_strike_is_uncapped():
    # No current_strike supplied -> can't cap; roll_up_blocked False (nothing to
    # block against), matching suggest_strike's own None-safety convention.
    out = strike_policy.regime_target_strike(150.0, 4.0, "red")
    assert out["roll_up_blocked"] is False
    assert out["strike"] == out["rule_strike"]


def test_apply_deadband_holds_within_band():
    # prior=145, raw target drifts to 145.2 with ATR=6.5 -> 0.25*6.5=1.625 band;
    # 0.2 move is well inside it -> held.
    out = strike_policy.apply_deadband(145.0, 145.2, 6.5)
    assert out == {"strike": 145.0, "held": True}


def test_apply_deadband_flips_past_the_band():
    out = strike_policy.apply_deadband(145.0, 147.0, 6.5)  # 2.0 > 1.625 band
    assert out["held"] is False
    assert out["strike"] == 147.0  # round(147.0*2)/2


def test_apply_deadband_missing_inputs_never_holds():
    assert strike_policy.apply_deadband(None, 145.2, 6.5) == {"strike": None, "held": False}
    assert strike_policy.apply_deadband(145.0, None, 6.5) == {"strike": None, "held": False}
    assert strike_policy.apply_deadband(145.0, 145.2, None) == {"strike": None, "held": False}
