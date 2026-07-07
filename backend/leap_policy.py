"""LEAP long-leg lifecycle: health, roll policy, roll-cost, delta velocity.

In a PMCC the LEAP *is* the deployed capital. The short side already has a full
management engine (75% rule, defend, roll ledger); this module gives the long
leg the same discipline:

  * leap_health()      — DTE, extrinsic remaining (+ weeks-of-juice runway),
                         juice-vs-burn maintenance, and delta velocity, computed
                         from stored state + live price/mark. Attached to each
                         position by position_manager.enrich_position and read by
                         the alert engine.
  * roll_policy()      — when to roll the long leg (DTE floor OR extrinsic runway
                         too short). Both thresholds are PROPOSED_DEFAULT.
  * roll_cost_estimate — suggested replacement LEAP + net debit + whether that
                         debit still fits the 2×ATR cash reserve (reuses the
                         account-gate reserve logic).

Nothing here executes; it recommends and prepares numbers. All price-dependent
math degrades to None offline/in demo when a leg can't be priced, never raises.
"""
from __future__ import annotations

import config
import data_handler
import indicators


# ---------------------------------------------------------------------------
# Roll policy (Task 1b) — pure, no market data
# ---------------------------------------------------------------------------
def roll_policy(leap_dte: int | None,
                extrinsic_weeks_remaining: float | None) -> dict:
    """A LEAP roll is RECOMMENDED when EITHER the DTE has dropped below the floor
    (theta steepens under ~90 DTE) OR the remaining extrinsic is worth less than
    a few weeks of the position's own juice (burn about to outpace collection).
    Returns {roll_due, reasons:[...]}."""
    reasons = []
    if leap_dte is not None and leap_dte < config.LEAP_ROLL_DTE_FLOOR:
        reasons.append(
            f"DTE {leap_dte} < {config.LEAP_ROLL_DTE_FLOOR} floor (long-leg theta steepens)")
    if (extrinsic_weeks_remaining is not None
            and extrinsic_weeks_remaining < config.LEAP_MIN_EXTRINSIC_WEEKS):
        reasons.append(
            f"extrinsic runway {extrinsic_weeks_remaining:.1f}wk < "
            f"{config.LEAP_MIN_EXTRINSIC_WEEKS}wk (burn about to outpace juice)")
    return {"roll_due": bool(reasons), "reasons": reasons}


# ---------------------------------------------------------------------------
# Health block (Tasks 1a, 2a, 3) — needs live price + the stored mark
# ---------------------------------------------------------------------------
def _leap_dte(position: dict, df=None) -> int | None:
    """Calendar days to LEAP expiry. Prefers the value recompute_derived stamped
    on the position; falls back to the stored expiration, then the static
    entry-time snapshot."""
    if position.get("leap_dte") is not None:
        return position["leap_dte"]
    leap = position.get("leap") or {}
    exp = leap.get("expiration")
    if exp:
        from datetime import datetime, timezone
        try:
            return (datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
                    - datetime.now(timezone.utc).date()).days
        except ValueError:
            pass
    return leap.get("dte")


def _delta_velocity(position: dict) -> dict:
    """Delta change over the last DELTA_VELOCITY_WINDOW recorded sessions from
    the position's rolling delta_history (oldest→newest). ``drop`` is positive
    when delta has fallen. sessions_available < window+1 means not enough history
    yet (the feature ships cold and warms up)."""
    history = [h for h in (position.get("delta_history") or [])
               if h.get("leap_delta") is not None]
    window = config.DELTA_VELOCITY_WINDOW
    out = {"window": window, "sessions_available": len(history),
           "start": None, "end": None, "drop": None}
    if len(history) >= window + 1:
        start = float(history[-1 - window]["leap_delta"])
        end = float(history[-1]["leap_delta"])
        out.update({"start": round(start, 4), "end": round(end, 4),
                    "drop": round(start - end, 4)})
    return out


