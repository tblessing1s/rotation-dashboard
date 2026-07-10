"""Weekly theta burn & net-juice accounting — pure, model-based, offline.

The LEAP is held ~8 weeks and exited/rolled around 130-140 DTE, the flattest
part of the theta curve. Only the extrinsic *consumed during the hold window* is
a true cost; the rest is recovered when the LEAP is sold (minus slippage). So a
position's burn is the extrinsic DIFFERENCE between two Black-Scholes model
prices — one at the current DTE and one at the planned-exit DTE, priced at the
SAME spot and IV — divided by the weeks in that window.

HARD_CFM_RULE (config.BURN_IS_MODEL_DIFF): burn is that two-point model
difference, NEVER ``total_extrinsic x (held_days / total_days)``. Straight-line
proration averages in the steep never-held tail and overstates front-end burn.

Everything here is a pure function of plain values — no I/O, no clock reads, no
provider calls — so it is fully offline-testable. The one Black-Scholes
dependency is ``indicators._bs_call_price`` (the app's single pricing engine);
this module never builds a second pricer. The caller resolves the IV (via the
put-IV substitution path in option_chain._augment_call_greeks) and the live spot,
then passes them in as plain numbers.

Units: ``spot``/``strike``/``*_per_share`` are per-share dollars; ``iv`` is
annualized volatility in PERCENT (e.g. 30.0 for 30%), matching the codebase's
``volatility`` / ``call_greeks(reported_iv=...)`` convention. Every extrinsic and
burn figure returned is WHOLE-POSITION dollars (x contracts x 100), matching
``indicators.leap_weekly_burn`` and ``leap_policy.net_weekly_maintenance``.
"""
from __future__ import annotations

import config
import indicators

# Never divide by a zero/near-zero window; the held-past-plan guard rail extends
# the window before we get here, so this is only a final safety floor.
_WEEKS_EPSILON = 1.0 / 7.0  # one day, expressed in weeks


def _model_extrinsic_per_share(spot: float, strike: float, dte: float,
                               sigma: float, q: float) -> float | None:
    """Model (Black-Scholes) extrinsic per share at ``dte`` days: the BS call
    price minus intrinsic. Same spot & sigma at both DTE points, so intrinsic
    cancels in a burn difference — but we return the clamped extrinsic itself so
    the deep-ITM low-extrinsic guard rail can inspect it. None when unpriceable."""
    T = (dte or 0) / 365.0
    if not (spot and spot > 0 and strike and strike > 0 and T > 0 and sigma and sigma > 0):
        return None
    price = indicators._bs_call_price(spot, strike, T, config.RISK_FREE_RATE, sigma, q)
    intrinsic = max(spot - strike, 0.0)
    return max(price - intrinsic, 0.0)


def exit_slippage_est(leap_price_total: float | None,
                      contracts: int,
                      bid: float | None = None,
                      ask: float | None = None) -> float | None:
    """Estimated round-trip exit slippage in whole-position dollars.

    Preferred (a fresh chain is cached): half the current LEAP bid-ask spread x 2
    (round trip) = the full spread, per share x contracts x 100. Fallback (no
    fresh chain): ``LEAP_SLIPPAGE_PCT_FALLBACK`` percent of the LEAP price. None
    when neither input is usable."""
    n = int(contracts or 0)
    if n <= 0:
        return None
    if bid is not None and ask is not None and ask >= bid >= 0:
        half_spread = (ask - bid) / 2.0
        return round(half_spread * 2.0 * n * 100, 2)  # round trip = full spread
    if leap_price_total is not None and leap_price_total > 0:
        return round(config.LEAP_SLIPPAGE_PCT_FALLBACK / 100.0 * leap_price_total, 2)
    return None


