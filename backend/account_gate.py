"""Level 5 entry gate — Account & Juice.

The 4-level gate (screening.entry_gate) answers "is this the right stock in the
right tape"; this level answers "is the ACCOUNT ready and does the TRADE pay".
Evaluated server-side inside executor.execute for every buy_leap: a failing
blocking check stops the entry unless the payload carries an explicit
``override_reason``, which is logged onto the immutable execution record.

All checks derive from state + cached OHLCV, so the gate works identically in
demo mode. When the caller (the Execute flow) has real chain numbers it passes
them in (leap cost, weekly extrinsic); otherwise the juice math falls back to a
Black-Scholes estimate from the ticker's own trailing volatility — the same
estimate the Scorecard's juice-adequacy column shows.
"""
from __future__ import annotations

import config
import data_handler
import dividends
import earnings
import indicators
import logging_handler as log
import position_types
import schwab_api


# ---------------------------------------------------------------------------
# Juice estimate — BS pricing off the ticker's own history (no chain needed).
# ---------------------------------------------------------------------------
def _leap_strike_for_delta(S: float, T: float, r: float, sigma: float,
                           target: float = config.LEAP_TARGET_DELTA) -> float | None:
    """Strike whose BS call delta ≈ target (delta falls as strike rises, so
    bisection is robust)."""
    lo, hi = 0.2 * S, 1.5 * S
    d_lo = indicators.bs_call_delta(S, lo, T, r, sigma)
    d_hi = indicators.bs_call_delta(S, hi, T, r, sigma)
    if d_lo is None or d_hi is None or not (d_hi <= target <= d_lo):
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        d = indicators.bs_call_delta(S, mid, T, r, sigma)
        if d is None:
            return None
        if abs(d - target) < 1e-4:
            return mid
        if d > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def juice_estimate(ticker: str, df=None) -> dict:
    """History-implied CFM income math for one ticker.

    Prices the 1.5x-ATR weekly short and a LEAP_TARGET_DELTA LEAP with
    Black-Scholes at the ticker's trailing 20d realized vol, then returns
    weekly extrinsic / LEAP cost — the weekly yield on deployed capital the
    strategy would earn if premium were priced exactly at realized vol.
    Everything is per-share/per-contract, so contract count cancels out.
    """
    if df is None:
        df = data_handler.get_daily(ticker)
    S = indicators.last(df)
    atr_val = indicators.atr(df) if df is not None else None
    hv = indicators.hist_vol(df) if df is not None else None  # annualized %
    none = {"ticker": ticker, "weekly_extrinsic_per_share": None,
            "leap_strike": None, "leap_cost_per_share": None,
            "weekly_yield_pct": None, "source": "estimate"}
    if S is None or atr_val is None or not hv:
        return none
    sigma = hv / 100.0
    r = config.RISK_FREE_RATE

    k_short = indicators.short_strike(S, atr_val)
    t_week = 5 / 365.0
    price_w = indicators._bs_call_price(S, k_short, t_week, r, sigma)
    extr_w = max(price_w - max(S - k_short, 0.0), 0.0)

    t_leap = config.LEAP_TARGET_DTE / 365.0
    k_leap = _leap_strike_for_delta(S, t_leap, r, sigma)
    if k_leap is None:
        return none
    leap_cost = indicators._bs_call_price(S, k_leap, t_leap, r, sigma)
    if not leap_cost:
        return none
    # NET juice: subtract the LEAP's model theta burn/week (over a hypothetical
    # entry at LEAP_ENTRY_DTE_DEFAULT held to PLANNED_EXIT_DTE, with fallback
    # slippage) from the gross weekly extrinsic. Computed through the SAME
    # burn.burn_projection the live position view uses, so the queue and the
    # position panel can never disagree (single source of truth). This is the
    # ranking key — it naturally penalizes high-IV candidates (more extrinsic
    # bought => more burn) with no separate rule. hv is annualized vol in percent.
    import burn
    net = burn.candidate_net_juice(spot=S, iv=hv, leap_strike=k_leap,
                                   leap_cost_per_share=leap_cost,
                                   weekly_extrinsic_per_share=extr_w)
    return {
        "ticker": ticker,
        "stock_price": round(S, 2),
        "short_strike": k_short,
        "weekly_extrinsic_per_share": round(extr_w, 3),
        "leap_strike": round(k_leap, 1),
        "leap_cost_per_share": round(leap_cost, 2),
        "weekly_yield_pct": round(extr_w / leap_cost * 100, 2),
        # .get() not [] — the shares path removes burn from the net-juice contract
        # (net == gross), so these keys may be absent/None. Hard-subscripting them
        # was the single KeyError site flagged in the migration audit (§Burn).
        "net_weekly_yield_pct": net.get("net_juice_weekly_pct"),
        "burn_weekly_per_share": net.get("burn_per_week_ps"),
        "net_weekly_extrinsic_per_share": net.get("net_juice_per_week_ps"),
        "hist_vol": round(hv, 1),
        "source": "estimate",
    }