def leap_health(position: dict, df=None, stock_price: float | None = None,
                q: float = 0.0) -> dict:
    """Full LEAP-health block for one position. Combines stored derived fields
    (leap_dte, trailing_avg_weekly_juice from recompute_derived) with live
    price/mark to produce extrinsic remaining, weekly burn, net maintenance,
    the roll recommendation, and delta velocity. Every price-dependent field is
    None when the leg can't be priced."""
    leap = position.get("leap") or {}
    contracts = int(leap.get("contracts") or 0)
    strike = leap.get("strike")
    ticker = position.get("ticker", "")
    dte = _leap_dte(position, df)
    trailing_juice = position.get("trailing_avg_weekly_juice")

    if stock_price is None:
        if df is None:
            df = data_handler.get_daily(ticker)
        stock_price = indicators.last(df)

    intrinsic = extrinsic_remaining = below_intrinsic = None
    weekly_burn = net_maintenance = weeks_remaining = leap_delta = None
    mark_ps = (float(leap["current_bid"]) / (contracts * 100)
               if (leap.get("current_bid") is not None and contracts) else None)

    if leap and contracts and strike is not None and stock_price is not None:
        intrinsic_total = max(stock_price - float(strike), 0.0) * contracts * 100
        value_total = float(leap["current_bid"]) if leap.get("current_bid") is not None else None
        if value_total is not None:
            raw_extrinsic = value_total - intrinsic_total
            # Deep-ITM midpoints can quote below intrinsic — that's a liquidity
            # signal, not real negative time value. Floor at 0, flag the raw sign.
            below_intrinsic = raw_extrinsic < 0
            extrinsic_remaining = round(max(raw_extrinsic, 0.0), 2)
            intrinsic = round(intrinsic_total, 2)
            if trailing_juice and trailing_juice > 0:
                weeks_remaining = round(extrinsic_remaining / trailing_juice, 1)
        weekly_burn = indicators.leap_weekly_burn(stock_price, strike, dte, mark_ps,
                                                  contracts, q)
        if weekly_burn is not None and trailing_juice is not None:
            net_maintenance = round(trailing_juice - weekly_burn, 2)
        leap_delta, _ = indicators.call_greeks(stock_price, strike, dte, mark_ps, q=q)

    if net_maintenance is None:
        maintenance_status = "unknown"
    elif net_maintenance >= 0:
        maintenance_status = "self_funding"
    else:
        maintenance_status = "burning"

    # Ongoing income adequacy: realized trailing weekly juice as a % of deployed
    # LEAP capital vs the strategy's per-profile target — the same bar the entry
    # gate (juice_adequacy) checks ONCE, re-checked every recompute. Distinct from
    # capital-burn (juice below LEAP THETA, the extreme): this owns the wide band
    # where a position still self-funds its decay but no longer clears the income
    # target — quietly underperforming while capital is intact to redeploy.
    juice_yield_pct = juice_target_pct = juice_adequate = None
    leap_cost = float(leap.get("cost_basis") or 0)
    if trailing_juice is not None and leap_cost:
        import account_gate
        juice_target_pct = account_gate.weekly_yield_target_pct(ticker)
        juice_yield_pct = round(trailing_juice / leap_cost * 100, 2)
        juice_adequate = juice_yield_pct >= juice_target_pct

    policy = roll_policy(dte, weeks_remaining)
    return {
        "leap_dte": dte,
        "leap_intrinsic": intrinsic,
        "leap_extrinsic_remaining": extrinsic_remaining,
        "leap_extrinsic_below_intrinsic": below_intrinsic,
        "trailing_avg_weekly_juice": trailing_juice,
        "leap_extrinsic_weeks_remaining": weeks_remaining,
        "leap_weekly_burn": weekly_burn,
        "net_weekly_maintenance": net_maintenance,
        "maintenance_status": maintenance_status,
        "weekly_juice_yield_pct": juice_yield_pct,
        "juice_target_pct": juice_target_pct,
        "juice_adequate": juice_adequate,
        "leap_delta": leap_delta,
        "delta_velocity": _delta_velocity(position),
        "roll_due": policy["roll_due"],
        "roll_reasons": policy["reasons"],
    }