def _effective_exit_dte(current_dte: int, planned_exit_dte: int,
                        extension_step_weeks: int) -> tuple[int, bool]:
    """Resolve the DTE the projection exits at, honouring the anti-zombie guard.

    Normal case (current DTE still above the plan): exit at ``planned_exit_dte``.
    Held past the plan (current DTE <= planned_exit_dte): the plan is moot, so
    project the NEXT ``extension_step_weeks`` of holding forward from now — exit
    at ``current_dte - step*7`` — and flag it extended. Burn/week rises because
    that window sits lower on (steeper part of) the theta curve. Returns
    (effective_exit_dte, extended)."""
    if current_dte > planned_exit_dte:
        return planned_exit_dte, False
    step_days = max(int(extension_step_weeks or config.EXTENSION_STEP_WEEKS), 1) * 7
    return current_dte - step_days, True


def burn_projection(leap_contract: dict,
                    spot: float | None,
                    iv: float | None,
                    current_dte: int | None,
                    planned_exit_dte: int | None = None,
                    clock=None,
                    *,
                    q: float = 0.0,
                    bid: float | None = None,
                    ask: float | None = None,
                    extension_step_weeks: int | None = None) -> dict:
    """Model-based theta burn over the *held* window for one LEAP.

    ``leap_contract`` supplies ``strike`` and ``contracts`` (and, optionally,
    ``expiration`` used only when ``current_dte`` is None and a ``clock`` is
    given). ``spot`` and ``iv`` are the current inputs (iv in percent; the caller
    resolves it via the put-IV substitution path). ``planned_exit_dte`` defaults
    to ``config.PLANNED_EXIT_DTE``.

    The ``clock`` argument exists for signature fidelity with the spec; the pure
    math needs only explicit DTE integers, so it is consulted solely to derive
    ``current_dte`` from ``expiration`` when the caller passes no DTE.

    Returns a JSON-serializable dict (never raises):
        extrinsic_now, extrinsic_at_exit          — whole-position $, model
        projected_burn_total                       — extrinsic_now - extrinsic_at_exit
        weeks_remaining                            — (current_dte - exit_dte)/7
        projected_burn_per_week                    — burn_total / weeks
        exit_slippage_est                          — round-trip $, amortized below
        burn_per_week_with_slippage                — the figure coverage uses
        planned_exit_dte                           — effective (post-extension)
        extended                                   — held past plan, window slid
        low_extrinsic_flag                         — deep-ITM, burn floored at 0
        priceable                                  — False => the $ fields are None
    """
    strike = (leap_contract or {}).get("strike")
    contracts = int((leap_contract or {}).get("contracts") or 0)
    if planned_exit_dte is None:
        planned_exit_dte = config.PLANNED_EXIT_DTE
    if extension_step_weeks is None:
        extension_step_weeks = config.EXTENSION_STEP_WEEKS

    if current_dte is None and clock is not None:
        current_dte = _dte_from_expiration((leap_contract or {}).get("expiration"), clock)

    out = {
        "extrinsic_now": None, "extrinsic_at_exit": None,
        "projected_burn_total": None, "weeks_remaining": None,
        "projected_burn_per_week": None, "exit_slippage_est": None,
        "burn_per_week_with_slippage": None,
        "planned_exit_dte": planned_exit_dte, "extended": False,
        "low_extrinsic_flag": False, "priceable": False,
    }
    if not (strike and contracts and current_dte is not None and spot and iv):
        return out

    exit_dte, extended = _effective_exit_dte(int(current_dte), int(planned_exit_dte),
                                             extension_step_weeks)
    out["planned_exit_dte"] = exit_dte
    out["extended"] = extended

    sigma = iv / 100.0
    ext_now_ps = _model_extrinsic_per_share(spot, strike, current_dte, sigma, q)
    ext_exit_ps = _model_extrinsic_per_share(spot, strike, exit_dte, sigma, q)
    if ext_now_ps is None or ext_exit_ps is None:
        return out

    scale = contracts * 100
    extrinsic_now = round(ext_now_ps * scale, 2)
    extrinsic_at_exit = round(ext_exit_ps * scale, 2)

    # Deep-ITM drift after a run-up: model extrinsic collapses toward zero. Floor
    # burn at zero and flag it — never emit negative burn (spec §2 guard rail).
    low_extrinsic = ext_now_ps < config.BURN_LOW_EXTRINSIC_FLOOR
    burn_total = max(ext_now_ps - ext_exit_ps, 0.0) * scale if low_extrinsic \
        else max((ext_now_ps - ext_exit_ps) * scale, 0.0)
    burn_total = round(burn_total, 2)

    weeks = max((current_dte - exit_dte) / 7.0, _WEEKS_EPSILON)
    burn_pw = round(burn_total / weeks, 2)

    leap_price_total = round((ext_now_ps + max(spot - strike, 0.0)) * scale, 2)
    slippage = exit_slippage_est(leap_price_total, contracts, bid=bid, ask=ask)
    slippage_pw = round((slippage or 0.0) / weeks, 2)
    burn_pw_slip = round(burn_pw + slippage_pw, 2)

    out.update({
        "extrinsic_now": extrinsic_now,
        "extrinsic_at_exit": extrinsic_at_exit,
        "projected_burn_total": burn_total,
        "weeks_remaining": round(weeks, 2),
        "projected_burn_per_week": burn_pw,
        "exit_slippage_est": slippage,
        "burn_per_week_with_slippage": burn_pw_slip,
        "low_extrinsic_flag": low_extrinsic,
        "priceable": True,
    })
    return out


