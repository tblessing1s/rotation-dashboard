"""Position-derived math: LEAP intrinsic/extrinsic, share-cap progress, and the
portfolio-level capital + milestone summary. Pure functions over a state dict.
"""
from __future__ import annotations

import config
import data_handler
import earnings


def _stock_price(ticker: str) -> float | None:
    q = data_handler.latest_quote(ticker)
    return q["price"] if q else None


def _live_short_marks(ticker: str, shorts: list[dict]) -> dict[tuple, float]:
    """Live per-share marks for a position's open shorts, keyed by (strike,
    expiration). One batched Schwab quote for all legs; best-effort — off-hours,
    in demo, without Schwab, or on any error it returns {} and callers fall back
    to the stored entry mark. Only legs carrying an expiration can be quoted."""
    import schwab_api
    if config.demo_enabled() or not schwab_api.configured():
        return {}
    syms: dict[str, tuple] = {}
    for sc in shorts:
        exp, strike = sc.get("expiration"), sc.get("strike")
        if exp and strike is not None:
            try:
                syms[schwab_api.occ_option_symbol(ticker, exp, float(strike), call=True)] = (strike, exp)
            except (TypeError, ValueError):
                continue
    if not syms:
        return {}
    try:
        quotes = data_handler.client().get_quotes(list(syms))
    except Exception:  # noqa: BLE001 — a marks fetch never blocks the positions view
        return {}
    out: dict[tuple, float] = {}
    for sym, key in syms.items():
        node = quotes.get(sym) or {}
        mark = node.get("mark")
        if mark is None:
            mark = node.get("bid")  # mid preferred; bid is the conservative fallback
        if mark is not None:
            out[key] = float(mark)
    return out


def enrich_leap(leap: dict, stock_price: float | None) -> dict:
    """Re-split a LEAP's current value into intrinsic/extrinsic.

    intrinsic = max(stock - strike, 0) * contracts * 100
    extrinsic = current option value - intrinsic
    Uses the stored current_bid (per-contract total) when present; otherwise
    leaves the stored values untouched.
    """
    out = dict(leap)
    strike = leap.get("strike")
    contracts = int(leap.get("contracts") or 0)
    if strike is not None and stock_price is not None and contracts:
        intrinsic = max(stock_price - strike, 0.0) * contracts * 100
        out["intrinsic"] = round(intrinsic, 2)
        current = leap.get("current_bid")
        if current is not None:
            out["extrinsic"] = round(float(current) - intrinsic, 2)
    return out


