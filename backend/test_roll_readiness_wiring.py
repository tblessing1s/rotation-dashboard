"""option_chain.roll_options wiring for roll_advisor.roll_readiness: the roll
picker attaches an advisory-only readiness signal built from the SAME
position_manager.enrich_short math the position tile uses, without disturbing
any of the picker's existing fields (strike table, suggested_strike, etc)."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-roll-readiness-test-"))

import config            # noqa: E402
import data_handler       # noqa: E402
import logging_handler as log  # noqa: E402
import option_chain as oc  # noqa: E402
import screening          # noqa: E402
import strike_policy      # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    strike_policy.set_posture("conservative")
    return tmp_path


def _flat_df():
    pd = __import__("pandas")
    return pd.DataFrame(
        {"Open": [150.0] * 60, "High": [151.0] * 60, "Low": [149.0] * 60,
         "Close": [150.0] * 60, "Volume": [1e6] * 60},
        index=pd.bdate_range("2024-01-01", periods=60))


def _mock_common(monkeypatch, short_call, chain_mark):
    monkeypatch.setattr(screening, "regime", lambda: {"status": "green"})
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _flat_df())
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 150.0, "source": "t"})
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "position_type": "SHARES", "short_calls": [short_call]})
    monkeypatch.setattr(oc, "_fetch_chain", lambda t, refresh=False: {
        "status": "SUCCESS", "underlyingPrice": 150.0,
        "callExpDateMap": {"2026-07-10:8": {str(short_call["strike"]): [
            {"symbol": "C", "strikePrice": short_call["strike"], "daysToExpiration": 8,
             "bid": chain_mark - 0.1, "ask": chain_mark + 0.1, "mark": chain_mark,
             "volatility": 30.0}]}},
    })


def test_mostly_decayed_short_is_flagged_ready(isolated_state, monkeypatch):
    # Sold $1.00/sh of extrinsic; only $0.15/sh left (intrinsic is 150-133=17,
    # mark 17.15) -> 85% captured, clears the 80% ROLL_READY_DECAY_PCT floor.
    # 11.3% ITM buffer stays well above the 3% floor, so decay is the ONLY
    # reason firing.
    short_call = {"strike": 133, "contracts": 1, "dte": 8, "expiration": "2026-07-10",
                  "entry_premium_total": 500.0, "entry_extrinsic_per_share": 1.00}
    _mock_common(monkeypatch, short_call, chain_mark=17.15)

    out = oc.roll_options("PG")
    rr = out["roll_readiness"]
    assert rr["ready"] is True
    assert rr["reasons"] == ["DECAY_CAPTURED"]
    assert rr["extrinsic_captured_pct"] == pytest.approx(85.0, abs=0.5)
    assert rr["itm_buffer_pct"] == pytest.approx(11.33, abs=0.1)
    assert rr["decay_threshold_pct"] == config.ROLL_READY_DECAY_PCT
    assert rr["itm_floor_pct"] == config.ROLL_READY_ITM_FLOOR_PCT
    assert rr["advisory"] is True


def test_fresh_short_with_room_is_not_flagged(isolated_state, monkeypatch):
    # Sold $1.00/sh, still worth $0.90/sh (intrinsic 17, mark 17.90) -> only
    # 10% captured; buffer is the same wide 11.3% -> neither trigger fires.
    short_call = {"strike": 133, "contracts": 1, "dte": 8, "expiration": "2026-07-10",
                  "entry_premium_total": 500.0, "entry_extrinsic_per_share": 1.00}
    _mock_common(monkeypatch, short_call, chain_mark=17.90)

    out = oc.roll_options("PG")
    rr = out["roll_readiness"]
    assert rr["ready"] is False
    assert rr["reasons"] == []


def test_thin_buffer_alone_flags_ready_even_with_fresh_decay(isolated_state, monkeypatch):
    # A strike deep enough ITM that the cushion itself is the problem, even
    # though barely any extrinsic has decayed yet.
    short_call = {"strike": 146, "contracts": 1, "dte": 8, "expiration": "2026-07-10",
                  "entry_premium_total": 500.0, "entry_extrinsic_per_share": 1.00}
    # intrinsic = 150-146 = 4 -> buffer_pct = 4/150*100 = 2.67% (< 3% floor).
    _mock_common(monkeypatch, short_call, chain_mark=4.95)

    out = oc.roll_options("PG")
    rr = out["roll_readiness"]
    assert rr["ready"] is True
    assert rr["reasons"] == ["ITM_BUFFER_THIN"]
    assert rr["itm_buffer_pct"] == pytest.approx(2.67, abs=0.05)


def test_readiness_is_purely_additive(isolated_state, monkeypatch):
    """Attaching roll_readiness must not perturb any existing roll_options field."""
    short_call = {"strike": 133, "contracts": 1, "dte": 8, "expiration": "2026-07-10",
                  "entry_premium_total": 500.0, "entry_extrinsic_per_share": 1.00}
    _mock_common(monkeypatch, short_call, chain_mark=17.15)

    out = oc.roll_options("PG")
    assert set(out) - {"roll_readiness"} == {
        "ticker", "underlying_price", "regime", "atr", "atr_mult", "itm_pct",
        "posture", "suggested_strike", "earnings_date", "iv_rank",
        "current_short", "expirations"}
    assert out["suggested_strike"] is not None
    assert out["current_short"]["strike"] == 133
    assert out["current_short"]["current_mark"] == pytest.approx(17.15)