def extension_cost(leap_contract: dict,
                   spot: float | None,
                   iv: float | None,
                   current_dte: int | None,
                   extra_weeks: int,
                   *,
                   q: float = 0.0,
                   bid: float | None = None,
                   ask: float | None = None) -> dict:
    """Burn/week if the hold is extended ``extra_weeks`` past *now*.

    Projects an exit at ``current_dte - extra_weeks*7`` — further down the theta
    curve — so burn/week is strictly higher than a shorter window. Drives the UI
    readout "Holding N more weeks raises burn/wk from $X to $Y". Values match a
    direct ``burn_projection`` call at the extended window (that IS the call).
    Returns {extra_weeks, exit_dte, burn_per_week_with_slippage, projection}."""
    n = max(int(extra_weeks or 0), 1)
    exit_dte = int(current_dte) - n * 7 if current_dte is not None else None
    proj = burn_projection(leap_contract, spot, iv, current_dte, exit_dte,
                           q=q, bid=bid, ask=ask)
    return {
        "extra_weeks": n,
        "exit_dte": exit_dte,
        "burn_per_week_with_slippage": proj.get("burn_per_week_with_slippage"),
        "projection": proj,
    }


def net_juice_per_week(juice_per_week: float | None,
                       burn_per_week_with_slippage: float | None) -> float | None:
    """The headline: juice collected/week - burn/week (with slippage). The SINGLE
    definition of net juice — the position view and the entry queue both call
    this so they can never disagree (spec §6). None when either input is None.

    DAY-COUNT CONVENTION [NET_JUICE_TIME_BASE / HARD_CFM_RULE]: both terms are on
    ONE shared time base — a 7-CALENDAR-day week. burn/week is theta_per_calendar
    -day (call_greeks_full's theta_year ÷ 365 — a deliberate engine choice) × 7,
    equivalently the two-point model difference ÷ (Δdte/7). juice/week (realized)
    is one weekly cycle's net juice booked per ISO calendar week (~7 calendar
    days). The subtraction is therefore like-for-like. Pinned by
    test_burn.test_net_juice_day_count_convention_is_pinned so it can't drift."""
    if juice_per_week is None or burn_per_week_with_slippage is None:
        return None
    return round(juice_per_week - burn_per_week_with_slippage, 2)