def enrich_short(sc: dict, stock_price: float | None, dividend: dict | None,
                 live_mark: float | None = None, today=None) -> dict:
    """Per-short management signals, all derived from stored execution data:

    - decay_pct + roll_now: the 75% buyback rule (HARD_CFM_RULE — when the short
      has surrendered >=75% of its sale premium with >2 DTE, roll early).
    - extrinsic capture: what extrinsic we sold at entry (the target to capture),
      what's left in the short now, and the % captured so far. An ITM weekly's
      premium is intrinsic (tracks the stock) + extrinsic (the theta we're here
      to collect); isolating the extrinsic is the honest "how much juice left."
    - intrinsic capture: the other half of an ITM sale — the intrinsic banked at
      entry (sold - extrinsic) that has since melted back to us as the stock fell
      toward/under the strike. Signed cash: positive kept, negative handed back
      (a climb hands it back but lifts the covering LEAP's intrinsic to match).
    - below_strike: the DEFEND trigger (stock closed under the short strike).
    - assignment_risk: extrinsic below the coming dividend before ex-div. The
      short is covered by a LEAP, NOT stock — assignment creates SHORT STOCK
      that owes the dividend, so the standard play is to roll before ex-div.

    ``live_mark`` (per share), when supplied by the caller from a fresh quote,
    overrides the stored entry mark so decay + extrinsic capture read live.
    """
    out = dict(sc)
    contracts = int(sc.get("contracts") or 0)
    sold = (float(sc["entry_premium_total"]) / (contracts * 100)
            if sc.get("entry_premium_total") and contracts else None)
    current = live_mark if live_mark is not None else sc.get("current_bid")
    out["current_bid"] = current
    out["sold_per_share"] = round(sold, 2) if sold else None
    decay = (1 - float(current) / sold) if (sold and current is not None) else None
    out["decay_pct"] = round(decay * 100, 1) if decay is not None else None
    strike = sc.get("strike")

    # Extrinsic capture: the extrinsic sold at entry is the target; what's left in
    # the short now is its current mark minus its current intrinsic; captured is
    # the difference. All best-effort — a missing mark leaves the live fields None
    # but always keeps the entry target visible.
    entry_extrinsic = sc.get("entry_extrinsic_per_share")
    entry_extrinsic = float(entry_extrinsic) if entry_extrinsic is not None else None
    intrinsic_now = (max(float(stock_price) - float(strike), 0.0)
                     if stock_price is not None and strike is not None else None)
    current_extrinsic = (max(float(current) - intrinsic_now, 0.0)
                         if current is not None and intrinsic_now is not None else None)
    captured = (max(entry_extrinsic - current_extrinsic, 0.0)
                if entry_extrinsic is not None and current_extrinsic is not None else None)
    captured_pct = (min(max(captured / entry_extrinsic * 100, 0.0), 100.0)
                    if captured is not None and entry_extrinsic else None)
    # SIGNED raw capture — the SAME arithmetic without the floor/clamp. The
    # payout/accounting figures above stay clamped (an IV spike must never book as
    # negative income). But the floor pins captured_pct at 0 when the short's
    # extrinsic has risen ABOVE entry (vol spike → the leg moved against you), which
    # hides an underwater short at defend-decision time. The raw figure (may be < 0,
    # may exceed 100) and the extrinsic_above_entry flag make that visible on the
    # management view without touching any payout number. [CAPTURE_CLAMP_SCOPE]
    captured_raw = (entry_extrinsic - current_extrinsic
                    if entry_extrinsic is not None and current_extrinsic is not None else None)
    captured_pct_raw = (captured_raw / entry_extrinsic * 100
                        if captured_raw is not None and entry_extrinsic else None)
    extrinsic_above_entry = bool(entry_extrinsic is not None and current_extrinsic is not None
                                 and current_extrinsic > entry_extrinsic)
    out["entry_extrinsic_per_share"] = round(entry_extrinsic, 2) if entry_extrinsic is not None else None
    out["current_extrinsic_per_share"] = round(current_extrinsic, 2) if current_extrinsic is not None else None
    out["extrinsic_captured_per_share"] = round(captured, 2) if captured is not None else None
    out["extrinsic_captured_pct"] = round(captured_pct, 1) if captured_pct is not None else None
    out["extrinsic_captured_pct_raw"] = round(captured_pct_raw, 1) if captured_pct_raw is not None else None
    out["extrinsic_above_entry"] = extrinsic_above_entry
    mult = contracts * 100
    out["entry_extrinsic_total"] = round(entry_extrinsic * mult, 2) if entry_extrinsic is not None and mult else None
    out["extrinsic_captured_total"] = round(captured * mult, 2) if captured is not None and mult else None
    out["extrinsic_remaining_total"] = round(current_extrinsic * mult, 2) if current_extrinsic is not None and mult else None

    # Intrinsic capture: an ITM short is sold for intrinsic + extrinsic, and the
    # intrinsic is real cash banked at entry. Unlike extrinsic (theta we're here to
    # collect), the intrinsic tracks the stock: it melts back to us when the stock
    # falls toward/under the strike, and is handed back when the stock climbs — but
    # a climb lifts the covering LEAP's intrinsic to match, so the short-side loss
    # is a hedge, not a leak. entry intrinsic = what we sold beyond the extrinsic
    # (sold - entry_extrinsic); captured = entry intrinsic that's no longer owed
    # (entry - current). SIGNED: positive = cash kept, negative = handed back.
    entry_intrinsic = (max(sold - entry_extrinsic, 0.0)
                       if sold is not None and entry_extrinsic is not None else None)
    intrinsic_captured = (entry_intrinsic - intrinsic_now
                          if entry_intrinsic is not None and intrinsic_now is not None else None)
    out["entry_intrinsic_per_share"] = round(entry_intrinsic, 2) if entry_intrinsic is not None else None
    out["current_intrinsic_per_share"] = round(intrinsic_now, 2) if intrinsic_now is not None else None
    out["intrinsic_captured_per_share"] = round(intrinsic_captured, 2) if intrinsic_captured is not None else None
    out["entry_intrinsic_total"] = round(entry_intrinsic * mult, 2) if entry_intrinsic is not None and mult else None
    out["intrinsic_captured_total"] = round(intrinsic_captured * mult, 2) if intrinsic_captured is not None and mult else None
    # The short's live intrinsic liability — what this leg owes right now, to weigh
    # against the covering LEAP's intrinsic (the hedge-balance check on the book).
    out["current_intrinsic_total"] = round(intrinsic_now * mult, 2) if intrinsic_now is not None and mult else None

    dte = sc.get("dte")
    out["roll_now"] = bool(decay is not None and decay >= config.BUYBACK_DECAY_PCT
                           and dte is not None and dte > config.BUYBACK_MIN_DTE)
    out["below_strike"] = bool(stock_price is not None and strike is not None
                               and stock_price < float(strike))

    # Assignment risk is an EXTRINSIC problem: an ITM short whose time value has
    # collapsed to ~0 is assignable any time (base trigger); a dividend the
    # extrinsic no longer covers before ex-div is an escalation of it. Dividend
    # escalation is preferred when both apply.
    out["assignment_risk"] = None
    itm = stock_price is not None and strike is not None and float(stock_price) > float(strike)
    ex_date, amount = (dividend or {}).get("ex_date"), (dividend or {}).get("amount")
    if ex_date and amount and current is not None and strike is not None and stock_price is not None:
        from datetime import date, datetime, timedelta
        today = today or date.today()
        try:
            ex = datetime.strptime(str(ex_date)[:10], "%Y-%m-%d").date()
            expiry = (datetime.strptime(str(sc["expiration"])[:10], "%Y-%m-%d").date()
                      if sc.get("expiration")
                      else today + timedelta(days=int(dte)) if dte is not None else None)
            extrinsic = max(float(current) - max(stock_price - float(strike), 0.0), 0.0)
            if expiry and today <= ex <= expiry and extrinsic < float(amount):
                out["assignment_risk"] = {
                    "trigger": "dividend",
                    "extrinsic": round(extrinsic, 2), "dividend": float(amount),
                    "ex_date": ex_date,
                    "note": ("Extrinsic below the dividend before ex-div — early assignment "
                             "likely. The short is covered by a LEAP, not stock: assignment "
                             "creates SHORT STOCK that owes the dividend. Roll before ex-div "
                             "(or accept the assignment mechanics deliberately)."),
                }
        except (TypeError, ValueError):
            pass
    if (out["assignment_risk"] is None and itm and current is not None
            and current_extrinsic is not None and dte is not None and int(dte) > 0
            and current_extrinsic < config.ASSIGNMENT_EXTRINSIC_FLOOR):
        out["assignment_risk"] = {
            "trigger": "extrinsic",
            "extrinsic": round(current_extrinsic, 2),
            "floor": config.ASSIGNMENT_EXTRINSIC_FLOOR,
            "note": ("Extrinsic has collapsed below a few cents while deep ITM — assignable "
                     "any time, no ex-div required. Roll the short up/out to re-establish "
                     "time value. The short is covered by a LEAP, not stock: never exercise "
                     "the LEAP to cover an assignment."),
        }
    return out


