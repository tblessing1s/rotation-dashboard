"""Position-derived math: LEAP intrinsic/extrinsic, share-cap progress, and the
portfolio-level capital + milestone summary. Pure functions over a state dict.
"""
from __future__ import annotations

import config
import data_handler
import earnings
import position_types


def _stock_price(ticker: str) -> float | None:
    q = data_handler.latest_quote(ticker)
    return q["price"] if q else None


def _live_short_marks(ticker: str, shorts: list[dict]) -> dict[tuple, float]:
    """Live per-share marks for a position's open shorts, keyed by (strike,
    expiration). One batched Schwab quote for all legs; best-effort — off-hours,
    in demo, without Schwab, or on any error it returns {} and callers fall back
    to the stored entry mark. Only legs carrying an expiration can be quoted."""
    import schwab_api
    if config.demo_enabled() or not schwab_api.market_configured():
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


def leap_cost_suspect(leg: dict, stock_price: float | None) -> bool:
    """True when a LEAP leg's ``cost_basis`` looks stored PER SHARE instead of the
    full per-contract-total dollars (a ~100× understatement — e.g. 53.05 where
    5305 was meant). A LEAP's per-contract cost can NEVER be below its intrinsic
    (you can't pay less than the in-the-money value), so a cost that sits under
    the intrinsic at entry — or ~20× under the live intrinsic — is a mis-scale,
    not a cheap buy. Purely a shape check on stored numbers; no live quote needed
    for the entry test. Powers the display guard + the one-click repair."""
    try:
        contracts = int(leg.get("contracts") or 0)
        strike = float(leg.get("strike"))
    except (TypeError, ValueError):
        return False
    if not contracts:
        return False
    try:
        cost_pc = float(leg.get("cost_basis") or 0) / contracts
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    if cost_pc <= 0:
        return False
    entry = leg.get("entry_stock_price")
    if entry not in (None, ""):
        try:
            entry_intrinsic_pc = max(float(entry) - strike, 0.0) * 100
            # Paid materially less than intrinsic → physically impossible.
            if entry_intrinsic_pc > cost_pc * 1.05:
                return True
        except (TypeError, ValueError):
            pass
    if stock_price is not None:
        cur_intrinsic_pc = max(float(stock_price) - strike, 0.0) * 100
        # Live intrinsic 20× the recorded cost → cost is off by ~100×, not a win.
        if cur_intrinsic_pc > cost_pc * 20:
            return True
    return False


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
    # Flag a per-share-scaled cost basis so the UI can warn + offer a repair
    # instead of rendering an absurd intrinsic-vs-cost ratio (e.g. 8,584%).
    out["cost_basis_suspect"] = leap_cost_suspect(leap, stock_price)
    return out


def strike_gap(stock_price: float | None, strike, put: bool = False) -> dict:
    """Where spot sits relative to ONE leg's strike.

    The single derivation both leg types use, so a call and a put can never
    disagree about which side of the strike is in the money. ``distance`` is
    always signed the same way — spot minus strike, positive when spot is ABOVE
    it — and ``itm`` is what inverts: a short call is in the money above its
    strike, a short put below.

    ``distance_pct`` is a percentage OF SPOT, i.e. how far the stock has to move
    from here to reach the strike, which is the question being asked when an
    operator looks at a short leg.
    """
    if stock_price is None or strike in (None, ""):
        return {"stock_price": None, "distance": None, "distance_pct": None,
                "itm": None, "moneyness": None}
    spot = float(stock_price)
    k = float(strike)
    distance = spot - k
    itm = (spot < k) if put else (spot > k)
    return {
        "stock_price": round(spot, 2),
        "distance": round(distance, 2),
        "distance_pct": round(distance / spot * 100, 2) if spot else None,
        "itm": bool(itm),
        "moneyness": "ATM" if distance == 0 else ("ITM" if itm else "OTM"),
    }


