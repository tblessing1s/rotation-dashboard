"""roll_advisor.roll_readiness — the advisory-only "clear to roll early" signal
surfaced in the Roll modal. Pure function: no state, no I/O."""
import pytest

import config
import roll_advisor as ra


def test_unmeasured_inputs_give_none_not_false():
    # Neither leg priced -> unmeasured, never a false "not ready".
    out = ra.roll_readiness(None, None, 5)
    assert out["ready"] is None
    assert out["reasons"] == []


def test_below_both_thresholds_is_not_ready():
    out = ra.roll_readiness(40.0, 8.0, 5)
    assert out["ready"] is False
    assert out["reasons"] == []


def test_decay_threshold_alone_triggers_ready():
    out = ra.roll_readiness(config.ROLL_READY_DECAY_PCT, 8.0, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["DECAY_CAPTURED"]


def test_decay_just_under_threshold_does_not_trigger():
    out = ra.roll_readiness(config.ROLL_READY_DECAY_PCT - 0.1, 8.0, 5)
    assert out["ready"] is False
    assert out["reasons"] == []


def test_itm_buffer_threshold_alone_triggers_ready():
    out = ra.roll_readiness(40.0, config.ROLL_READY_ITM_FLOOR_PCT - 0.1, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["ITM_BUFFER_THIN"]


def test_itm_buffer_at_floor_does_not_trigger():
    # Strictly LESS than the floor, not <=.
    out = ra.roll_readiness(40.0, config.ROLL_READY_ITM_FLOOR_PCT, 5)
    assert out["ready"] is False


def test_both_triggers_report_both_reasons():
    out = ra.roll_readiness(95.0, 1.0, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["DECAY_CAPTURED", "ITM_BUFFER_THIN"]


def test_none_itm_buffer_means_not_itm_and_only_decay_can_fire():
    # Caller passes None when the short isn't ITM (an OTM short has no "buffer"
    # in this sense) — must never be treated as "thin buffer" by accident.
    out = ra.roll_readiness(95.0, None, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["DECAY_CAPTURED"]


def test_never_authoritative_shape():
    # No key here could be mistaken for veto/block machinery.
    out = ra.roll_readiness(95.0, 1.0, 5)
    assert out["advisory"] is True
    assert "veto" not in out and "blocking" not in out


# ---------------------------------------------------------------------------
# Phase-1 roll-dialog audit — juice_per_week / cushion_atr / rank_weeks_by_juice
# / roll_up_guard. Pure functions; the frontend does no price arithmetic of its
# own, so these are the ONLY place this math is allowed to live.
# ---------------------------------------------------------------------------

def test_juice_per_week_matches_the_live_case_by_hand():
    # SPCX 2026-09-03 conversation numbers: 145 strike, 8 DTE, spot 151.34,
    # mark 8.32 -> intrinsic 6.34, extrinsic 1.98 -> ~1.144%/wk.
    out = ra.juice_per_week(8.32, 145.0, 151.34, 8)
    assert out == pytest.approx(1.144, abs=0.01)


def test_juice_per_week_none_when_unpriced_or_expired():
    assert ra.juice_per_week(None, 145.0, 151.34, 8) is None
    assert ra.juice_per_week(8.32, 145.0, 151.34, 0) is None
    assert ra.juice_per_week(8.32, 145.0, 151.34, None) is None
    assert ra.juice_per_week(8.32, 145.0, None, 8) is None


def test_juice_per_week_never_negative_denominator_crash_on_otm():
    # Deep OTM (spot < strike): intrinsic floors at 0, extrinsic == mark. Must
    # not raise or silently misprice.
    out = ra.juice_per_week(0.50, 200.0, 151.34, 8)
    assert out == pytest.approx((0.50 / 151.34) * (7 / 8) * 100, rel=1e-6)


def test_cushion_atr_basic():
    assert ra.cushion_atr(145.0, 151.34, 6.51) == pytest.approx((151.34 - 145.0) / 6.51)
    assert ra.cushion_atr(None, 151.34, 6.51) is None
    assert ra.cushion_atr(145.0, None, 6.51) is None
    assert ra.cushion_atr(145.0, 151.34, 0) is None


def test_rank_weeks_by_juice_prefers_shorter_dte_within_parity_band():
    # 8 DTE at ~1.144%/wk vs 15 DTE at ~1.135%/wk — within the default 0.05%/wk
    # band, so the SHORTER DTE (8) must win despite not having the raw max.
    rows = [
        {"expiration": "2026-09-11", "dte": 8, "juice_per_week_pct": 1.144},
        {"expiration": "2026-09-18", "dte": 15, "juice_per_week_pct": 1.135},
        {"expiration": "2026-10-16", "dte": 43, "juice_per_week_pct": 0.90},
    ]
    out = ra.rank_weeks_by_juice(rows)
    assert out["best"]["expiration"] == "2026-09-11"


def test_rank_weeks_by_juice_picks_the_real_max_outside_the_band():
    rows = [
        {"expiration": "A", "dte": 8, "juice_per_week_pct": 1.0},
        {"expiration": "B", "dte": 15, "juice_per_week_pct": 1.5},  # clearly better, not a tie
    ]
    out = ra.rank_weeks_by_juice(rows, parity_band_pct=0.05)
    assert out["best"]["expiration"] == "B"


def test_rank_weeks_by_juice_ignores_unpriced_rows():
    rows = [
        {"expiration": "A", "dte": 8, "juice_per_week_pct": None},
        {"expiration": "B", "dte": 15, "juice_per_week_pct": 1.0},
    ]
    out = ra.rank_weeks_by_juice(rows)
    assert out["best"]["expiration"] == "B"


def test_rank_weeks_by_juice_all_unpriced_returns_no_best():
    rows = [{"expiration": "A", "dte": 8, "juice_per_week_pct": None}]
    out = ra.rank_weeks_by_juice(rows)
    assert out["best"] is None
    assert out["rows"] == rows


def test_roll_up_guard_none_when_not_rolling_up():
    assert ra.roll_up_guard(
        current_strike=133, chosen_strike=133, earnings_in_week=False,
        ex_div_known=True, ex_div_in_week=False, chosen_juice_per_week_pct=1.0,
        juice_floor_pct=0.75, operating_cash=20000, reserve_required=13000,
        net_credit=-500) is None
    assert ra.roll_up_guard(
        current_strike=133, chosen_strike=130, earnings_in_week=False,
        ex_div_known=True, ex_div_in_week=False, chosen_juice_per_week_pct=1.0,
        juice_floor_pct=0.75, operating_cash=20000, reserve_required=13000,
        net_credit=-500) is None


def test_roll_up_guard_all_pass():
    out = ra.roll_up_guard(
        current_strike=133, chosen_strike=145, earnings_in_week=False,
        ex_div_known=True, ex_div_in_week=False, chosen_juice_per_week_pct=1.5,
        juice_floor_pct=0.75, operating_cash=20000, reserve_required=13000,
        net_credit=-1023)
    assert out["summary"] == "PASS"
    assert all(c["status"] == "PASS" for c in out["checks"])
    assert out["advisory"] is True


def test_roll_up_guard_earnings_fails():
    out = ra.roll_up_guard(
        current_strike=133, chosen_strike=145, earnings_in_week=True,
        ex_div_known=True, ex_div_in_week=False, chosen_juice_per_week_pct=1.5,
        juice_floor_pct=0.75, operating_cash=20000, reserve_required=13000,
        net_credit=-1023)
    assert out["summary"] == "FAIL"
    assert {c["id"]: c["status"] for c in out["checks"]}["earnings"] == "FAIL"


def test_roll_up_guard_unknown_ex_div_is_unknown_not_pass():
    out = ra.roll_up_guard(
        current_strike=133, chosen_strike=145, earnings_in_week=False,
        ex_div_known=False, ex_div_in_week=None, chosen_juice_per_week_pct=1.5,
        juice_floor_pct=0.75, operating_cash=20000, reserve_required=13000,
        net_credit=-1023)
    assert {c["id"]: c["status"] for c in out["checks"]}["ex_div"] == "UNKNOWN"
    # UNKNOWN alone (no FAIL) -> summary is UNKNOWN, not PASS.
    assert out["summary"] == "UNKNOWN"


def test_roll_up_guard_cash_reserve_breach_fails():
    # 20000 operating cash, -8000 net debit -> 12000 post-roll, below the 13000
    # reserve.
    out = ra.roll_up_guard(
        current_strike=133, chosen_strike=145, earnings_in_week=False,
        ex_div_known=True, ex_div_in_week=False, chosen_juice_per_week_pct=1.5,
        juice_floor_pct=0.75, operating_cash=20000, reserve_required=13000,
        net_credit=-8000)
    assert {c["id"]: c["status"] for c in out["checks"]}["cash_reserve"] == "FAIL"
    assert out["summary"] == "FAIL"


def test_roll_up_guard_worst_signal_wins_fail_over_unknown():
    out = ra.roll_up_guard(
        current_strike=133, chosen_strike=145, earnings_in_week=True,
        ex_div_known=False, ex_div_in_week=None, chosen_juice_per_week_pct=None,
        juice_floor_pct=0.75, operating_cash=None, reserve_required=13000,
        net_credit=None)
    assert out["summary"] == "FAIL"