def delta_coverage(position: dict, price: float | None, q: float = 0.0) -> dict:
    """The delta-coverage guardrail as a PURE function over a position dict, a
    stock price, and a continuous dividend yield — the single decision core
    shared by alerts.check_delta_uncovered and the recommendation engine (which
    feed it live vs frozen-snapshot inputs respectively).

    Two independent checks [HARD_CFM_RULE]:
      - floor: the weakest LEAP leg's delta below config.LEAP_DELTA_FLOOR — the
        long no longer tracks the stock;
      - inverted: the shorts' contract-weighted delta exceeding the longs' —
        the diagonal is net-short deltas.
    Greeks recomputed per leg via indicators.call_greeks (pure math, q-aware).
    Returns None-valued fields when no long leg is priceable."""
    import indicators
    import logging_handler as log
    long_total, long_contracts, min_leg_delta = 0.0, 0, None
    for leg in log.leap_legs(position):
        n = int(leg.get("contracts") or 0)
        if not n:
            continue
        leg_mark = (float(leg["current_bid"]) / (n * 100)
                    if leg.get("current_bid") is not None else None)
        d, _ = indicators.call_greeks(price, leg.get("strike"), leg.get("dte"), leg_mark, q=q)
        if d is None:
            continue
        long_total += d * n
        long_contracts += n
        min_leg_delta = d if min_leg_delta is None else min(min_leg_delta, d)
    short_total = 0.0
    priced_shorts = False
    for sc in position.get("short_calls", []):
        sd, _ = indicators.call_greeks(price, sc.get("strike"), sc.get("dte"),
                                       sc.get("current_bid"), q=q)
        if sd is not None:
            priced_shorts = True
            short_total += sd * int(sc.get("contracts") or 0)
    assessable = min_leg_delta is not None
    return {
        "assessable": assessable,
        "min_leg_delta": min_leg_delta,
        "long_delta": round(long_total, 4) if assessable else None,
        "long_contracts": long_contracts,
        "short_delta": round(short_total, 4) if priced_shorts else None,
        "floor": config.LEAP_DELTA_FLOOR,
        "floor_breach": bool(assessable and min_leg_delta < config.LEAP_DELTA_FLOOR),
        "inverted": bool(assessable and position.get("short_calls")
                         and short_total > long_total + 1e-9),
    }