def enrich_short(sc: dict, stock_price: float | None, dividend: dict | None,
                 live_mark: float | None = None, today=None,
                 position_type: str | None = None) -> dict:
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

    # Spot vs this leg's strike — the reading an operator makes first on a short
    # call ("how far am I from being called away"), carried on the leg so the card
    # states it instead of leaving it to be inferred from the share block.
    gap = strike_gap(stock_price, strike, put=False)
    out["stock_price"] = gap["stock_price"]
    out["strike_distance"] = gap["distance"]
    out["strike_distance_pct"] = gap["distance_pct"]
    out["itm"] = gap["itm"]
    out["moneyness"] = gap["moneyness"]

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
                import position_types as _pt
                if position_type == _pt.SHARES:
                    note = ("Extrinsic below the dividend before ex-div — early assignment "
                            "possible. The short is covered by REAL SHARES: assignment is a "
                            "clean called-away delivery of your owned shares at the strike "
                            "(the planned exit) — no synthetic short stock, no dividend "
                            "liability. Roll only if you want to keep the shares.")
                else:
                    note = ("Extrinsic below the dividend before ex-div — early assignment "
                            "likely. The short is covered by a LEAP, not stock: assignment "
                            "creates SHORT STOCK that owes the dividend. Roll before ex-div "
                            "(or accept the assignment mechanics deliberately).")
                out["assignment_risk"] = {
                    "trigger": "dividend",
                    "extrinsic": round(extrinsic, 2), "dividend": float(amount),
                    "ex_date": ex_date, "note": note,
                }
        except (TypeError, ValueError):
            pass
    if (out["assignment_risk"] is None and itm and current is not None
            and current_extrinsic is not None and dte is not None and int(dte) > 0
            and current_extrinsic < config.ASSIGNMENT_EXTRINSIC_FLOOR):
        import position_types as _pt
        if position_type == _pt.SHARES:
            note = ("Extrinsic has collapsed below a few cents while deep ITM — assignable "
                    "any time, no ex-div required. The short is covered by REAL SHARES: "
                    "assignment simply delivers your owned shares at the strike (a clean "
                    "called-away exit). Roll the short up/out only if you want to keep them.")
        else:
            note = ("Extrinsic has collapsed below a few cents while deep ITM — assignable "
                    "any time, no ex-div required. Roll the short up/out to re-establish "
                    "time value. The short is covered by a LEAP, not stock: never exercise "
                    "the LEAP to cover an assignment.")
        out["assignment_risk"] = {
            "trigger": "extrinsic",
            "extrinsic": round(current_extrinsic, 2),
            "floor": config.ASSIGNMENT_EXTRINSIC_FLOOR,
            "note": note,
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
    import position_types
    # CASH-SECURED PUT: coverage is UNDEFINED, not zero. A put holds no base leg
    # to cover anything with, so "how many lots does the long cover" is not a
    # question with a numeric answer — and `covered_lots(0)` would cheerfully
    # return `coverable_lots: 0`, a confident answer to a question that should not
    # have been asked. Every field is None and `assessable` is False so a reader
    # renders N/A rather than a zero that looks like a real reading.
    if position_types.is_put(position):
        return {
            "assessable": False,
            "not_applicable": True,
            "reason": "coverage is undefined for a cash-secured put (no base leg)",
            "position_type": position_types.CASH_SECURED_PUT,
            "coverable_lots": None, "fragment_shares": None,
            "short_contracts": None, "min_leg_delta": None,
            "long_delta": None, "long_contracts": None, "short_delta": None,
            "floor": None, "floor_breach": None, "inverted": None,
            "naked_short": None, "status": None,
        }
    # SHARES base: coverage is a literal covered-LOT count, not a Greek. A shares
    # delta is permanently 1.0, so the LEAP floor/inversion delta checks can never
    # legitimately fire; the real guardrail is floor(shares/100) >= total short
    # contracts. ``inverted``/``naked_short`` fires when the shorts exceed the
    # coverable lots (a naked short — HARD_CFM_RULE: fragments never coverable).
    if position_types.is_shares(position):
        cl = covered_lots(int((position.get("shares") or {}).get("count") or 0))
        lots = cl["coverable_lots"]
        short_contracts = sum(int(sc.get("contracts") or 0)
                              for sc in position.get("short_calls", []))
        return {
            "assessable": True,
            "position_type": position_types.SHARES,
            "coverable_lots": lots,
            "fragment_shares": cl["fragment_shares"],
            "short_contracts": short_contracts,
            "min_leg_delta": 1.0,
            "long_delta": round(float(lots), 4),
            "long_contracts": lots,
            "short_delta": round(float(short_contracts), 4) if position.get("short_calls") else None,
            "floor": config.LEAP_DELTA_FLOOR,
            "floor_breach": False,   # delta 1.0 — the LEAP floor is unreachable
            "inverted": bool(short_contracts > lots),
            "naked_short": bool(short_contracts > lots),
        }
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
            # True when any leg's cost basis is mis-scaled (stored per-share), so
            # the aggregate ratio the orange shows would read absurdly high.
            "cost_basis_suspect": any(l.get("cost_basis_suspect") for l in enriched_legs),
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
    ptype = position_types.of(position)
    out["short_calls"] = [
        enrich_short(sc, price, dividend,
                     live_mark=marks.get((sc.get("strike"), sc.get("expiration"))),
                     position_type=ptype)
        for sc in shorts]
    out["defend"] = any(sc["below_strike"] for sc in out["short_calls"])
    # Coverage verdict on the position view (schema v21). For a SHARES base this is
    # the hard guardrail the Positions UI reads: short contracts may never exceed
    # floor(shares/100) coverable lots, and ``naked_short`` says when they do. The
    # SAME pure core the alert sweep and the recommendation engine call, so the
    # panel and the alert can never disagree. Best-effort — an unpriceable leg
    # degrades to None, never blanks the position.
    try:
        import dividends as _div
        # A SHARES base never reaches the Greeks (its coverage is a lot count), so
        # q only matters for a legacy long leg — resolve it defensively, same as
        # the alert sweep does, and fall back to 0 when there's no dividend data.
        out["coverage"] = delta_coverage(position, price, q=_div.yield_for(ticker) or 0.0)
    except Exception:  # noqa: BLE001 — a display readout never sinks the position
        out["coverage"] = None
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
        # Symbol Genius on the held name — a SECOND consumer of the same per-name
        # four-light engine the scan uses (its fourth light is SMA50>SMA200). A held
        # position slipping to YELLOW (exactly 3 green) or RED is an early structural
        # warning that complements the RS-based kill switch. Informational only —
        # never closes or blocks a position (the kill switch / circuit breaker own
        # exits); the same compute path as the scan, so the two never disagree.
        try:
            import data_handler
            import symbol_genius
            sg = symbol_genius.compute(data_handler.get_daily(ticker))
            out["symbol_genius"] = {"color": sg["color"], "greens": sg["greens"],
                                    "insufficient": sg["insufficient"], "lights": sg["lights"]}
        except Exception:  # noqa: BLE001 — informational, never block positions
            out["symbol_genius"] = None
    # ---- Open short puts (schema v22) + the share readouts they make undefined --
    open_puts = []
    for sp in position.get("short_puts") or []:
        leg = dict(sp)
        strike = float(leg.get("strike") or 0)
        n = int(leg.get("contracts") or 0)
        # Intrinsic / extrinsic through the ONE pricing engine. A short put is
        # in-the-money when spot is BELOW the strike, so intrinsic inverts relative
        # to a call. ONLY the extrinsic half is income; the intrinsic half is a
        # share-purchase obligation and must never reach the juice ledger.
        intrinsic_ps = max(strike - price, 0.0) if price is not None else None
        mark_ps = _put_mark_per_share(ticker, leg, price)
        extrinsic_ps = (None if (mark_ps is None or intrinsic_ps is None)
                        else max(mark_ps - intrinsic_ps, 0.0))
        leg["intrinsic_per_share"] = (None if intrinsic_ps is None
                                      else round(intrinsic_ps, 4))
        leg["extrinsic_per_share"] = (None if extrinsic_ps is None
                                      else round(extrinsic_ps, 4))
        leg["mark_per_share"] = None if mark_ps is None else round(mark_ps, 4)
        gap = strike_gap(price, strike, put=True)
        leg["itm"] = gap["itm"]
        leg["stock_price"] = gap["stock_price"]
        leg["strike_distance"] = gap["distance"]
        leg["strike_distance_pct"] = gap["distance_pct"]
        leg["moneyness"] = gap["moneyness"]
        leg["collateral"] = leg.get("collateral") or scan_verdict_put_collateral(strike, n)
        open_puts.append(leg)
    out["short_puts"] = open_puts
    out["put_collateral"] = round(sum(float(l.get("collateral") or 0)
                                      for l in open_puts), 2) if open_puts else 0.0
    if open_puts:
        out["put_regate"] = put_regate(position)
        out["tempo"] = _tempo(ticker)

    shares = dict(position.get("shares") or {})
    count = int(shares.get("count") or 0)
    if position_types.is_put(position):
        # SHARE READOUTS ARE UNDEFINED FOR A PUT, NOT ZERO. A put holds no shares
        # by construction, so cap progress, covered-lot capacity and the
        # accumulation guard are not "0 of 500" — they are questions that do not
        # apply until assignment converts the collateral into shares. Rendering
        # them as zero would show a full-looking meter at 0% and a coverage
        # guardrail that reads as satisfied.
        shares.update({
            "not_applicable": True,
            "reason": "a cash-secured put holds no shares until assignment",
            "count": None, "cap": None, "pct_to_cap": None, "locked": None,
            "coverable_lots": None, "fragment_shares": None, "has_fragment": None,
        })
    else:
        cap = int(shares.get("cap") or config.SHARE_CAP)
        shares["cap"] = cap
        shares["pct_to_cap"] = round(count / cap * 100, 1) if cap else 0
        shares["locked"] = count >= cap
        # Covered-lot capacity (schema v20): floor(count/100) sellable lots +
        # fragment flag. For a SHARES base this is the coverage guardrail (short
        # count can never exceed coverable_lots); harmless on a legacy sidecar.
        shares.update(covered_lots(count))
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


def put_regate(position: dict, df=None) -> dict:
    """THE DAILY RE-GATE (§2.1): would this name be admitted today?

    If yes, assignment is a GOOD ENTRY and the recommendation is to let it happen —
    the put was written at a price you wanted to pay and the thesis is intact. If
    no, the recommendation is to CLOSE the put rather than accept delivery of
    shares the entry rules would refuse.

    RULE REUSE, NEVER A FORK. The four structural signals are read from the modules
    that own them — ``circuit_breaker.evaluate`` for the two MA legs and the
    operator's line, ``kill_switch.classify`` for RS3M-vs-SPY — and handed to
    ``scan_verdict.put_close_advice``, which is pure. There is exactly one
    definition of each rule in the codebase and this reads all four.

    WHAT IS DELIBERATELY NOT HERE:

      * MA21, in any form. It is a timing reference, not a thesis signal, and
        quality names in intact uptrends dip below it routinely.
      * The 8/21 EMA cross. Tempo only — surfaced beside this, never inside it.
      * ``circuit_breaker``'s DRAWDOWN condition. §2.1 enumerates the close
        triggers and drawdown-from-entry is not among them. That is a real
        narrowing relative to a shares position, and it is deliberate: a put's
        "entry price" is the spot when it was written, which is not the price you
        agreed to pay — the STRIKE is. Judging a 15% drawdown against the wrong
        reference would close puts that are doing exactly what they were sold to do.
      * The account, tradeability and staleness vetoes. See
        ``scan_verdict.PUT_CLOSE_TRIGGERS`` for why each is nonsense as a close
        reason.
    """
    import circuit_breaker
    import data_handler
    import kill_switch
    import scan_verdict
    ticker = position.get("ticker", "")
    if df is None:
        df = data_handler.get_daily(ticker)
    cb = circuit_breaker.evaluate(position, df)
    by_id = {c["id"]: c for c in cb.get("conditions") or []}
    rs = None
    try:
        rs = kill_switch.evaluate(ticker).get("rs3m_vs_spy")
    except Exception:  # noqa: BLE001 — an RS miss reads as unknown, never as a break
        rs = None
    blocks = scan_verdict.put_close_triggers(
        ma_fast_breached=bool((by_id.get("ma_fast") or {}).get("tripped")),
        ma_slow_breached=bool((by_id.get("ma_slow") or {}).get("tripped")),
        rs3m_vs_spy=rs,
        line_breached=bool((by_id.get("manual_line") or {}).get("tripped")))
    advice = scan_verdict.put_close_advice(blocks=blocks)
    return {**advice, "as_of": _today_iso(), "rs3m_vs_spy": rs,
            "conditions": {k: bool((v or {}).get("tripped"))
                           for k, v in by_id.items() if k != "drawdown"}}


def _today_iso() -> str:
    from datetime import date as _date
    return _date.today().isoformat()


def _tempo(ticker: str) -> dict:
    """The 8/21 EMA cross for the position card. A FLAG AND NOTHING ELSE.

    Surfaced next to the re-gate and structurally unable to reach it: ``put_regate``
    above never reads this, and ``scan_verdict.put_close_triggers`` accepts no tempo
    argument. See ``scan_verdict.tempo_signal``."""
    import data_handler
    import indicators
    import scan_verdict
    df = data_handler.get_daily(ticker)
    if df is None:
        return scan_verdict.tempo_signal(None, None)
    return scan_verdict.tempo_signal(indicators.ema(df, 8), indicators.ema(df, 21))


def scan_verdict_put_collateral(strike, contracts):
    """``scan_verdict.put_collateral``, imported lazily.

    The formula is NOT restated here. It already exists because the scan's route
    selector needs it to say what a put would tie up, and one definition with two
    callers is the only shape in which the advisory figure and the booked figure
    cannot drift apart."""
    import scan_verdict
    return scan_verdict.put_collateral(strike, contracts) or 0.0


def _put_mark_per_share(ticker: str, leg: dict, spot) -> float | None:
    """Model mark for one short-put leg, per share, through the EXISTING BSM
    engine (``indicators._bs_put_price``). No second pricing path.

    Vol is the name's trailing realized vol from cached bars — the same input
    ``account_gate.juice_estimate`` prices the weekly short call at — so an open
    put is valuable off-hours and offline, which is exactly when expiry-day
    monitoring needs it. Returns None rather than a guess when the frame, the
    spot or the DTE cannot be resolved; a put whose mark is unknown must read as
    unknown, never as zero extrinsic.
    """
    import data_handler
    import indicators
    from datetime import date as _date
    try:
        strike = float(leg.get("strike") or 0)
        if not (spot and strike > 0):
            return None
        exp = _date.fromisoformat(str(leg.get("expiration"))[:10])
        dte = (exp - _date.today()).days
        if dte < 0:
            return None
        df = data_handler.get_daily(ticker)
        sigma = indicators.hist_vol(df) if df is not None else None
        if not sigma:
            return None
        T = max(dte, 1) / 365.0
        return indicators._bs_put_price(float(spot), strike, T,
                                        config.RISK_FREE_RATE, float(sigma) / 100.0)
    except Exception:  # noqa: BLE001 — a valuation miss reads as unknown, not zero
        return None


def covered_lots(shares_count) -> dict:
    """Covered-call capacity of an owned-share count (schema v20, SHARES base).

    HARD_CFM_RULE: only whole 100-share round lots can be sold against — a
    fragment below one lot is never coverable. 150 shares -> 1 coverable lot + a
    50-share fragment, NEVER 2. This is the atomic 100-share -> 1-contract floor
    the covered-short derivation keys off (a SHARES position's short-call count
    can never exceed ``coverable_lots``)."""
    n = max(int(shares_count or 0), 0)
    lots = n // config.SHARES_PER_LOT
    fragment = n - lots * config.SHARES_PER_LOT
    return {"shares": n, "coverable_lots": lots, "fragment_shares": fragment,
            "has_fragment": fragment > 0}


def position_capital(p: dict) -> float:
    """Capital deployed in one position: every LEAP leg's cost basis, plus any
    accumulated shares (count x cost basis per share), plus any open short-put
    COLLATERAL. The buy/open executions set these on the position, so this is the
    source of truth.

    COLLATERAL COUNTS AGAINST THE DEPLOYED-CAPITAL CAP (schema v22) and does NOT
    draw the ATR cash reserve. Those are two separate figures by construction, not
    by convention: the reserve is a formula over ATR (RESERVE_ATR_MULT x ATR x
    contracts x 100, see account_gate) and is not computed from deployed capital
    at all, so adding a term here cannot reach it. Collateral IS the position —
    it is not a defence of one — which is why it belongs on this side of the line.
    """
    import logging_handler as log
    total = sum(float(l.get("cost_basis") or 0) for l in log.leap_legs(p))
    total += sum(float(sp.get("collateral") or 0) for sp in p.get("short_puts") or [])
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
    # The most a SINGLE 100-share lot may cost right now — the affordability bar
    # the scan filters against (schema v21). The tighter of two ceilings:
    #   * `deployable`   — the dry powder above, itself min(capital cap headroom,
    #                      cash above the defensive reserve);
    #   * PER_POSITION_CAP_USD — the per-position lot-cost SIZE-BLOCK Level 5
    #                      enforces (round_lot_size).
    # Derived from the SAME inputs account_gate's cash_reserve / capital_limit /
    # round_lot_size checks use, so a name the scan shows can't be one the Execute
    # gate would then reject on size, and vice versa.
    #
    # None when operating cash is UNKNOWN — i.e. never configured and never read
    # from Schwab. state.metadata.operating_cash defaults to 0, so a zero is
    # ambiguous between "I have no money" and "I never told the app", and the safe
    # reading is the second: an unknown bar filters NOTHING. Without this the whole
    # scan would come back empty on a fresh book and look broken rather than broke.
    #
    # A free position SLOT is deliberately NOT a term here. Whether a slot is open
    # is the position_limit gate's job, and it already surfaces as a near-miss with
    # a path ("a position slot frees") — a full book should still show the pipeline
    # it will draw from, not an empty screen.
    max_lot_cost = (round(min(deployable, config.PER_POSITION_CAP_USD), 2)
                    if operating > 0 else None)
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
        # Affordability bar for a single 100-share lot (see above). The scan hides
        # names whose lot costs more than this; `per_position_cap` is carried so the
        # UI can say WHICH ceiling is binding rather than just showing a number.
        "max_lot_cost": max_lot_cost,
        "per_position_cap": config.PER_POSITION_CAP_USD,
        "shares_per_lot": config.SHARES_PER_LOT,
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
                      f"{ev.get('rs3m_vs_spy')}")
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
