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
    # Previously RED was absent from option_chain's regime->ATR-mult map and
    # silently fell back to YELLOW's multiplier even when rolling during a red
    # tape (management is explicitly allowed on RED). The table now has a real
    # RED row, so roll_options must use it rather than falling back.
    import data_handler
    import option_chain as oc
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "red"})
    strike_policy.set_posture("conservative")
    df_frame = __import__("pandas").DataFrame(
        {"Open": [150.0] * 60, "High": [151.0] * 60, "Low": [149.0] * 60,
         "Close": [150.0] * 60, "Volume": [1e6] * 60},
        index=__import__("pandas").bdate_range("2024-01-01", periods=60))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df_frame)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 150.0, "source": "t"})
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "short_calls": [{"strike": 148, "contracts": 5, "dte": 2, "expiration": "2026-07-03"}]})
    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {
        "status": "SUCCESS", "underlyingPrice": 150.0,
        "callExpDateMap": {"2026-07-10:8": {"142.5": [
            {"symbol": "C", "strikePrice": 142.5, "daysToExpiration": 8,
             "bid": 8.0, "ask": 9.0, "mark": 8.5, "volatility": 30.0}],
            "148.0": [
            {"symbol": "C2", "strikePrice": 148.0, "daysToExpiration": 8,
             "bid": 3.0, "ask": 4.0, "mark": 3.5, "volatility": 30.0}]}},
    })
    out = oc.roll_options("PG")
    # RED/conservative = 1.5 ATR / 5% ITM: ATR is flat (High-Low=2) -> ATR=2 ->
    # atr_strike=150-3=147; itm_strike=150*0.95=142.5 -> deeper (142.5) wins.
    assert out["regime"] == "red"
    assert out["atr_mult"] == 1.5 and out["itm_pct"] == 0.05 and out["posture"] == "conservative"
    assert out["suggested_strike"] == 142.5