def sector_size_suggestion(ticker: str, full_contracts: int | None = None) -> dict:
    """A sector-strength SIZING suggestion for a proposed entry — ADVISORY ONLY.

    Now that sector strength is a Level-2 VETO rather than a selector, it is kept
    as a SIZE lever: a STRONG sector (RS1M > SECTOR_RS1M_MIN AND breadth >=
    SECTOR_BREADTH_MIN) suggests full size; a merely-neutral sector that clears the
    veto suggests a reduced size (round(full x SECTOR_NEUTRAL_SIZE_FACTOR), min 1);
    a deteriorating sector — which Level 2 blocks — would size minimally if entered
    on an override. This never changes the ENFORCED contract count (the cash /
    reserve / capital checks use the caller's `contracts`); it is a recommendation
    the Execute flow can show and the operator can take or leave."""
    import sector_data
    import screening
    full = int(full_contracts or config.LEAP_CONTRACTS)
    etf = sector_data.sector_for(ticker) or ""
    try:
        sec = screening.sectors().get(etf, {}) if etf else {}
    except Exception:  # noqa: BLE001 — a sector sweep failure never blocks the gate
        sec = {}
    strong = bool(sec.get("strong"))
    deteriorating = bool(sec.get("deteriorating"))
    reduced = max(1, round(full * config.SECTOR_NEUTRAL_SIZE_FACTOR))
    if strong:
        modifier, suggested, reason = 1.0, full, "sector strong — full size"
    elif deteriorating:
        modifier, suggested = config.SECTOR_NEUTRAL_SIZE_FACTOR, reduced
        reason = "sector deteriorating — Level 2 blocks entry; minimal size if overridden"
    else:
        modifier, suggested = config.SECTOR_NEUTRAL_SIZE_FACTOR, reduced
        reason = "sector neutral — reduced size"
    return {"sector": etf, "sector_status": sec.get("status"),
            "strong": strong, "deteriorating": deteriorating,
            "modifier": modifier, "full_contracts": full,
            "suggested_contracts": suggested, "reason": reason}


def weekly_yield_target_pct(ticker: str | None = None) -> float:
    """Minimum weekly juice as % of LEAP cost. Growth stocks use the CFM cycle
    target (low end of 15-25% over the slow end of 4-8 weeks, ~1.9%/wk); ETFs
    (lower IV, steadier-income sleeve) clear the lower ETF bar instead."""
    import sector_data
    if ticker and sector_data.is_etf(ticker):
        return config.ETF_WEEKLY_JUICE_TARGET_PCT
    return round(config.CYCLE_RETURN_MIN / config.CYCLE_WEEKS_MAX * 100, 2)


def resolve_operating_cash(state: dict) -> dict:
    """Operating cash for the cash_reserve check: the LIVE Schwab account
    balance when connected, else the stored manual value (state.metadata.
    operating_cash) — which also serves as the last-known-good fallback on any
    live-fetch failure (token expired, network error, account not approved for
    trading). Demo mode never touches Schwab; a Schwab connection alone is
    enough to read live cash even with CFM_LIVE_TRADING off (this is a
    read-only account call, not an order).

    On a successful live read the fetched number is persisted back to
    state.metadata.operating_cash so every other reader of that field (the
    Positions Capital card, portfolio_risk, the daily checklist's reserve
    check) picks up the fresh value too, without separate wiring.
    """
    manual = float((state.get("metadata") or {}).get("operating_cash") or 0)
    if config.demo_enabled() or not schwab_api.configured():
        return {"amount": manual, "source": "manual", "error": None}
    try:
        live = round(float(data_handler.client().cash_balance()), 2)
    except Exception as e:  # noqa: BLE001 — degrade to the manual fallback
        return {"amount": manual, "source": "manual", "error": str(e)}
    if live != manual:
        state.setdefault("metadata", {})["operating_cash"] = live
        log.save_state(state)
    return {"amount": live, "source": "schwab", "error": None}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def _open_positions(state: dict) -> list[dict]:
    return [p for p in state.get("positions", []) if p.get("status") != "closed"]


