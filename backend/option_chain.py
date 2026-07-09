"""Option-chain viewer for CFM: auto-picks the deep-ITM LEAP strike (delta ~0.90,
closest to 180 DTE) and a regime-aware ATR-based weekly short strike, each with
live bid/ask/extrinsic, so the user can eyeball both before executing.

Chains come from Schwab only (Alpha Vantage has no usable options data) and are
cached for 5 minutes per ticker so repeated modal opens don't hammer the API.

Market regime + the operator's risk posture set the weekly short strike via
strike_policy.suggest_strike (config.STRIKE_TABLE — the ATR-mult/ITM%-floor
table, "Genius System" reference). New entries are still blocked on a RED tape
(RegimeBlocked is raised; the route returns 403) regardless of posture.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta

import config
import data_handler
import dividends
import indicators
import iv_history
import logging_handler as log
import market_calendar
import schwab_api
import screening
import strike_policy

_CHAIN_TTL = 300  # seconds — 5-minute per-ticker cache
_chain_cache: dict[str, tuple[float, dict]] = {}
_chain_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class RegimeBlocked(RuntimeError):
    """Raised when the market regime is RED — no entries are allowed."""


def _chain_lock(ticker: str) -> threading.Lock:
    with _locks_guard:
        return _chain_locks.setdefault(ticker, threading.Lock())


def _fetch_chain(ticker: str) -> dict:
    """Raw Schwab CALL chain spanning near-term through ~LEAP expirations, cached
    for 5 minutes per ticker. One lock per ticker collapses concurrent opens."""
    hit = _chain_cache.get(ticker)
    if hit and time.time() - hit[0] < _CHAIN_TTL:
        return hit[1]
    with _chain_lock(ticker):
        hit = _chain_cache.get(ticker)
        if hit and time.time() - hit[0] < _CHAIN_TTL:
            return hit[1]
        if not schwab_api.configured():
            raise schwab_api.SchwabError(
                "Schwab is not connected — re-authorize at /auth/schwab to load option chains")
        today = datetime.now()
        to_date = (today + timedelta(days=config.LEAP_TARGET_DTE + 90)).strftime("%Y-%m-%d")
        payload = data_handler.client().get_option_chain(
            ticker, strike_count=100, from_date=today.strftime("%Y-%m-%d"), to_date=to_date)
        status = (payload or {}).get("status")
        if status and status != "SUCCESS":
            raise schwab_api.SchwabError(f"Schwab returned status '{status}' for {ticker}")
        _chain_cache[ticker] = (time.time(), payload)
        return payload


def _median(vals: list) -> float | None:
    nums = sorted(v for v in vals if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    return nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2


def _is_weekly_boundary(exp: str | None) -> bool:
    """True when an expiration date is the standard end-of-week expiration to
    sell a weekly against: a Friday, or — when that Friday is a market holiday
    (so the series expires a day early) — the Thursday before it.

    Names like IWM/SPY now list a fresh daily expiration every trading day; CFM
    sells one *weekly* call, so Mon–Thu dailies are skipped and the short lands
    on Friday (Thursday on a Good-Friday / holiday week)."""
    if not exp:
        return False
    try:
        d = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    if d.weekday() == 4:  # Friday — unless the exchange is closed that day
        return not market_calendar.is_market_holiday(d)
    if d.weekday() == 3:  # Thursday — only when the next day's Friday is a holiday
        return market_calendar.is_market_holiday(d + timedelta(days=1))
    return False


def _weekly_expirations(contracts: list[dict], count: int = 2) -> list[str]:
    """The next `count` weekly-short expirations, nearest first: weekly boundaries
    (Friday, or a holiday-week Thursday) with dte>0. Falls back to the nearest
    dated expirations when the chain lists no Friday boundary (e.g. a monthly-only
    name). dte>0 excludes today's 0-DTE contract, whose time value is gone.

    CFM sells one weekly, but the juice comparison needs a full week of premium,
    so callers want both this week's boundary and next week's to work with."""
    dated = [c for c in contracts if c.get("dte") is not None and c["dte"] > 0]
    pool = [c for c in dated if _is_weekly_boundary(c.get("expiration"))] or dated
    by_exp: dict[str, int] = {}
    for c in pool:
        exp = c.get("expiration")
        if exp is not None:
            by_exp[exp] = min(by_exp.get(exp, c["dte"]), c["dte"])
    return sorted(by_exp, key=lambda e: by_exp[e])[:count]


def _weekly_expiration(contracts: list[dict]) -> str | None:
    """The expiration for this week's short: the nearest weekly boundary
    (Friday, or a holiday-week Thursday) with dte>0. Falls back to the nearest
    dated expiration when the chain lists no Friday boundary."""
    exps = _weekly_expirations(contracts, count=1)
    return exps[0] if exps else None


def _pick_comparison_weekly(groups: list[dict]) -> dict | None:
    """From the weekly expiration groups (nearest first, each carrying a `dte`),
    pick the one whose extrinsic is a fair juice comparison: the nearest with at
    least WEEKLY_MIN_COMPARISON_DTE days of premium. When even the furthest listed
    weekly is a stub, fall back to that furthest one (most time value available)."""
    if not groups:
        return None
    for g in groups:
        if g.get("dte") is not None and g["dte"] >= config.WEEKLY_MIN_COMPARISON_DTE:
            return g
    return groups[-1]


def _iv_view(weekly_iv: float | None, leap_iv: float | None, hv: float | None,
             ticker: str | None = None) -> dict:
    """Compare the weekly short's IV to the stock's 20-day realized volatility AND
    to its own trailing-year range (IV rank). IV above realized = rich vs the
    stock's typical move; a high IV rank = rich vs its OWN history — the
    constructive twin of the juice-rich warning ("a good week to sell")."""
    out = {"weekly_iv": weekly_iv, "leap_iv": leap_iv, "hist_vol": hv,
           "iv_rank": None, "iv_percentile": None}
    if ticker and weekly_iv is not None:
        rank = iv_history.iv_rank(ticker, weekly_iv)
        out["iv_rank"] = rank["iv_rank"]
        out["iv_percentile"] = rank["iv_percentile"]
        out["iv_rank_days"] = rank["days"]
    if weekly_iv is None or hv is None or hv == 0:
        out["premium"] = "unknown"
        out["label"] = "IV vs realized unavailable"
        out["iv_vs_hv"] = None
        return out
    ratio = weekly_iv / hv
    out["iv_vs_hv"] = round(ratio, 2)
    ivr = out["iv_rank"]
    rank_note = (f" · IV rank {ivr:g}"
                 + (" — rich vs its own year, a good week to sell" if ivr is not None and ivr >= 50
                    else " — cheap vs its own year" if ivr is not None and ivr <= 25 else "")
                 if ivr is not None else "")
    if ratio >= 1.1:
        out["premium"] = "rich"
        out["label"] = f"IV {weekly_iv:g}% is HIGHER than 20-day realized {hv:g}% — premium rich (favorable to sell){rank_note}"
    elif ratio <= 0.9:
        out["premium"] = "cheap"
        out["label"] = f"IV {weekly_iv:g}% is LOWER than 20-day realized {hv:g}% — premium cheap (thin to sell){rank_note}"
    else:
        out["premium"] = "fair"
        out["label"] = f"IV {weekly_iv:g}% is in line with 20-day realized {hv:g}%{rank_note}"
    return out


def _detect_action(has_leap: bool, open_shorts: list, management_only: bool = False) -> tuple[str, str]:
    """Pick the action the user most likely wants next, given current positions.

    In management_only mode (RED tape) entries are off the table, so the only
    move is closing/rolling an open short to de-risk or exit."""
    if management_only:
        if open_shorts:
            return "close_short", "Market is RED — buy to close / roll the open short first to remove the obligation."
        if has_leap:
            return "close_leap", "Market is RED — sell the LEAP to close and exit the long."
        return "close_short", "Market is RED — entries blocked."
    if not has_leap:
        return "buy_leap", "No LEAP held yet — establish the deep-ITM long first."
    if not open_shorts:
        return "sell_short", "LEAP held with no open short — sell this week's call for juice."
    return "close_short", "A short call is already open — roll it (buy to close)."


def roll_options(ticker: str) -> dict:
    """Data for the short-roll picker: the current open short with its live
    buy-to-close cost, plus every candidate expiration out to ROLL_MAX_DTE with
    nearby strikes around the regime-aware ATR target.

    The frontend uses this to let the user roll to the SAME or a DIFFERENT week
    (pick an expiration) and the SAME or a DIFFERENT strike (pick a strike within
    it). The current short's own strike is always included in every expiration's
    list so a same-strike roll is selectable everywhere.
    """
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker is required")

    state = log.load_state()
    pos = log.find_position(state, ticker)
    open_shorts = [sc for sc in (pos or {}).get("short_calls", []) if sc] if pos else []
    if not open_shorts:
        return {"ticker": ticker, "current_short": None, "expirations": [],
                "error": "no open short to roll"}

    payload = _fetch_chain(ticker)
    underlying, contracts = schwab_api.parse_call_chain(payload)
    if underlying is None:
        quote = data_handler.latest_quote(ticker)
        underlying = quote["price"] if quote else None
    if not contracts:
        raise schwab_api.SchwabError(f"no call contracts returned for {ticker}")

    # Regime + posture aware target strike (same rule the entry chain uses;
    # RED is fully supported here since rolling an open short is allowed even
    # on a red tape — only fresh entries are blocked).
    reg = screening.regime()
    df = data_handler.get_daily(ticker)
    atr_val = indicators.atr(df)
    price = underlying if underlying is not None else indicators.last(df)
    sp = (strike_policy.suggest_strike(price, atr_val, reg.get("status"))
          if atr_val is not None and price is not None else None)
    atr_mult = sp["atr_mult"] if sp else None
    itm_pct = sp["itm_pct"] if sp else None
    posture = sp["posture"] if sp else None
    suggested_strike = sp["strike"] if sp else None

    # The current short to roll = the nearest-dated open leg, with a live buyback.
    current = min(open_shorts, key=lambda s: s.get("dte") if s.get("dte") is not None else 1e9)
    cur_strike = current.get("strike")
    cur_exp = current.get("expiration")
    match = next((c for c in contracts if c.get("strike") == cur_strike
                  and (cur_exp is None or c.get("expiration") == cur_exp)), None)
    if match is None:
        match = next((c for c in contracts if c.get("strike") == cur_strike), None)
    match = indicators._augment(match, underlying) if match else None
    cur_exp = cur_exp or (match or {}).get("expiration")
    current_view = {
        "strike": cur_strike,
        "contracts": current.get("contracts"),
        "expiration": cur_exp,
        "dte": current.get("dte") if current.get("dte") is not None else (match or {}).get("dte"),
        "current_bid": (match or {}).get("bid"),
        "current_ask": (match or {}).get("ask"),
        "current_mark": (match or {}).get("mark"),
        "entry_extrinsic_per_share": current.get("entry_extrinsic_per_share"),
    }

    # Next earnings date — a roll week that SPANS the report gets a deep-ITM
    # suggested strike so the short keeps intrinsic cover across the gap.
    earn_date = None
    try:
        import earnings
        raw = (earnings.next_earnings(ticker) or {}).get("date")
        if raw:
            earn_date = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001 — earnings lookup must not sink the roll picker
        earn_date = None
    today = datetime.utcnow().date()
    earn_strike = (strike_policy.suggest_earnings_strike(price, atr_val, reg.get("status"))["strike"]
                   if earn_date is not None and atr_val is not None and price is not None else None)

    # Candidate expirations out to ROLL_MAX_DTE, each with nearby strikes.
    by_exp: dict[str, dict] = {}
    for c in contracts:
        exp, dte = c.get("expiration"), c.get("dte")
        if exp is None or dte is None or dte < 0 or dte > config.ROLL_MAX_DTE:
            continue
        # Offer only weekly boundaries (Friday, or a holiday-week Thursday) — the
        # same rule the entry chain uses — so the daily expirations on IWM/SPY
        # don't clutter the picker. The current short's own expiration is always
        # kept so a same-week roll stays selectable even if it sits off-Friday.
        if exp != cur_exp and not _is_weekly_boundary(exp):
            continue
        by_exp.setdefault(exp, {"expiration": exp, "dte": dte, "contracts": []})["contracts"].append(c)

    default_target = suggested_strike if suggested_strike is not None else cur_strike
    expirations = []
    for exp in sorted(by_exp, key=lambda e: by_exp[e]["dte"]):
        grp = by_exp[exp]
        # Earnings falls inside this new short's week if the report is on/before
        # the expiration (and not already past).
        exp_date = None
        try:
            exp_date = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            exp_date = None
        earnings_in_week = bool(earn_date and exp_date and today <= earn_date <= exp_date)
        target = earn_strike if (earnings_in_week and earn_strike is not None) else default_target
        strikes = indicators.get_nearby_strikes(grp["contracts"], target, underlying, count=7)
        # Guarantee the current strike is offered so "same strike" always works.
        if cur_strike is not None and not any(s["strike"] == cur_strike for s in strikes):
            same = next((c for c in grp["contracts"] if c.get("strike") == cur_strike), None)
            if same:
                strikes = sorted(strikes + [indicators._augment(same, underlying)],
                                 key=lambda s: s["strike"])
        expirations.append({
            "expiration": exp,
            "dte": grp["dte"],
            "is_current_week": exp == cur_exp,
            "earnings_in_week": earnings_in_week,
            "deep_itm_suggested": bool(earnings_in_week and earn_strike is not None),
            "strikes": strikes,
        })

    return {
        "ticker": ticker,
        "underlying_price": round(underlying, 2) if underlying is not None else None,
        "regime": reg.get("status"),
        "atr": round(atr_val, 2) if atr_val is not None else None,
        "atr_mult": atr_mult,
        "itm_pct": itm_pct,
        "posture": posture,
        "suggested_strike": suggested_strike,
        "earnings_date": earn_date.isoformat() if earn_date else None,
        "iv_rank": iv_history.iv_rank(ticker),
        "current_short": current_view,
        "expirations": expirations,
    }


def _augment_call_greeks(payload: dict, contracts: list[dict], underlying, ticker: str) -> None:
    """Recompute delta + IV via Black–Scholes–Merton for every call contract, in
    place, so strike selection (delta band), display, and the coverage check all
    use TOS-consistent values rather than Schwab's unreliable chain greeks.

    For an ITM call we prefer the same-strike PUT's IV (the OTM put's vol is
    stable and skew-aware, whereas the deep-ITM call's own IV collapses on thin
    time value → delta ~1.0). When that IV is missing (off-hours NaNs) we imply
    it from the put's mark. A dividend yield lowers a payer's call delta.
    """
    put_iv = schwab_api.parse_put_iv(payload)
    put_q = schwab_api.parse_put_quotes(payload)
    div_yield = dividends.yield_for(ticker)
    for c in contracts:
        mark = c.get("mark")
        if mark is None and c.get("bid") is not None and c.get("ask") is not None:
            mark = round((c["bid"] + c["ask"]) / 2, 4)
        strike = c.get("strike")
        reported_iv = c.get("volatility")
        if underlying and strike and strike < underlying:  # ITM call -> use OTM put vol
            skew_iv = put_iv.get((c["expiration"], strike))
            if skew_iv is None:
                pq = put_q.get((c["expiration"], strike))
                dte = c.get("dte")
                if pq and pq.get("mark") and dte:
                    ivp = indicators.implied_vol_put(pq["mark"], underlying, strike,
                                                     dte / 365.0, config.RISK_FREE_RATE, div_yield)
                    if ivp:
                        skew_iv = round(ivp * 100, 2)
            reported_iv = skew_iv or reported_iv
        d, iv = indicators.call_greeks(underlying, strike, c.get("dte"), mark,
                                       reported_iv=reported_iv, q=div_yield)
        if d is not None:
            c["delta"] = d
        if iv is not None:
            c["volatility"] = iv


# Delta guardrails for the Poor Man's Covered Call (diagonal):
#   • LEAP (long) delta must stay >= this floor or it stops acting like a
#     deep-ITM stock proxy (too much extrinsic/theta) — roll it deeper ITM.
#   • The short is "covered" only while the long's total delta >= the short's
#     total delta; if the short's delta climbs past the long's, an up-move loses
#     faster on the short than it gains on the long (effectively uncovered).
LEAP_DELTA_FLOOR = 0.50
_FLOOR_WATCH = 0.55       # approaching the floor
_COVER_WATCH = 0.05       # long delta within this of short delta (per contract)


def _match_delta(contracts, strike, expiration, prefer_far):
    """Delta of the held contract at `strike`: prefer the exact stored
    expiration, else the far-dated (LEAP) or nearest-dated (short) match."""
    cands = [c for c in contracts if c.get("strike") == strike and c.get("delta") is not None]
    if not cands:
        return None, None
    if expiration:
        exact = [c for c in cands if c.get("expiration") == expiration]
        if exact:
            cands = exact
    pick = max(cands, key=lambda c: c.get("dte") or 0) if prefer_far \
        else min(cands, key=lambda c: c.get("dte") if c.get("dte") is not None else 1e9)
    return pick.get("delta"), pick.get("expiration")


def coverage(ticker: str) -> dict:
    """Delta-coverage assessment for a held position: the LEAP delta vs the 0.50
    floor, and whether the long still covers the short (total delta). Degrades to
    status "unknown" when deltas can't be sourced (Schwab off / off-hours)."""
    ticker = ticker.strip().upper()
    state = log.load_state()
    pos = log.find_position(state, ticker)
    if not pos or pos.get("status") == "closed":
        return {"ticker": ticker, "status": "none", "message": "No open position."}

    leap = pos.get("leap") or None
    shorts = [sc for sc in (pos.get("short_calls") or []) if sc]
    if not schwab_api.configured():
        return {"ticker": ticker, "status": "unknown",
                "message": "Schwab not connected — live deltas unavailable."}
    try:
        payload = _fetch_chain(ticker)
        underlying, contracts = schwab_api.parse_call_chain(payload)
        if underlying is None:
            quote = data_handler.latest_quote(ticker)
            underlying = quote["price"] if quote else None
        _augment_call_greeks(payload, contracts, underlying, ticker)
    except Exception as e:  # noqa: BLE001 — never let a monitor crash the page
        return {"ticker": ticker, "status": "unknown", "message": str(e)}

    leap_delta = leap_contracts = None
    if leap:
        leap_contracts = int(leap.get("contracts") or 0)
        leap_delta, _ = _match_delta(contracts, leap.get("strike"), leap.get("expiration"), prefer_far=True)
    leap_view = {"strike": (leap or {}).get("strike"), "contracts": leap_contracts, "delta": leap_delta}

    short_views, short_total, max_short_delta = [], 0.0, None
    for sc in shorts:
        d, _ = _match_delta(contracts, sc.get("strike"), sc.get("expiration"), prefer_far=False)
        n = int(sc.get("contracts") or 0)
        short_views.append({"strike": sc.get("strike"), "contracts": n,
                            "expiration": sc.get("expiration"), "delta": d})
        if d is not None:
            short_total += d * n
            max_short_delta = d if max_short_delta is None else max(max_short_delta, d)
    long_total = (leap_delta or 0) * (leap_contracts or 0)

    alerts, status, alert = [], "green", False
    # LEAP delta floor (long leg only).
    if leap_delta is not None:
        if leap_delta < LEAP_DELTA_FLOOR:
            status, alert = "red", True
            alerts.append(f"LEAP delta {leap_delta:.2f} is below {LEAP_DELTA_FLOOR:.2f} — "
                          "roll the LEAP deeper ITM (it's no longer a stock proxy).")
        elif leap_delta < _FLOOR_WATCH and status != "red":
            status = "yellow"
            alerts.append(f"LEAP delta {leap_delta:.2f} is nearing the {LEAP_DELTA_FLOOR:.2f} floor.")
    # Coverage: long total delta must stay >= short total delta. Compare the
    # contract-weighted TOTALS (not a single leg's delta) so a position with more
    # than one short reads correctly — the sum of short deltas is what the long has
    # to cover. The watch buffer scales with long contracts so the single-short /
    # equal-contract case keeps its original per-contract semantics.
    if leap_delta is not None and max_short_delta is not None:
        cover_watch = _COVER_WATCH * max(int(leap_contracts or 0), 1)
        if short_total > long_total + 1e-9:
            status, alert = "red", True
            alerts.append(f"Short delta exceeds the LEAP's ({short_total:.2f} vs {long_total:.2f}) — "
                          "the long isn't covering the short; roll the short up/out.")
        elif (long_total - short_total) < cover_watch and status != "red":
            status = "yellow"
            alerts.append(f"Short delta {short_total:.2f} is closing on the LEAP's {long_total:.2f} "
                          "— coverage thinning.")
    elif shorts and leap_delta is None:
        status, alert = "red", True
        alerts.append("Short open with no LEAP delta — the short is uncovered.")

    return {
        "ticker": ticker,
        "status": status,
        "alert": alert,
        "leap": leap_view,
        "shorts": short_views,
        "long_total_delta": round(long_total, 4),
        "short_total_delta": round(short_total, 4),
        "net_delta": round(long_total - short_total, 4),
        "floor": LEAP_DELTA_FLOOR,
        "covered": (leap_delta is not None and max_short_delta is not None and short_total <= long_total + 1e-9),
        "message": " ".join(alerts) or "Covered — LEAP delta ≥ floor and ≥ short delta.",
    }