def _parse_day(value):
    from datetime import datetime
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def whipsaw_status(position: dict, rolls: list[dict] | None = None,
                   today=None) -> dict:
    """The cumulative defend-whipsaw guard for one position, derived from its
    roll-ledger entries. Trips when EITHER too many defensive (reason="defend")
    rolls landed in the trailing WHIPSAW_WINDOW_WEEKS, OR cumulative roll drag
    (debits paid) has passed WHIPSAW_DRAG_PCT of the position's capital. Scoped to
    the current cycle (rolls on/after the position's entry_date, when known) so a
    prior cycle's rolls don't bleed in. Pure — no market data."""
    from datetime import date, timedelta
    today = today or date.today()
    ticker = position.get("ticker", "")
    rolls = [r for r in (rolls or []) if r.get("ticker") == ticker]
    entry = _parse_day(position.get("entry_date"))
    if entry:
        rolls = [r for r in rolls if (_parse_day(r.get("date")) or today) >= entry]

    window_start = today - timedelta(weeks=config.WHIPSAW_WINDOW_WEEKS)
    defends = [r for r in rolls if r.get("reason") == "defend"
               and (_parse_day(r.get("date")) or today) >= window_start]
    n_def = len(defends)
    drag = round(sum(r["net"] for r in rolls
                     if r.get("net") is not None and r["net"] < 0), 2)
    capital = position_capital(position)
    drag_pct = round(abs(drag) / capital * 100, 1) if capital else None

    rolls_trip = n_def >= config.WHIPSAW_DEFEND_ROLLS
    drag_trip = drag_pct is not None and drag_pct >= config.WHIPSAW_DRAG_PCT * 100
    reasons = []
    if rolls_trip:
        reasons.append(f"{n_def} defensive rolls in {config.WHIPSAW_WINDOW_WEEKS}wk")
    if drag_trip:
        reasons.append(f"cumulative roll drag ${abs(drag):,.0f} = {drag_pct:g}% "
                       f"of ${capital:,.0f} capital")
    return {
        "tripped": rolls_trip or drag_trip,
        "defensive_rolls": n_def,
        "window_weeks": config.WHIPSAW_WINDOW_WEEKS,
        "defend_roll_threshold": config.WHIPSAW_DEFEND_ROLLS,
        "roll_drag": drag,
        "drag_pct": drag_pct,
        "drag_pct_threshold": round(config.WHIPSAW_DRAG_PCT * 100, 1),
        "position_capital": capital,
        "rolls_trip": rolls_trip,
        "drag_trip": drag_trip,
        "reasons": reasons,
    }