def coverage(juice_per_week: float | None,
             burn_per_week_with_slippage: float | None,
             low_extrinsic_flag: bool = False) -> dict:
    """Coverage ratio = juice/week / burn/week (with slippage), with thresholds.

    status: healthy (>= COVERAGE_HEALTHY), marginal ([MARGINAL, HEALTHY)),
    flagged (< MARGINAL). When burn is floored near zero (low_extrinsic_flag, or a
    non-positive denominator) the ratio is capped at COVERAGE_DISPLAY_CAP and
    status is "low_extrinsic" — a near-zero denominator must never make a stressed
    position read as healthy; the frontend pairs this with the delta / assignment
    indicators. Returns {ratio, capped, status, healthy, marginal}."""
    out = {"ratio": None, "capped": False, "status": "unknown",
           "healthy": config.COVERAGE_HEALTHY, "marginal": config.COVERAGE_MARGINAL}
    if juice_per_week is None or burn_per_week_with_slippage is None:
        return out

    if low_extrinsic_flag or burn_per_week_with_slippage <= 0:
        out.update({"ratio": config.COVERAGE_DISPLAY_CAP, "capped": True,
                    "status": "low_extrinsic"})
        return out

    ratio = juice_per_week / burn_per_week_with_slippage
    capped = ratio > config.COVERAGE_DISPLAY_CAP
    if ratio >= config.COVERAGE_HEALTHY:
        status = "healthy"
    elif ratio >= config.COVERAGE_MARGINAL:
        status = "marginal"
    else:
        status = "flagged"
    out.update({"ratio": round(min(ratio, config.COVERAGE_DISPLAY_CAP), 2),
                "capped": capped, "status": status})
    return out


def candidate_net_juice(spot: float | None,
                        iv: float | None,
                        leap_strike: float | None,
                        leap_cost_per_share: float | None,
                        weekly_extrinsic_per_share: float | None,
                        *,
                        q: float = 0.0,
                        contracts: int = 1,
                        entry_dte: int | None = None,
                        exit_dte: int | None = None) -> dict:
    """Net juice/week for a HYPOTHETICAL entry — the entry-queue ranking metric.

    Prices a LEAP bought at ``entry_dte`` (default LEAP_ENTRY_DTE_DEFAULT) and
    exited at ``exit_dte`` (default PLANNED_EXIT_DTE) and nets its model burn/week
    (with fallback slippage) off the weekly short extrinsic. Runs through the SAME
    ``burn_projection`` the live position view uses, so identical inputs yield an
    identical net figure (spec §6 single-source-of-truth). This naturally
    penalizes high-IV candidates — more extrinsic bought means more burn — with no
    separate rule.

    Returns per-share and per-LEAP-cost-% views:
        {net_juice_per_week_ps, net_juice_weekly_pct, burn_per_week_ps,
         gross_weekly_extrinsic_ps, projection}. Fields are None when unpriceable.
    """
    if entry_dte is None:
        entry_dte = config.LEAP_ENTRY_DTE_DEFAULT
    if exit_dte is None:
        exit_dte = config.PLANNED_EXIT_DTE
    out = {"net_juice_per_week_ps": None, "net_juice_weekly_pct": None,
           "burn_per_week_ps": None, "gross_weekly_extrinsic_ps": weekly_extrinsic_per_share,
           "projection": None}
    if not (spot and iv and leap_strike and leap_cost_per_share
            and weekly_extrinsic_per_share is not None):
        return out

    proj = burn_projection({"strike": leap_strike, "contracts": contracts},
                           spot, iv, entry_dte, exit_dte, q=q)
    out["projection"] = proj
    burn_pw_total = proj.get("burn_per_week_with_slippage")
    if burn_pw_total is None:
        return out
    burn_pw_ps = burn_pw_total / (contracts * 100)
    net_ps = net_juice_per_week(weekly_extrinsic_per_share, burn_pw_ps)
    out["burn_per_week_ps"] = round(burn_pw_ps, 4)
    out["net_juice_per_week_ps"] = round(net_ps, 4) if net_ps is not None else None
    out["net_juice_weekly_pct"] = (round(net_ps / leap_cost_per_share * 100, 2)
                                   if net_ps is not None else None)
    return out


def _dte_from_expiration(expiration, clock) -> int | None:
    """Calendar days from ``clock``'s today to ``expiration`` (YYYY-MM-DD). Only
    used when a caller passes a clock but no explicit current_dte. Best-effort."""
    if not expiration or clock is None:
        return None
    try:
        from datetime import datetime
        exp = datetime.strptime(str(expiration)[:10], "%Y-%m-%d").date()
        today = clock().date() if callable(clock) else clock.date()
        return (exp - today).days
    except (TypeError, ValueError, AttributeError):
        return None