# ---------------------------------------------------------------------------
# Roll-cost estimator (Task 1c)
# ---------------------------------------------------------------------------
def roll_cost_estimate(ticker: str, position: dict | None = None,
                       df=None, state: dict | None = None) -> dict:
    """Estimate the net debit to roll this position's LEAP into a fresh
    ~target-delta / ~180-DTE LEAP, and whether that debit still fits the 2×ATR
    cash reserve. New LEAP ask-side and the current LEAP bid-side both come from
    the live chain when available, else a Black-Scholes estimate at the ticker's
    trailing realized vol — the same fallback convention as the juice math.

    A LEAP roll leaves contract count and ATR unchanged, so the book's reserve
    requirement is unchanged; the check is simply whether post-debit free cash
    still covers it. A breach surfaces as a blocking warning (reserve_ok=False),
    handled at execution the same way an entry-gate breach is (override_reason).
    """
    import account_gate
    import logging_handler as log

    ticker = ticker.upper()
    if state is None:
        state = log.load_state()
    if position is None:
        position = log.find_position(state, ticker)
    if not position or not (position.get("leap") or {}):
        return {"ticker": ticker, "error": "no open LEAP"}
    leap = position["leap"]
    contracts = int(leap.get("contracts") or 0)

    if df is None:
        df = data_handler.get_daily(ticker)
    S = indicators.last(df)
    hv = indicators.hist_vol(df) if df is not None else None
    if S is None or not hv or not contracts:
        return {"ticker": ticker, "error": "insufficient data to price a roll"}
    sigma = hv / 100.0
    r = config.RISK_FREE_RATE
    t_leap = config.LEAP_TARGET_DTE / 365.0

    # New LEAP: target-delta strike priced at realized vol (ask-side estimate).
    k_new = account_gate._leap_strike_for_delta(S, t_leap, r, sigma)
    if k_new is None:
        return {"ticker": ticker, "error": "could not solve a target-delta LEAP strike"}
    new_cost_ps = indicators._bs_call_price(S, k_new, t_leap, r, sigma)
    new_cost_total = round(new_cost_ps * contracts * 100, 2)

    # Current LEAP: bid-side sell-to-close value — the stored mark when present,
    # else a BS estimate of the held strike at its live DTE.
    dte = _leap_dte(position, df)
    if leap.get("current_bid") is not None:
        cur_value_total = round(float(leap["current_bid"]), 2)
    else:
        cur_ps = indicators._bs_call_price(S, float(leap.get("strike") or 0),
                                           max(dte or 0, 1) / 365.0, r, sigma)
        cur_value_total = round(cur_ps * contracts * 100, 2)

    net_debit = round(new_cost_total - cur_value_total, 2)

    # Reserve check (reuse the account-gate machinery). Contract count/ATR are
    # unchanged by a roll, so reserve_required is the book's existing total.
    cash_info = account_gate.resolve_operating_cash(state)
    operating = cash_info["amount"]
    reserves = [account_gate._position_reserve(p) for p in state.get("positions", [])
                if p.get("status") != "closed"]
    reserve_required = round(sum(x for x in reserves if x is not None), 2)
    free_after = round(operating - net_debit, 2)
    reserve_ok = free_after >= reserve_required

    return {
        "ticker": ticker,
        "current_leap": {"strike": leap.get("strike"), "contracts": contracts,
                         "dte": dte, "sell_to_close_value": cur_value_total},
        "new_leap": {"strike": round(k_new, 1), "target_dte": config.LEAP_TARGET_DTE,
                     "target_delta": config.LEAP_TARGET_DELTA,
                     "est_cost": new_cost_total},
        "net_debit": net_debit,
        "reserve_required": reserve_required,
        "operating_cash": operating,
        "operating_cash_source": cash_info["source"],
        "free_cash_after": free_after,
        "reserve_ok": reserve_ok,
        "source": "estimate",
    }