def enrich_position(position: dict, roll_summary: dict | None = None,
                    rolls: list[dict] | None = None) -> dict:
    out = dict(position)
    ticker = position.get("ticker", "")
    price = _stock_price(ticker)
    out["stock_price"] = price
    import logging_handler as log
    legs = log.leap_legs(position)
    if legs:
        enriched_legs = [enrich_leap(l, price) for l in legs]
        out["leap_legs"] = enriched_legs
        out["leap"] = enriched_legs[0]

        # Ticker-level totals across every leg — what the high-level views (the
        # juice stand's orange, capital summaries) aggregate on. None-safe: a
        # sum only exists when at least one leg carries the field.
        def _sum(key):
            vals = [l.get(key) for l in enriched_legs if l.get(key) is not None]
            return round(sum(float(v) for v in vals), 2) if vals else None
        out["leap_totals"] = {
            "legs": len(enriched_legs),
            "contracts": sum(int(l.get("contracts") or 0) for l in enriched_legs),
            "cost_basis": _sum("cost_basis"),
            "current_value": _sum("current_bid"),
            "intrinsic": _sum("intrinsic"),
            "extrinsic": _sum("extrinsic"),
        }

        # LEAP long-leg health: DTE, extrinsic runway, juice-vs-burn, delta
        # velocity, and the roll recommendation (Task 1-3). Best-effort — a
        # pricing gap degrades to Nones, never blanks the position. Multi-leg
        # engines also get per-leg health + the aggregated verdict.
        try:
            import leap_policy
            # Dividend-adjusted burn/roll-runway: the stored burn marks already use
            # q (maintenance sweep), so the live card must too or the two disagree
            # on the LEAP roll-timing decision. q=0 when no dividend data. [R3]
            import dividends
            q = dividends.yield_for(position.get("ticker", ""))
            out["leap_health"] = leap_policy.leap_health(position, stock_price=price, q=q)
            if len(legs) > 1:
                per_leg = [leap_policy.leap_health(position, stock_price=price, q=q, leg=l)
                           for l in legs]
                out["leap_health_legs"] = per_leg
                out["leap_health_agg"] = leap_policy.aggregate_health(per_leg)
        except Exception:  # noqa: BLE001 — health is informational, never block positions
            out["leap_health"] = None
    else:
        out["leap_legs"] = []
        out["leap_totals"] = None
    dividend = position.get("dividend")
    shorts = position.get("short_calls", [])
    marks = _live_short_marks(ticker, shorts)
    out["short_calls"] = [
        enrich_short(sc, price, dividend,
                     live_mark=marks.get((sc.get("strike"), sc.get("expiration"))))
        for sc in shorts]
    out["defend"] = any(sc["below_strike"] for sc in out["short_calls"])
    out["roll_summary"] = roll_summary or {"count": 0, "net_total": 0.0, "drag_total": 0.0}
    # Whipsaw circuit breaker: too many defensive rolls / too much cumulative drag
    # -> exit, not another defend (the roll-down spiral no single check owns).
    out["whipsaw"] = whipsaw_status(position, rolls)
    # Price circuit breaker: 15% drop / 3 closes below the 50-day MA / close below
    # the 200-day MA / operator line — whichever trips first (circuit_breaker.py).
    if position.get("status") != "closed":
        try:
            import circuit_breaker
            out["circuit_breaker_status"] = circuit_breaker.evaluate(position)
        except Exception:  # noqa: BLE001 — the breaker view is informational, never block positions
            out["circuit_breaker_status"] = None
    shares = dict(position.get("shares") or {})
    count = int(shares.get("count") or 0)
    cap = int(shares.get("cap") or config.SHARE_CAP)
    shares["cap"] = cap
    shares["pct_to_cap"] = round(count / cap * 100, 1) if cap else 0
    shares["locked"] = count >= cap
    # Accumulation-vs-kill-switch guard (config flag; see can_add_shares).
    if config.BLOCK_ACCUMULATION_ON_RS_DETERIORATION:
        blocked, why = _accumulation_block(ticker)
        shares["accumulation_blocked"] = blocked
        shares["accumulation_block_reason"] = why
    out["shares"] = shares
    try:
        out["earnings"] = earnings.next_earnings(ticker)
    except Exception:  # noqa: BLE001 — earnings is informational, never block positions
        out["earnings"] = {"ticker": ticker, "date": None, "days_until": None,
                           "warning": False, "source": "error"}
    return out