def _position_reserve(p: dict) -> float | None:
    """One position's defensive reserve: RESERVE_ATR_MULT x ATR$ x contracts x 100."""
    leap = p.get("leap") or {}
    contracts = int(leap.get("contracts") or 0)
    if not contracts:
        return 0.0
    p_df = data_handler.get_daily(p.get("ticker", ""))
    atr_val = indicators.atr(p_df) if p_df is not None else None
    if atr_val is None:
        return None
    return config.RESERVE_ATR_MULT * atr_val * contracts * 100


def suggested_circuit_breaker(ticker: str, df=None) -> dict:
    """Default line-in-the-sand: max(MA50, price - 2xATR), capped just below
    spot when the stock is trading under its 50-day trend. Without the cap an
    above-spot MA50 would put a fresh LEAP past its own exit line on day one
    (today's close is already at/below the line). Operator-editable."""
    if df is None:
        df = data_handler.get_daily(ticker)
    price = indicators.last(df)
    atr_val = indicators.atr(df) if df is not None else None
    ma50 = indicators.sma(df, 50) if df is not None else None
    if price is None or atr_val is None:
        return {"price": None, "spot": round(price, 2) if price else None,
                "ma50": round(ma50, 2) if ma50 else None, "atr_stop": None,
                "capped": False, "below_trend": None}
    atr_stop = price - config.CIRCUIT_BREAKER_ATR_MULT * atr_val
    line = max(v for v in (ma50, atr_stop) if v is not None)
    below_trend = ma50 is not None and price < ma50
    # A stop at/above the current close is meaningless — clamp it just under
    # spot so the suggestion is a real leash, and flag why (below_trend).
    capped = line > price
    if capped:
        line = price - 0.01
    return {"price": round(line, 2), "spot": round(price, 2),
            "ma50": round(ma50, 2) if ma50 else None,
            "atr_stop": round(atr_stop, 2),
            "capped": capped, "below_trend": below_trend}


def _check(id_: str, label: str, ok, blocking: bool, detail: dict) -> dict:
    return {"id": id_, "label": label, "pass": bool(ok), "blocking": blocking,
            "detail": detail}


