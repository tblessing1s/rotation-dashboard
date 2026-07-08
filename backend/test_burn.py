"""Weekly theta burn & net-juice — pure-function tests.

Offline, no provider keys, no clock reads. Run with: python -m pytest backend -q

Fixtures use the app's real Black-Scholes engine (indicators._bs_call_price) via
burn.py — no second pricer, no mocked prices except where a chain spread is
injected for the slippage case.

Note on the theta curve (see IMPLEMENTATION_NOTES): a deep-ITM 0.90-delta LEAP
decays its extrinsic FASTER early, not later, so the correct invariant is that
held-window burn is a small fraction of TOTAL entry extrinsic (~1/3, the real
value prop), NOT that it is below a straight-line proration.
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import burn          # noqa: E402
import config        # noqa: E402
import indicators    # noqa: E402

# A realistic deep-ITM ~0.90-delta LEAP: spot 100, strike 79, IV 30%.
LEAP = {"strike": 79, "contracts": 1}


def _model_ext_ps(S, K, dte, iv, q=0.0):
    T = dte / 365.0
    return max(indicators._bs_call_price(S, K, T, config.RISK_FREE_RATE, iv / 100.0, q)
               - max(S - K, 0.0), 0.0)


# ---------------------------------------------------------------------------
# Case 1 — two-point model identity + the true "~1/3 of total extrinsic" rule
# ---------------------------------------------------------------------------
def test_burn_is_two_point_model_difference():
    p = burn.burn_projection(LEAP, 100, 30, 195, 135)
    assert p["priceable"]
    # Burn total == extrinsic_now - extrinsic_at_exit, EXACTLY (both model prices).
    assert p["projected_burn_total"] == round(p["extrinsic_now"] - p["extrinsic_at_exit"], 2)
    # And equals the independent two-point model computation.
    ext_now = _model_ext_ps(100, 79, 195, 30) * 100
    ext_exit = _model_ext_ps(100, 79, 135, 30) * 100
    assert p["extrinsic_now"] == round(ext_now, 2)
    assert p["extrinsic_at_exit"] == round(ext_exit, 2)
    assert p["projected_burn_total"] == round(ext_now - ext_exit, 2)


def test_held_window_burn_is_roughly_one_third_of_total_extrinsic():
    """HARD_CFM_RULE (spec point #1): the true cost is the extrinsic consumed in
    the held window, ~1/3 of the total entry extrinsic the old accounting used as
    the hurdle. Materially below the total, never equal to it."""
    p = burn.burn_projection(LEAP, 100, 30, 195, 135)
    total_entry_extrinsic = p["extrinsic_now"]
    held_burn = p["projected_burn_total"]
    assert held_burn < total_entry_extrinsic * 0.5  # materially below the hurdle
    # And in the ballpark of one-third for this entry/exit band.
    assert 0.25 < held_burn / total_entry_extrinsic < 0.45


def test_burn_never_uses_straight_line_proration():
    """The model difference is not the straight-line figure — proving we did not
    prorate. (They differ; direction is curve-dependent, so we assert inequality
    of VALUES, which is what 'never straight-line' means operationally.)"""
    p = burn.burn_projection(LEAP, 100, 30, 195, 135)
    straight_total = p["extrinsic_now"] * ((195 - 135) / 195.0)
    assert abs(p["projected_burn_total"] - straight_total) > 1e-6


def test_weeks_remaining_and_per_week():
    p = burn.burn_projection(LEAP, 100, 30, 195, 135)
    assert p["weeks_remaining"] == round((195 - 135) / 7.0, 2)
    assert p["projected_burn_per_week"] == round(p["projected_burn_total"] / p["weeks_remaining"], 2)


# ---------------------------------------------------------------------------
# Case 2 — IV sensitivity
# ---------------------------------------------------------------------------
def test_higher_iv_raises_projected_burn():
    lo = burn.burn_projection(LEAP, 100, 25, 195, 135)
    hi = burn.burn_projection(LEAP, 100, 45, 195, 135)
    assert hi["projected_burn_per_week"] > lo["projected_burn_per_week"]
    assert hi["extrinsic_now"] > lo["extrinsic_now"]


def test_realized_burn_negative_when_extrinsic_grows_on_iv_spike():
    """Realized burn between two marks = prev_extrinsic - current_extrinsic. If IV
    spikes so extrinsic GREW, realized burn is negative — recorded as-is, not an
    error (the mark-job math; here we assert the sign convention)."""
    prev = burn.burn_projection(LEAP, 100, 25, 160, 135)["extrinsic_now"]
    # A week later, fewer DTE but IV spiked hard -> extrinsic higher than before.
    cur = burn.burn_projection(LEAP, 100, 60, 153, 135)["extrinsic_now"]
    realized = round(prev - cur, 2)
    assert cur > prev
    assert realized < 0


# ---------------------------------------------------------------------------
# Case 3 — deep-ITM drift: extrinsic near zero -> floor, flag, capped coverage
# ---------------------------------------------------------------------------
def test_deep_itm_drift_floors_burn_and_caps_coverage():
    # A deep-ITM dividend payer: q offsets r so model extrinsic collapses to ~0.
    p = burn.burn_projection({"strike": 50, "contracts": 1}, 200, 20, 150, 135, q=0.03)
    assert p["extrinsic_now"] == 0.0
    assert p["low_extrinsic_flag"] is True
    assert p["projected_burn_total"] == 0.0
    assert p["projected_burn_per_week"] >= 0.0  # never negative
    cov = burn.coverage(50.0, p["burn_per_week_with_slippage"], p["low_extrinsic_flag"])
    assert cov["capped"] is True
    assert cov["status"] == "low_extrinsic"
    assert cov["ratio"] == config.COVERAGE_DISPLAY_CAP  # not an absurd number


# ---------------------------------------------------------------------------
# Case 4 — hold past plan: auto-extend, burn/week monotonically increases
# ---------------------------------------------------------------------------
def test_hold_past_plan_triggers_extension():
    # current_dte (130) < planned_exit_dte (135): held past plan.
    p = burn.burn_projection(LEAP, 100, 30, 130, 135)
    assert p["extended"] is True
    # Window slid to project the next EXTENSION_STEP_WEEKS forward from now.
    assert p["planned_exit_dte"] == 130 - config.EXTENSION_STEP_WEEKS * 7
    assert p["weeks_remaining"] > 0
    assert p["projected_burn_per_week"] > 0


def test_extension_burn_per_week_is_monotonic_and_theta_is_flat():
    """Anti-zombie readout. For a deep-ITM 0.90-delta LEAP the extrinsic decay is
    FRONT-loaded (fast early, slow late — the inverse of ATM theta), so 135 DTE
    sits in a genuinely FLAT region: model burn/week barely moves as the hold
    extends, and if anything eases toward expiry. The with-slippage figure
    monotonically DECREASES because the fixed round-trip slippage amortizes over
    more weeks. This is the honest behavior (see IMPLEMENTATION_NOTES); the spec's
    'extending raises burn/wk' assumes an ATM curve and does not hold for a real
    LEAP. The real hold risk (delta saturation / roll floor) is owned elsewhere."""
    model_bpw, slip_bpw = [], []
    for extra in (1, 2, 3, 4, 6, 8):
        c = burn.extension_cost(LEAP, 100, 30, 139, extra)
        model_bpw.append(c["projection"]["projected_burn_per_week"])
        slip_bpw.append(c["burn_per_week_with_slippage"])
    # Model burn/week is flat: within a few percent across an 8-week extension.
    assert max(model_bpw) - min(model_bpw) < 0.05 * max(model_bpw)
    # With-slippage figure is strictly monotonic (decreasing) — no jitter.
    assert all(slip_bpw[i] > slip_bpw[i + 1] for i in range(len(slip_bpw) - 1))


# ---------------------------------------------------------------------------
# Case 5 — extension_cost matches a direct burn_projection at the extended window
# ---------------------------------------------------------------------------
def test_extension_cost_matches_direct_projection():
    extra = 4
    c = burn.extension_cost(LEAP, 100, 30, 139, extra)
    exit_dte = 139 - extra * 7
    direct = burn.burn_projection(LEAP, 100, 30, 139, exit_dte)
    assert c["exit_dte"] == exit_dte
    assert c["burn_per_week_with_slippage"] == direct["burn_per_week_with_slippage"]


def test_extension_cost_changes_burn_per_week_vs_now():
    """extension_cost projects a different (well-defined) burn/wk than the current
    plan window, so the UI can render 'burn/wk of $Y over N more weeks vs $X now'.
    The values are genuinely distinct — we don't claim a direction the instrument
    doesn't support (see the flatness test above)."""
    at_plan = burn.burn_projection(LEAP, 100, 30, 139, config.PLANNED_EXIT_DTE)
    extended = burn.extension_cost(LEAP, 100, 30, 139, 4)
    assert extended["burn_per_week_with_slippage"] != at_plan["burn_per_week_with_slippage"]
    assert extended["burn_per_week_with_slippage"] is not None


# ---------------------------------------------------------------------------
# Case 6 — slippage: exact amortized round-trip; fallback % when no chain
# ---------------------------------------------------------------------------
def test_slippage_adds_exact_amortized_round_trip_from_chain():
    # Injected chain spread: bid/ask on the LEAP. Round-trip = full spread.
    bid, ask = 21.00, 21.40
    p = burn.burn_projection(LEAP, 100, 30, 195, 135, bid=bid, ask=ask)
    expected_slip_total = (ask - bid) * 1 * 100  # full spread x contracts x 100
    assert p["exit_slippage_est"] == round(expected_slip_total, 2)
    amortized = round(expected_slip_total / p["weeks_remaining"], 2)
    assert p["burn_per_week_with_slippage"] == round(p["projected_burn_per_week"] + amortized, 2)
    assert p["burn_per_week_with_slippage"] > p["projected_burn_per_week"]


def test_slippage_falls_back_to_pct_when_no_chain_cached():
    p = burn.burn_projection(LEAP, 100, 30, 195, 135)  # no bid/ask
    leap_price_total = round((p["extrinsic_now"] / 100 + max(100 - 79, 0.0)) * 100, 2)
    expected = round(config.LEAP_SLIPPAGE_PCT_FALLBACK / 100.0 * leap_price_total, 2)
    assert p["exit_slippage_est"] == expected


# ---------------------------------------------------------------------------
# Case 7 — net juice + coverage thresholds; portfolio rollup = sum of nets
# ---------------------------------------------------------------------------
def test_net_juice_is_juice_minus_burn():
    assert burn.net_juice_per_week(50.0, 14.0) == 36.0
    assert burn.net_juice_per_week(None, 14.0) is None
    assert burn.net_juice_per_week(50.0, None) is None


def test_coverage_threshold_classification_at_boundaries():
    # Exactly healthy boundary.
    assert burn.coverage(30.0, 10.0)["status"] == "healthy"           # 3.0
    assert burn.coverage(29.9, 10.0)["status"] == "marginal"          # just under 3.0
    # Exactly marginal boundary.
    assert burn.coverage(20.0, 10.0)["status"] == "marginal"          # 2.0
    assert burn.coverage(19.9, 10.0)["status"] == "flagged"           # just under 2.0
    assert burn.coverage(5.0, 10.0)["status"] == "flagged"            # 0.5


def test_coverage_capped_when_burn_non_positive():
    c = burn.coverage(50.0, 0.0)
    assert c["capped"] is True and c["status"] == "low_extrinsic"
    assert c["ratio"] == config.COVERAGE_DISPLAY_CAP


def test_portfolio_rollup_sums_net_not_gross():
    per_position = [
        burn.net_juice_per_week(50.0, 14.0),
        burn.net_juice_per_week(30.0, 20.0),
        burn.net_juice_per_week(40.0, 12.0),
    ]
    rollup = round(sum(per_position), 2)
    assert rollup == round((50 - 14) + (30 - 20) + (40 - 12), 2)  # 74.0


# ---------------------------------------------------------------------------
# Case 8 — queue integration: net-juice ranking + single source of truth
# ---------------------------------------------------------------------------
def test_equal_gross_juice_but_higher_iv_ranks_lower_on_net():
    """Two candidates, identical gross weekly extrinsic per share, but different
    IV. The higher-IV name buys more LEAP extrinsic -> more burn -> lower NET
    juice. This penalizes high IV with no separate rule."""
    # Same spot, same LEAP strike, same gross weekly extrinsic; differ only in IV.
    lo = burn.candidate_net_juice(spot=100, iv=25, leap_strike=79,
                                  leap_cost_per_share=22.0, weekly_extrinsic_per_share=0.40)
    hi = burn.candidate_net_juice(spot=100, iv=45, leap_strike=79,
                                  leap_cost_per_share=22.0, weekly_extrinsic_per_share=0.40)
    assert hi["net_juice_weekly_pct"] < lo["net_juice_weekly_pct"]
    assert hi["burn_per_week_ps"] > lo["burn_per_week_ps"]


def test_queue_ranks_on_net_not_gross():
    """build_queue_state orders GO candidates by NET juice/week. Two names with
    equal gross but different net rank net-first (higher net = rank 1)."""
    import queue_state
    rows = [
        {"ticker": "AAA", "verdict": "GO", "juice_weekly_pct": 2.0, "net_juice_weekly_pct": 1.2},
        {"ticker": "BBB", "verdict": "GO", "juice_weekly_pct": 2.0, "net_juice_weekly_pct": 1.7},
        {"ticker": "CCC", "verdict": "GO", "juice_weekly_pct": 2.0, "net_juice_weekly_pct": 0.9},
    ]
    qs = queue_state.build_queue_state(state={"positions": []}, rows=rows)
    order = [c.symbol for c in qs.candidates]
    assert order == ["BBB", "AAA", "CCC"]  # by net desc, not gross (all equal gross)


def test_queue_falls_back_to_gross_when_net_missing():
    import queue_state
    rows = [
        {"ticker": "AAA", "verdict": "GO", "juice_weekly_pct": 3.0},  # no net
        {"ticker": "BBB", "verdict": "GO", "juice_weekly_pct": 1.0, "net_juice_weekly_pct": 2.0},
    ]
    qs = queue_state.build_queue_state(state={"positions": []}, rows=rows)
    order = [c.symbol for c in qs.candidates]
    assert order == ["AAA", "BBB"]  # AAA's gross 3.0 outranks BBB's net 2.0


def test_queue_and_position_view_agree_for_identical_inputs():
    """Single source of truth: the candidate (queue) net figure and the position
    view both funnel through burn_projection + net_juice_per_week, so identical
    inputs yield an identical net $/week."""
    spot, iv, strike = 100, 30, 79
    entry_dte, exit_dte = config.LEAP_ENTRY_DTE_DEFAULT, config.PLANNED_EXIT_DTE
    weekly_extr_ps = 0.42

    # Queue path.
    cand = burn.candidate_net_juice(spot=spot, iv=iv, leap_strike=strike,
                                    leap_cost_per_share=22.0,
                                    weekly_extrinsic_per_share=weekly_extr_ps)
    # Position-view path: same burn_projection, same net_juice_per_week helper.
    proj = burn.burn_projection({"strike": strike, "contracts": 1}, spot, iv,
                                entry_dte, exit_dte)
    burn_pw_ps = proj["burn_per_week_with_slippage"] / 100.0
    net_ps = burn.net_juice_per_week(weekly_extr_ps, burn_pw_ps)
    assert cand["net_juice_per_week_ps"] == round(net_ps, 4)


# ---------------------------------------------------------------------------
# Guard rails / None-safety
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Regression — XLK-like live entry (~195 DTE, elevated IV)
# ---------------------------------------------------------------------------
def test_xlk_like_entry_net_below_gross_and_burn_reflects_iv():
    """A position resembling the live XLK entry: deep-ITM LEAP entered ~195 DTE
    with elevated IV. Its NET juice must be visibly below GROSS, and its projected
    burn must reflect the elevated IV (materially higher than at a calm IV)."""
    spot, strike, leap_cost_ps, weekly_extr_ps = 235.0, 205.0, 38.0, 0.62
    elevated = burn.candidate_net_juice(spot=spot, iv=28, leap_strike=strike,
                                        leap_cost_per_share=leap_cost_ps,
                                        weekly_extrinsic_per_share=weekly_extr_ps)
    calm = burn.candidate_net_juice(spot=spot, iv=16, leap_strike=strike,
                                    leap_cost_per_share=leap_cost_ps,
                                    weekly_extrinsic_per_share=weekly_extr_ps)
    gross_pct = round(weekly_extr_ps / leap_cost_ps * 100, 2)
    # Net visibly below gross (burn takes a real bite).
    assert elevated["net_juice_weekly_pct"] < gross_pct
    assert gross_pct - elevated["net_juice_weekly_pct"] > 0.1  # visible gap
    # Burn reflects the elevated IV: materially higher than the calm-IV burn.
    assert elevated["burn_per_week_ps"] > calm["burn_per_week_ps"] * 1.3
    # And the entry was priced at ~195 DTE held to the planned exit.
    assert elevated["projection"]["planned_exit_dte"] == config.PLANNED_EXIT_DTE
    assert elevated["projection"]["weeks_remaining"] == round(
        (config.LEAP_ENTRY_DTE_DEFAULT - config.PLANNED_EXIT_DTE) / 7.0, 2)


def test_unpriceable_inputs_return_none_fields_not_raise():
    for args in [
        ({"strike": None, "contracts": 1}, 100, 30, 195, 135),
        ({"strike": 79, "contracts": 0}, 100, 30, 195, 135),
        ({"strike": 79, "contracts": 1}, None, 30, 195, 135),
        ({"strike": 79, "contracts": 1}, 100, None, 195, 135),
        ({"strike": 79, "contracts": 1}, 100, 30, None, 135),
    ]:
        p = burn.burn_projection(*args)
        assert p["priceable"] is False
        assert p["projected_burn_per_week"] is None


def test_clock_derives_current_dte_from_expiration_when_absent():
    from datetime import date

    class _Clock:
        def __init__(self, d):
            self._d = d

        def __call__(self):
            from datetime import datetime
            return datetime(self._d.year, self._d.month, self._d.day)

    # expiration ~195 days out from the frozen clock.
    exp = "2026-07-15"
    clk = _Clock(date(2026, 1, 1))  # 195 days before 2026-07-15
    p = burn.burn_projection({"strike": 79, "contracts": 1, "expiration": exp},
                             100, 30, None, 135, clock=clk)
    assert p["priceable"] is True
    assert p["weeks_remaining"] > 0