def positions_view(state: dict) -> list[dict]:
    roll_ledger = state.get("roll_ledger") or {}
    by_ticker = roll_ledger.get("by_ticker", {})
    all_rolls = roll_ledger.get("rolls", [])
    out = [enrich_position(p, by_ticker.get(p.get("ticker", "")), rolls=all_rolls)
           for p in state.get("positions", [])]
    # Wash-sale visibility on OPEN positions: the cycle derivation marks a
    # loss-closing cycle "flagged" when the underlying is re-entered inside the
    # window — carry that onto the currently open position for the same name.
    flagged: dict[str, dict] = {}
    for c in state.get("cycles", []):
        ws = c.get("wash_sale")
        if ws and ws.get("status") == "flagged":
            flagged[c["ticker"]] = {"loss_exit_date": c.get("exit_date"),
                                    "loss": ws.get("loss"),
                                    "note": "Re-entry within 30 days of a loss exit "
                                            "— wash-sale rules likely defer the loss."}
    for p in out:
        p["wash_sale_flag"] = (flagged.get(p.get("ticker", ""))
                               if p.get("status") != "closed" else None)
    return out


def position_capital(p: dict) -> float:
    """Capital deployed in one position: every LEAP leg's cost basis plus any
    accumulated shares (count x cost basis per share). The buy executions set
    these on the position, so this is the source of truth."""
    import logging_handler as log
    total = sum(float(l.get("cost_basis") or 0) for l in log.leap_legs(p))
    shares = p.get("shares") or {}
    count = int(shares.get("count") or 0)
    cps = shares.get("cost_basis_per_share")
    if count and cps is not None:
        total += float(cps) * count
    return round(total, 2)


def deployed_capital(state: dict) -> float:
    """Total capital deployed across all OPEN positions, derived from their LEAP
    cost bases + shares. Derived (never a hand-maintained metadata figure) so it
    reflects the book the moment a LEAP is bought — the same principle as the
    theta ledger and payback meters."""
    return round(sum(position_capital(p) for p in state.get("positions", [])
                     if p.get("status") != "closed"), 2)


def capital_summary(state: dict) -> dict:
    meta = state.get("metadata", {})
    deployed = deployed_capital(state)
    reserve = float(meta.get("reserve_required") or config.RESERVE_REQUIRED)
    # Live Schwab balance when connected (also persists back to state.metadata
    # so this stays the single source other readers agree on); manual entry
    # is the fallback in demo mode, when Schwab isn't connected, or on error.
    import account_gate
    cash_info = account_gate.resolve_operating_cash(state)
    operating = cash_info["amount"]
    ytd = float(state.get("theta_ledger", {}).get("totals", {}).get("ytd") or 0)
    monthly = float(state.get("theta_ledger", {}).get("totals", {}).get("this_month") or 0)
    # Deploy capacity ("dry powder"): the honest headline is how much MORE capital
    # I can put to work right now, which is the tighter of two ceilings — the
    # deployed-capital cap and the cash that sits above the defensive reserve.
    # Both formulas live here (server-side, single source) rather than in the UI,
    # same principle as the ledger/payback meters. The caps themselves are the
    # HARD_CFM_RULE / PROPOSED_DEFAULT figures from config.
    open_positions = sum(1 for p in state.get("positions", [])
                         if p.get("status") != "closed")
    capital_headroom = round(max(0.0, config.MAX_DEPLOYED_CAPITAL - deployed), 2)
    cash_above_reserve = round(max(0.0, operating - reserve), 2)
    deployable = round(min(capital_headroom, cash_above_reserve), 2)
    slots_open = max(0, config.MAX_CFM_POSITIONS - open_positions)
    return {
        "capital_deployed": deployed,
        "reserve_required": reserve,
        "operating_cash": operating,
        "operating_cash_source": cash_info["source"],
        "operating_cash_error": cash_info["error"],
        "reserve_ok": operating >= reserve or reserve == 0,
        "max_deployed": config.MAX_DEPLOYED_CAPITAL,
        "max_positions": config.MAX_CFM_POSITIONS,
        "open_positions": open_positions,
        "capital_headroom": capital_headroom,
        "cash_above_reserve": cash_above_reserve,
        "deployable": deployable,
        "slots_open": slots_open,
        "milestones": {
            "half_nut": {
                "target": config.MILESTONE_HALF_NUT,
                "current": monthly,
                "pct": round(monthly / config.MILESTONE_HALF_NUT * 100, 1) if config.MILESTONE_HALF_NUT else 0,
            },
            "quit_safe": {
                "target": config.MILESTONE_QUIT_SAFE,
                "current": monthly,
                "pct": round(monthly / config.MILESTONE_QUIT_SAFE * 100, 1) if config.MILESTONE_QUIT_SAFE else 0,
            },
        },
        "juice_ytd": ytd,
    }


