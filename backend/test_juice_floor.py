"""Shares-base juice adequacy floor — SHADOW/ENFORCE mode (§4.6).

The adequacy floor moves off the LEAP-cost denominator onto SHARE NOTIONAL and is
staged in SHADOW by default: it is evaluated and LOGGED as a calibration datapoint
but does NOT block entry and does NOT touch SCORE. ENFORCE restores the safety
block. The HARD floor (net juice <= 0) is a separate HARD_CFM_RULE and blocks in
BOTH modes.

Run: python -m pytest backend/test_juice_floor.py -q
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import account_gate  # noqa: E402
import config  # noqa: E402
import juice_floor  # noqa: E402
import position_types  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    return tmp_path


# ---- Share-notional denominator ---------------------------------------------
def test_shares_weekly_juice_pct_uses_share_notional():
    # A delta-1.0 base: notional per share == spot, so achieved % = extrinsic / spot.
    assert juice_floor.shares_weekly_juice_pct(0.50, 100.0) == 0.5
    assert juice_floor.shares_weekly_juice_pct(2.05, 100.0) == 2.05
    assert juice_floor.shares_weekly_juice_pct(None, 100.0) is None
    assert juice_floor.shares_weekly_juice_pct(0.50, 0) is None
    assert juice_floor.shares_weekly_juice_pct(0.50, None) is None


def test_default_mode_is_shadow():
    # The migration ships SHADOW so the number is set from real data, not a guess.
    assert config.JUICE_FLOOR_MODE == "SHADOW"


# ---- Hard floor: net <= 0 blocks in BOTH modes (spec test 7) -----------------
def test_hard_floor_blocks_in_both_modes():
    for mode in ("SHADOW", "ENFORCE"):
        ev = juice_floor.evaluate("XYZ", weekly_extrinsic_per_share=0.20, spot=100.0,
                                  net_juice_weekly_pct=-0.10, mode=mode)
        assert ev["hard_block"] is True
        assert ev["tier"] == "hard"
        assert ev["blocks"] is True, mode


# ---- Adequacy floor: shadowed (spec test 5) vs enforced (spec test 6) --------
def test_adequacy_shadow_does_not_block():
    # achieved 0.30% < floor 1.5%, but net juice positive -> SHADOW logs, never blocks.
    ev = juice_floor.evaluate("THIN", weekly_extrinsic_per_share=0.30, spot=100.0,
                              net_juice_weekly_pct=0.30, floor_pct=1.5, mode="SHADOW")
    assert ev["adequacy_fail"] is True
    assert ev["tier"] == "adequacy"
    assert ev["blocks"] is False


def test_adequacy_enforce_blocks():
    ev = juice_floor.evaluate("THIN", weekly_extrinsic_per_share=0.30, spot=100.0,
                              net_juice_weekly_pct=0.30, floor_pct=1.5, mode="ENFORCE")
    assert ev["adequacy_fail"] is True
    assert ev["tier"] == "adequacy"
    assert ev["blocks"] is True


def test_adequate_juice_passes_both_modes():
    for mode in ("SHADOW", "ENFORCE"):
        ev = juice_floor.evaluate("RICH", weekly_extrinsic_per_share=2.00, spot=100.0,
                                  net_juice_weekly_pct=2.00, floor_pct=1.5, mode=mode)
        assert ev["adequacy_fail"] is False
        assert ev["adequacy_pass"] is True
        assert ev["tier"] is None
        assert ev["blocks"] is False, mode


# ---- The evaluation is a persisted calibration datapoint ---------------------
def test_evaluation_is_logged_and_reads_back(store):
    ev = juice_floor.evaluate("LOGME", weekly_extrinsic_per_share=0.30, spot=100.0,
                              net_juice_weekly_pct=0.30, iv=0.35, ivr=42.0,
                              atr_depth_mult=1.5, floor_pct=1.5, mode="SHADOW")
    juice_floor.record(ev)
    series = juice_floor.series("LOGME")
    assert len(series) == 1
    r = series[0]
    assert r["symbol"] == "LOGME"
    assert r["achieved_pct"] == 0.3 and r["floor_pct"] == 1.5
    assert r["iv"] == 0.35 and r["ivr"] == 42.0 and r["atr_depth_mult"] == 1.5
    assert r["blocks"] is False and r["mode"] == "SHADOW"
    assert "date" in r


def test_log_is_capped_per_symbol(store, monkeypatch):
    monkeypatch.setattr(config, "JUICE_FLOOR_LOG_MAX", 3)
    for i in range(6):
        juice_floor.record(juice_floor.evaluate(
            "CAP", weekly_extrinsic_per_share=0.30 + i * 0.01, spot=100.0,
            net_juice_weekly_pct=0.30, mode="SHADOW"))
    assert len(juice_floor.series("CAP")) == 3  # newest 3 retained


def test_record_never_raises_on_bad_input(store):
    # Telemetry must never sink its caller.
    assert juice_floor.record(None)["ok"] is False
    assert juice_floor.record({"symbol": "OK", "achieved_pct": 1.0})["ok"] is True


# ---- Integration: account_gate SHARES entry ---------------------------------
def _est(spot, extr):
    return {"ticker": "X", "stock_price": spot, "weekly_extrinsic_per_share": extr,
            "leap_strike": spot * 0.9, "leap_cost_per_share": spot * 0.5,
            "weekly_yield_pct": 2.0, "source": "estimate"}


def test_account_gate_shares_floor_advisory_in_shadow(store, monkeypatch):
    # 0.30 extrinsic on a $100 share = 0.30%/wk < 1.5% floor. SHADOW -> a check is
    # present, marked failing, but NON-blocking (not in blocking_failures).
    monkeypatch.setattr(config, "JUICE_FLOOR_MODE", "SHADOW")
    monkeypatch.setattr(account_gate, "juice_estimate", lambda t, df=None: _est(100.0, 0.30))
    g = account_gate.evaluate("THIN", contracts=1, leap_cost_per_share=50.0,
                              weekly_extrinsic_per_share=0.30,
                              position_type=position_types.SHARES)
    chk = next(c for c in g["checks"] if c["id"] == "shares_juice_floor")
    assert chk["pass"] is False and chk["blocking"] is False
    assert chk["detail"]["mode"] == "SHADOW"
    assert chk["detail"]["achieved_pct"] == 0.3
    assert "shares_juice_floor" not in g["blocking_failures"]
    assert "shares_juice_floor" in g["warnings"]


def test_account_gate_shares_floor_blocks_in_enforce(store, monkeypatch):
    monkeypatch.setattr(config, "JUICE_FLOOR_MODE", "ENFORCE")
    monkeypatch.setattr(account_gate, "juice_estimate", lambda t, df=None: _est(100.0, 0.30))
    g = account_gate.evaluate("THIN", contracts=1, leap_cost_per_share=50.0,
                              weekly_extrinsic_per_share=0.30,
                              position_type=position_types.SHARES)
    chk = next(c for c in g["checks"] if c["id"] == "shares_juice_floor")
    assert chk["pass"] is False and chk["blocking"] is True
    assert "shares_juice_floor" in g["blocking_failures"]


def test_account_gate_legacy_has_no_shares_floor_check(store, monkeypatch):
    monkeypatch.setattr(account_gate, "juice_estimate", lambda t, df=None: _est(100.0, 0.30))
    g = account_gate.evaluate("THIN", contracts=1, leap_cost_per_share=50.0,
                              weekly_extrinsic_per_share=0.30)  # default -> legacy
    assert not any(c["id"] == "shares_juice_floor" for c in g["checks"])