def option_chain(ticker: str, strategy: str = "atr") -> dict:
    """Build the option-chain view: regime banner, auto-picked LEAP, and the
    ATR-suggested weekly short with nearby strikes.

    On a RED tape entries are blocked: if there's nothing to manage we raise
    RegimeBlocked, but an existing position drops into management-only mode so the
    user can still close/roll the open short and get out."""
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker is required")

    reg = screening.regime()
    regime_status = reg.get("status")

    # --- Current position (cheap local read) — drives action + RED handling -
    state = log.load_state()
    pos = log.find_position(state, ticker)
    existing_leap = (pos or {}).get("leap") or None
    has_leap = bool(existing_leap)
    open_shorts = [sc for sc in (pos or {}).get("short_calls", []) if sc] if pos else []

    management_only = regime_status == "red"
    if management_only and not (has_leap or open_shorts):
        # Nothing to manage and entries are blocked — there's nothing to show.
        raise RegimeBlocked("Market is RED. No entries.")
    suggested_action, action_reason = _detect_action(has_leap, open_shorts, management_only)

    payload = _fetch_chain(ticker)
    underlying, contracts = schwab_api.parse_call_chain(payload)
    if not contracts:
        raise schwab_api.SchwabError(f"no call contracts returned for {ticker}")

    # Anchor the spot price: chain quote first, then a live quote, then last close.
    if underlying is None:
        quote = data_handler.latest_quote(ticker)
        underlying = quote["price"] if quote else None

    _augment_call_greeks(payload, contracts, underlying, ticker)

    # --- LEAP: candidate strikes in the preferred delta band (closest to 180
    # DTE) so the user can choose; the suggested one is closest to target delta.
    leap_strikes = indicators.get_leap_strikes(contracts, underlying)
    suggested_leap = next((s for s in leap_strikes if s.get("suggested")),
                          leap_strikes[0] if leap_strikes else None)
    leap_contracts = int(existing_leap.get("contracts")) if has_leap and existing_leap.get("contracts") else config.LEAP_CONTRACTS
    leap = None
    if suggested_leap:
        ext = suggested_leap.get("extrinsic")
        leap = {**suggested_leap, "strikes": leap_strikes, "target_contracts": leap_contracts,
                "extrinsic_total": round(ext * 100 * config.LEAP_CONTRACTS, 2) if ext is not None else None,
                "delta_band": [config.LEAP_DELTA_MIN, config.LEAP_DELTA_MAX]}

    # --- Weekly short (regime-aware ATR strike + nearby strikes) ------------
    df = data_handler.get_daily(ticker)
    atr_val = indicators.atr(df)
    hv = indicators.hist_vol(df)
    price = underlying if underlying is not None else indicators.last(df)
    weekly: dict | None = None
    weekly_iv = None
    if atr_val is not None and price is not None:
        sp = strike_policy.suggest_strike(price, atr_val, regime_status)
        suggested_strike = sp["strike"]
        # CFM sells one weekly call on the coming Friday (Thursday on a holiday
        # week) — the nearest weekly boundary, not the nearest *daily*. IWM/SPY
        # list a new daily expiration every trading day, so Mon–Thu dailies are
        # skipped in favour of the Friday. We surface this week's AND next week's
        # weekly so the operator can see both — and so the juice comparison has a
        # full-week short to price against when the coming Friday is a 1–2 DTE
        # stub whose thin extrinsic would otherwise falsely block the Level-5 gate.
        weekly_exps = _weekly_expirations(contracts, count=2)
        exp_groups = []
        for exp in weekly_exps:
            exp_contracts = [c for c in contracts if c["expiration"] == exp]
            exp_groups.append({
                "expiration": exp,
                "dte": exp_contracts[0]["dte"] if exp_contracts else None,
                "strikes": indicators.get_nearby_strikes(exp_contracts, suggested_strike, underlying),
            })
        # Each week is a full chain with its own ATR-target `suggested` strike.
        # The comparison week (>= a full week of DTE) is the one flagged
        # is_comparison: it seeds the default selection and feeds the Level-5
        # juice gate, so a 1–2 DTE stub's thin extrinsic can't falsely block.
        comparison = _pick_comparison_weekly(exp_groups)
        for g in exp_groups:
            g["is_comparison"] = g is comparison
        strikes = comparison["strikes"] if comparison else []
        sug = next((s for s in strikes if s.get("suggested")), strikes[0] if strikes else None)
        weekly_iv = (sug or {}).get("volatility") or _median([s.get("volatility") for s in strikes])
        # Accrue one IV-history point per day from the IV we already computed —
        # this is what IV rank is measured against (zero extra chain fetches).
        iv_history.record(ticker, weekly_iv)
        weekly = {
            "expiration": comparison["expiration"] if comparison else None,
            "dte": comparison["dte"] if comparison else None,
            "suggested_strike": suggested_strike,
            "atr": round(atr_val, 2),
            "atr_mult": sp["atr_mult"],
            "itm_pct": sp["itm_pct"],
            "posture": sp["posture"],
            "strikes": strikes,
            "expirations": exp_groups,
        }

    # --- If a short is already open, surface its live buy-to-close cost ------
    open_short_view = None
    if open_shorts:
        sc = min(open_shorts, key=lambda s: s.get("dte") if s.get("dte") is not None else 1e9)
        match = next((c for c in contracts if c.get("strike") == sc.get("strike")), None)
        match = indicators._augment(match, underlying) if match else None
        open_short_view = {
            "strike": sc.get("strike"),
            "contracts": sc.get("contracts"),
            "dte": sc.get("dte"),
            "expiration": sc.get("expiration") or (match or {}).get("expiration"),
            "symbol": (match or {}).get("symbol"),
            "current_bid": (match or {}).get("bid"),
            "current_ask": (match or {}).get("ask"),
            "current_mark": (match or {}).get("mark"),
            "entry_extrinsic_per_share": sc.get("entry_extrinsic_per_share"),
        }

    # --- Existing LEAP: surface its live sell-to-close value (for exits/rolls).
    # Match the held strike to the far-dated contract (largest DTE = the LEAP).
    existing_leap_view = None
    if has_leap:
        held_strike = existing_leap.get("strike")
        held_exp = existing_leap.get("expiration")
        cands = [c for c in contracts if c.get("strike") == held_strike and c.get("dte") is not None]
        # Prefer the exact stored expiration; fall back to the far-dated match for
        # older positions saved before the expiration was persisted.
        if held_exp:
            exact = [c for c in cands if c.get("expiration") == held_exp]
            if exact:
                cands = exact
        match = max(cands, key=lambda c: c["dte"]) if cands else None
        match = indicators._augment(match, underlying) if match else None
        existing_leap_view = {
            "strike": held_strike,
            "contracts": existing_leap.get("contracts"),
            "cost_basis": existing_leap.get("cost_basis"),
            "expiration": held_exp or (match or {}).get("expiration"),
            "symbol": (match or {}).get("symbol"),
            "current_bid": (match or {}).get("bid"),
            "current_ask": (match or {}).get("ask"),
            "current_mark": (match or {}).get("mark"),
            "current_dte": (match or {}).get("dte"),
            "extrinsic_remaining": state.get("extrinsic_payback", {}).get(ticker, {}).get("remaining_to_payback"),
        }

    # --- Income payoff: how much LEAP extrinsic must be covered, and a rough
    # weeks-to-income-positive estimate from the suggested weekly juice --------
    qty = leap_contracts
    if has_leap:
        extrinsic_to_cover = state.get("extrinsic_payback", {}).get(ticker, {}).get("remaining_to_payback")
        cover_basis = "remaining on existing LEAP"
    else:
        extrinsic_to_cover = (leap or {}).get("extrinsic")
        extrinsic_to_cover = round(extrinsic_to_cover * 100 * qty, 2) if extrinsic_to_cover is not None else None
        cover_basis = "new LEAP entry extrinsic"
    sug_strike = next((s for s in (weekly or {}).get("strikes", []) if s.get("suggested")), None)
    weekly_ext_ps = (sug_strike or {}).get("extrinsic")
    weekly_juice = round(weekly_ext_ps * 100 * qty, 2) if weekly_ext_ps else None
    weeks = (math.ceil(extrinsic_to_cover / weekly_juice)
             if extrinsic_to_cover and weekly_juice and weekly_juice > 0 else None)
    payoff = {
        "leap_extrinsic_to_cover": extrinsic_to_cover,
        "cover_basis": cover_basis,
        "weekly_juice_estimate": weekly_juice,
        "weeks_to_income_positive": weeks,
        "quantity": qty,
    }

    return {
        "ticker": ticker,
        "strategy": strategy,
        "regime": regime_status,
        "management_only": management_only,
        "atr_mult": (weekly or {}).get("atr_mult"),
        "itm_pct": (weekly or {}).get("itm_pct"),
        "posture": (weekly or {}).get("posture"),
        "underlying_price": round(underlying, 2) if underlying is not None else None,
        "suggested_action": suggested_action,
        "action_reason": action_reason,
        "quantity_default": qty,
        "position": {
            "has_leap": has_leap,
            "leap_strike": (existing_leap or {}).get("strike"),
            "leap_contracts": (existing_leap or {}).get("contracts"),
            "open_short_count": len(open_shorts),
            "open_short": open_short_view,
            "existing_leap": existing_leap_view,
        },
        "iv": _iv_view(weekly_iv, (leap or {}).get("volatility"), hv, ticker),
        "leap": leap,
        "weekly": weekly,
        "payoff": payoff,
    }