def net_juice_rollup(positions: list[dict]) -> dict:
    """Portfolio income rollup on NET juice/week (juice collected - LEAP theta
    burn with slippage), summed across open positions — NEVER gross (spec §6,
    NET_JUICE_IS_HEADLINE). Reads the already-enriched per-position leap_health
    (multi-leg positions use the aggregated block). Each component sums only over
    positions that carry it, so a single unpriceable name never blanks the total."""
    gross = burn_wk = net = 0.0
    have_gross = have_burn = have_net = False
    counted = 0
    for p in positions or []:
        if p.get("status") == "closed":
            continue
        h = p.get("leap_health_agg") or p.get("leap_health") or {}
        g = h.get("trailing_avg_weekly_juice")
        b = h.get("model_burn_per_week")
        n = h.get("net_juice_per_week")
        if g is not None:
            gross += float(g); have_gross = True
        if b is not None:
            burn_wk += float(b); have_burn = True
        if n is not None:
            net += float(n); have_net = True
        if g is not None or b is not None or n is not None:
            counted += 1
    return {
        "gross_juice_per_week": round(gross, 2) if have_gross else None,
        "burn_per_week": round(burn_wk, 2) if have_burn else None,
        "net_juice_per_week": round(net, 2) if have_net else None,
        "positions_counted": counted,
    }


def _accumulation_block(ticker: str) -> tuple[bool, str | None]:
    """Kill-switch / RS3M-deterioration guard for share accumulation. Returns
    (blocked, reason). Any non-green kill-switch read blocks: red is an exit in
    progress; yellow (CAUTION) means RS is thinning toward the kill line — the
    pullback-accumulation play must not add to a name the strategy is about to
    leave."""
    try:
        import kill_switch
        ev = kill_switch.evaluate(ticker)
    except Exception:  # noqa: BLE001 — no data, no verdict: don't block on error
        return False, None
    if ev.get("status") in ("red", "yellow"):
        return True, (f"kill-switch {ev['status'].upper()} — RS3M vs SPY "
                      f"{ev.get('rs3m_vs_spy')}, vs Sector {ev.get('rs3m_vs_sector')}")
    return False, None


def can_add_shares(state: dict, ticker: str) -> bool:
    """A position can accumulate more shares only until it hits the 500 cap —
    and, when BLOCK_ACCUMULATION_ON_RS_DETERIORATION is on, only while the
    kill switch reads green for the name."""
    from logging_handler import find_position
    if config.BLOCK_ACCUMULATION_ON_RS_DETERIORATION and _accumulation_block(ticker)[0]:
        return False
    p = find_position(state, ticker)
    if not p:
        return True
    shares = p.get("shares") or {}
    return int(shares.get("count") or 0) < int(shares.get("cap") or config.SHARE_CAP)
