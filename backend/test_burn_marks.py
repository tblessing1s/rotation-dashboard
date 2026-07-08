"""Weekly burn-mark telemetry + realized-vs-projected divergence.

Offline. Sets a temp DATA_DIR so the on-disk store is isolated (mirrors
test_iv_history). Run: python -m pytest backend -q
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cfm-burnmark-test-")

import burn          # noqa: E402
import burn_marks    # noqa: E402
import config        # noqa: E402


def _clear():
    try:
        os.remove(burn_marks.BURN_MARKS_PATH)
    except OSError:
        pass


def _proj(extrinsic_now, burn_pw, *, exit_dte=135, low=False):
    """A minimal burn_projection-shaped dict for controlled mark tests."""
    return {"priceable": True, "extrinsic_now": extrinsic_now,
            "projected_burn_per_week": burn_pw,
            "burn_per_week_with_slippage": burn_pw + 2.0,
            "planned_exit_dte": exit_dte, "low_extrinsic_flag": low}


def setup_function(_):
    _clear()


# ---------------------------------------------------------------------------
# Mark recording + realized-burn sign convention
# ---------------------------------------------------------------------------
def test_realized_burn_is_prior_minus_current_per_week():
    burn_marks.record_mark("NVDA", _proj(300.0, 12.0), spot=100, iv=30,
                           current_dte=160, day="2026-01-02")
    m2 = burn_marks.record_mark("NVDA", _proj(280.0, 12.5), spot=99, iv=30,
                                current_dte=153, day="2026-01-09")  # +7 days
    assert m2["realized_burn_week"] == 20.0        # (300 - 280) / 1 week
    assert m2["projected_last_week"] == 12.0       # the prior mark's projection


def test_realized_burn_negative_when_extrinsic_grows_on_iv_spike():
    """IV spikes between marks so extrinsic GREW — realized burn is negative,
    recorded as-is (information, not an error)."""
    burn_marks.record_mark("NVDA", _proj(250.0, 12.0), spot=100, iv=25,
                           current_dte=160, day="2026-01-02")
    m2 = burn_marks.record_mark("NVDA", _proj(300.0, 22.0), spot=100, iv=55,
                                current_dte=153, day="2026-01-09")
    assert m2["realized_burn_week"] == -50.0
    assert m2["realized_burn_week"] < 0


def test_same_day_rerun_overwrites_and_keeps_prior_as_base():
    burn_marks.record_mark("NVDA", _proj(300.0, 12.0), spot=100, iv=30,
                           current_dte=160, day="2026-01-02")
    burn_marks.record_mark("NVDA", _proj(280.0, 12.0), spot=99, iv=30,
                           current_dte=153, day="2026-01-09")
    # A same-day re-run replaces the latest mark; realized still keys off Jan 2.
    m = burn_marks.record_mark("NVDA", _proj(275.0, 13.0), spot=98, iv=31,
                               current_dte=153, day="2026-01-09")
    assert len(burn_marks.series("NVDA")) == 2
    assert m["realized_burn_week"] == 25.0  # (300 - 275) / 1 week, vs Jan 2 base


def test_unpriceable_projection_is_not_recorded():
    assert burn_marks.record_mark("NVDA", {"priceable": False}, spot=None,
                                  iv=None, day="2026-01-02") is None
    assert burn_marks.series("NVDA") == []


# ---------------------------------------------------------------------------
# Case 10 — divergence beyond / below the warn threshold
# ---------------------------------------------------------------------------
def test_divergence_warns_beyond_threshold():
    # Projected 12/wk, realized 20/wk => +66.7% divergence > 25% warn threshold.
    burn_marks.record_mark("NVDA", _proj(300.0, 12.0), spot=100, iv=30,
                           current_dte=160, day="2026-01-02")
    burn_marks.record_mark("NVDA", _proj(280.0, 12.0), spot=99, iv=30,
                           current_dte=153, day="2026-01-09")
    d = burn_marks.divergence("NVDA")
    assert d["sample"] == 1
    assert d["weeks"][0]["divergence_pct"] > config.BURN_DIVERGENCE_WARN_PCT
    assert d["warn"] is True


def test_divergence_quiet_below_threshold():
    # Projected 12/wk, realized 13/wk => +8.3% divergence < 25% => no warn.
    burn_marks.record_mark("NVDA", _proj(300.0, 12.0), spot=100, iv=30,
                           current_dte=160, day="2026-01-02")
    burn_marks.record_mark("NVDA", _proj(287.0, 12.0), spot=99, iv=30,
                           current_dte=153, day="2026-01-09")
    d = burn_marks.divergence("NVDA")
    assert abs(d["weeks"][0]["divergence_pct"]) < config.BURN_DIVERGENCE_WARN_PCT
    assert d["warn"] is False


def test_aggregate_divergence_pools_across_tickers():
    burn_marks.record_mark("NVDA", _proj(300.0, 12.0), spot=100, iv=30,
                           current_dte=160, day="2026-01-02")
    burn_marks.record_mark("NVDA", _proj(280.0, 12.0), spot=99, iv=30,
                           current_dte=153, day="2026-01-09")
    burn_marks.record_mark("XLK", _proj(200.0, 10.0), spot=235, iv=22,
                           current_dte=160, day="2026-01-02")
    burn_marks.record_mark("XLK", _proj(189.0, 10.0), spot=234, iv=22,
                           current_dte=153, day="2026-01-09")
    agg = burn_marks.aggregate_divergence()
    assert set(agg["per_ticker"]) == {"NVDA", "XLK"}
    assert agg["sample"] == 2
    assert agg["mean_abs_divergence_pct"] is not None


def test_divergence_ignores_near_zero_projection():
    # A near-zero projection (low-extrinsic week) is not comparable — no divergence.
    burn_marks.record_mark("NVDA", _proj(0.5, 0.0, low=True), spot=300, iv=20,
                           current_dte=160, day="2026-01-02")
    burn_marks.record_mark("NVDA", _proj(0.4, 0.0, low=True), spot=300, iv=20,
                           current_dte=153, day="2026-01-09")
    d = burn_marks.divergence("NVDA")
    assert d["sample"] == 0
    assert d["warn"] is False


# ---------------------------------------------------------------------------
# Weekly cadence gate
# ---------------------------------------------------------------------------
def test_weekly_due_only_end_of_week_and_once_per_iso_week():
    # 2026-01-05 is a Monday .. 2026-01-09 a Friday.
    assert burn_marks.weekly_due("2026-01-05") is False   # Mon
    assert burn_marks.weekly_due("2026-01-08") is False   # Thu
    assert burn_marks.weekly_due("2026-01-09") is True    # Fri, nothing marked yet
    # After a Friday mark, the rest of that ISO week is not due again.
    burn_marks.record_mark("NVDA", _proj(300.0, 12.0), spot=100, iv=30,
                           current_dte=160, day="2026-01-09")
    assert burn_marks.weekly_due("2026-01-10") is False   # Sat, already marked
    assert burn_marks.weekly_due("2026-01-11") is False   # Sun, already marked


def test_real_projection_feeds_a_mark_end_to_end():
    proj = burn.burn_projection({"strike": 79, "contracts": 1}, 100, 30, 160, 135)
    m = burn_marks.record_mark("NVDA", proj, spot=100, iv=30, current_dte=160,
                               day="2026-01-09")
    assert m is not None
    assert m["extrinsic_now"] == proj["extrinsic_now"]
    assert m["projected_burn_per_week"] == proj["projected_burn_per_week"]