def evaluate(ticker: str, contracts: int | None = None,
             leap_cost_per_share: float | None = None,
             weekly_extrinsic_per_share: float | None = None,
             state: dict | None = None,
             position_type: str | None = None) -> dict:
    """Run every Level-5 check for a proposed entry.

    `leap_cost_per_share` / `weekly_extrinsic_per_share` come from the live
    chain when the Execute flow has one; missing values fall back to the
    history-implied estimate so the gate always produces numbers.

    `state` lets a bulk caller (see evaluate_many) pass one already-loaded
    state dict across many tickers instead of re-reading state.json each
    time; a single-ticker caller can omit it and a fresh load is used.

    `position_type` selects the base-leg profile (schema v20). SHARES adds the
    round-lot SIZE-BLOCK; the default (None -> LEAP_PMCC_LEGACY) preserves the
    exact legacy check set so existing callers are unaffected.
    """
    ticker = ticker.upper()
    contracts = int(contracts or config.LEAP_CONTRACTS)
    state = state if state is not None else log.load_state()
    meta = state.get("metadata", {})
    open_pos = [p for p in _open_positions(state) if p.get("ticker") != ticker]

    df = data_handler.get_daily(ticker)
    est = juice_estimate(ticker, df)
    leap_cost = leap_cost_per_share if leap_cost_per_share is not None else est["leap_cost_per_share"]
    weekly_extr = (weekly_extrinsic_per_share if weekly_extrinsic_per_share is not None
                   else est["weekly_extrinsic_per_share"])
    juice_source = "chain" if (leap_cost_per_share is not None
                               and weekly_extrinsic_per_share is not None) else "estimate"
    proposed_cost = leap_cost * contracts * 100 if leap_cost is not None else None

    checks = []

    # 1) Cash reserve: post-trade free cash >= 2xATR defensive reserve across
    #    the whole book including this position. operating_cash is the live
    #    Schwab balance when connected, else the stored manual fallback.
    cash_info = resolve_operating_cash(state)
    operating = cash_info["amount"]
    reserves = [_position_reserve(p) for p in open_pos]
    new_atr = indicators.atr(df) if df is not None else None
    reserves.append(config.RESERVE_ATR_MULT * new_atr * contracts * 100
                    if new_atr is not None else None)
    reserve_required = sum(r for r in reserves if r is not None) if reserves else 0.0
    free_after = operating - proposed_cost if proposed_cost is not None else None
    checks.append(_check(
        "cash_reserve",
        f"Post-trade cash ≥ {config.RESERVE_ATR_MULT:g}×ATR reserve (${reserve_required:,.0f})",
        free_after is not None and free_after >= reserve_required,
        True,
        {"operating_cash": operating, "operating_cash_source": cash_info["source"],
         "operating_cash_error": cash_info["error"], "proposed_cost": proposed_cost,
         "free_cash_after": round(free_after, 2) if free_after is not None else None,
         "reserve_required": round(reserve_required, 2),
         "reserve_incomplete": any(r is None for r in reserves)}))

    # 2) Position count + deployed-capital caps.
    n_open = len(open_pos)
    checks.append(_check(
        "position_limit", f"≤ {config.MAX_CFM_POSITIONS} concurrent CFM positions",
        n_open + 1 <= config.MAX_CFM_POSITIONS, True,
        {"open_positions": n_open, "max": config.MAX_CFM_POSITIONS}))

    import position_manager
    deployed = round(sum(position_manager.position_capital(p) for p in open_pos), 2)
    after = deployed + (proposed_cost or 0)
    checks.append(_check(
        "capital_limit", f"Deployed capital ≤ ${config.MAX_DEPLOYED_CAPITAL:,}",
        proposed_cost is not None and after <= config.MAX_DEPLOYED_CAPITAL, True,
        {"capital_deployed": deployed, "proposed_cost": proposed_cost,
         "after": round(after, 2), "max": config.MAX_DEPLOYED_CAPITAL}))

    # 2b) Round-lot SIZE-BLOCK (SHARES base, schema v20). A shares entry is round
    #     lots only; block any underlying whose single 100-share lot cost exceeds
    #     the per-position cap. No coded per-position dollar cap existed before the
    #     migration (only book-wide MAX_DEPLOYED_CAPITAL); PER_POSITION_CAP_USD is a
    #     PROPOSED_DEFAULT. Only appended for a SHARES entry — legacy callers keep
    #     the exact prior check set. Missing spot degrades to pass (never a false block).
    if position_type == position_types.SHARES:
        spot = est.get("stock_price") if isinstance(est, dict) else None
        lot_cost = round(float(spot) * config.SHARES_PER_LOT, 2) if spot else None
        checks.append(_check(
            "round_lot_size",
            f"100-share lot ≤ ${config.PER_POSITION_CAP_USD:,.0f} (SIZE-BLOCKED)",
            lot_cost is None or lot_cost <= config.PER_POSITION_CAP_USD, True,
            {"spot": spot, "shares_per_lot": config.SHARES_PER_LOT, "lot_cost": lot_cost,
             "per_position_cap": config.PER_POSITION_CAP_USD,
             "size_blocked": bool(lot_cost is not None and lot_cost > config.PER_POSITION_CAP_USD)}))

    # 3) Sector concentration — the filters funnel into the hottest sector.
    import sector_data
    sector = sector_data.sector_for(ticker) or ""
    same_sector = [p["ticker"] for p in open_pos if p.get("sector") == sector]
    checks.append(_check(
        "sector_concentration",
        f"≤ {config.MAX_POSITIONS_PER_SECTOR} position(s) in {sector or 'sector'}",
        len(same_sector) < config.MAX_POSITIONS_PER_SECTOR, True,
        {"sector": sector, "already_held": same_sector,
         "max": config.MAX_POSITIONS_PER_SECTOR}))

    # 4) Juice adequacy vs the underlying's profile bar: growth stocks use the
    #    CFM cycle target (~1.9%/wk); ETFs run a lower steady-income bar.
    is_etf_underlying = sector_data.is_etf(ticker)
    target = weekly_yield_target_pct(ticker)
    weekly_yield = (round(weekly_extr / leap_cost * 100, 2)
                    if (weekly_extr is not None and leap_cost) else None)
    profile_note = ("ETF income sleeve" if is_etf_underlying
                    else f"{config.CYCLE_RETURN_MIN * 100:g}% over {config.CYCLE_WEEKS_MAX} wks")
    checks.append(_check(
        "juice_adequacy",
        f"Weekly juice ≥ {target:g}% of LEAP cost ({profile_note})",
        weekly_yield is not None and weekly_yield >= target, True,
        {"weekly_yield_pct": weekly_yield, "target_pct": target,
         "weekly_extrinsic_per_share": weekly_extr, "leap_cost_per_share": leap_cost,
         "source": juice_source, "estimate": est, "is_etf": is_etf_underlying,
         "profile": "etf" if is_etf_underlying else "stock"}))

    # 4b) Juice too rich (warning): actual premium far above what the ticker's
    #     own realized vol implies = the market is pricing an event/risk.
    est_extr = est["weekly_extrinsic_per_share"]
    too_rich = (juice_source == "chain" and weekly_extr is not None and est_extr
                and weekly_extr > config.JUICE_RICH_FACTOR * est_extr)
    checks.append(_check(
        "juice_rich",
        f"Juice not > {config.JUICE_RICH_FACTOR:g}× history-implied (risk pricing)",
        not too_rich, False,
        {"weekly_extrinsic_per_share": weekly_extr,
         "history_implied_per_share": est_extr,
         "factor": config.JUICE_RICH_FACTOR}))

    # 5) Earnings inside the planned 4-8 week cycle. BLOCKING: the CFM rule is
    # "be out or really deep" before a report, so opening a fresh cycle over one
    # is a hard stop — overridable with a typed reason (e.g. deliberately
    # planning to roll deep-ITM into it), logged onto the execution like any
    # Level 5 override.
    try:
        earn = earnings.next_earnings(ticker)
    except Exception:  # noqa: BLE001
        earn = {"date": None, "days_until": None}
    cycle_days = config.CYCLE_WEEKS_MAX * 7
    inside = earn.get("days_until") is not None and 0 <= earn["days_until"] <= cycle_days
    checks.append(_check(
        "earnings_in_cycle", f"No earnings inside the {config.CYCLE_WEEKS_MAX}-week cycle",
        not inside, True, {"earnings": earn, "cycle_days": cycle_days}))

    # 6) Circuit breaker suggestion (storing one is enforced by the executor).
    cb = suggested_circuit_breaker(ticker, df)

    # 7) Dividend snapshot (feeds ASSIGNMENT_RISK once the position exists).
    try:
        div = dividends.next_dividend(ticker)
    except Exception:  # noqa: BLE001
        div = {"ex_date": None, "amount": None}

    # 7b) Ex-dividend inside the planned cycle (payers only). A short-call cycle
    #     straddling an ex-div carries early-assignment risk; WARN (not a hard
    #     block — PROPOSED_DEFAULT) so the operator plans to roll around it. The
    #     dividend sibling of earnings_in_cycle; only meaningful when an ex-date is
    #     known (non-payers and unknown dates simply pass).
    ex_date = div.get("ex_date") if isinstance(div, dict) else None
    ex_in_cycle, ex_days = False, None
    if ex_date:
        try:
            from datetime import date, datetime
            ex = datetime.strptime(str(ex_date)[:10], "%Y-%m-%d").date()
            ex_days = (ex - date.today()).days
            ex_in_cycle = 0 <= ex_days <= cycle_days
        except (TypeError, ValueError):
            ex_in_cycle = False
    checks.append(_check(
        "ex_div_in_cycle", f"No ex-dividend inside the {config.CYCLE_WEEKS_MAX}-week cycle",
        not ex_in_cycle, False,
        {"dividend": div, "ex_date": ex_date, "days_until": ex_days, "cycle_days": cycle_days}))

    blocking_failures = [c for c in checks if c["blocking"] and not c["pass"]]
    warnings = [c for c in checks if not c["blocking"] and not c["pass"]]
    return {
        "ticker": ticker,
        "level": 5,
        "name": "Account & Juice",
        "pass": not blocking_failures,
        "checks": checks,
        "blocking_failures": [c["id"] for c in blocking_failures],
        "warnings": [c["id"] for c in warnings],
        "suggested_circuit_breaker": cb,
        "dividend": div,
        "juice": {"weekly_yield_pct": weekly_yield, "target_pct": target,
                  "source": juice_source, "is_etf": is_etf_underlying,
                  "profile": "etf" if is_etf_underlying else "stock"},
        # Advisory sector-strength size suggestion (never enforced — the checks
        # above use the caller's `contracts`; this is a recommendation only).
        "sizing": sector_size_suggestion(ticker, contracts),
    }


def evaluate_many(tickers: list[str], contracts: int | None = None) -> dict[str, dict]:
    """Level 5 for many tickers against ONE shared account snapshot — state is
    loaded once (not once per ticker) and the live Schwab cash balance is
    resolved once and reused (schwab_api.cash_balance's own 60s cache makes
    this cheap regardless, but loading state.json per ticker isn't). For bulk
    screening (see GET /api/scan/ready), not per-trade execution — juice
    always uses the history-implied estimate here, never a live chain.
    """
    tickers = [t.strip().upper() for t in tickers if t.strip()]
    if not tickers:
        return {}
    state = log.load_state()
    data_handler.prefetch(tickers)  # warm the OHLCV cache in parallel first
    return {t: evaluate(t, contracts=contracts, state=state) for t in tickers}
