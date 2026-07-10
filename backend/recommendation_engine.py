"""Recommendation engine — the pure decision layer of the trust stack.

This module turns a frozen market snapshot + position state into explicit,
actionable Recommendation records BEFORE the operator acts. It is the exact
code path a future automation switch would call: supervised operation ("emit
and wait for the operator's tap") and autonomous operation ("emit and submit")
differ ONLY in what the caller does with the returned records. Nothing here is
automated — this version has no submit path at all.

PURITY CONTRACT: ``evaluate(market, state, now, open_recs)`` reads nothing but
its arguments. No provider access, no ``datetime.now()``, no state.json loads,
no network. Every impure input (prices, RS3M pairs, regime, earnings, q, bars)
arrives frozen inside ``market`` (built by recommendation_runner.py, the impure
shell); the clock arrives as ``now``. This is what makes the whole engine
exercisable offline against labeled fixtures — and what makes supervised trust
evidence transfer to a future autonomous mode.

RULE REUSE (never fork): kill_switch.classify, circuit_breaker.evaluate(df=...),
position_manager.whipsaw_status / delta_coverage / enrich_short,
strike_policy.suggest_strike / suggest_earnings_strike are the single sources of
truth; the engine only feeds them snapshot inputs and wraps their verdicts in
Recommendation records.

EMISSION POLICY: one dominant action recommendation per open position per pass
(exit triggers dominate defend, defend dominates rolls — priority order below);
every other fired trigger is preserved in input_snapshot.secondary_triggers.
A pass over a healthy position emits an explicit ALL_CLEAR — silence is not a
valid output. Re-evaluations that agree with an open, unexpired recommendation
emit nothing (the open record is the claim, and duplicates within a validity
window are forbidden); re-evaluations that disagree supersede it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import circuit_breaker
import config
import indicators
import kill_switch
import position_manager
import strike_policy
from rec_types import ActionType, TriggerRule

ENGINE_VERSION = 1

# Trigger priority (first fired wins the dominant slot), per action family.
_EXIT_PRIORITY = (
    TriggerRule.KILL_RS_SECTOR,
    TriggerRule.CIRCUIT_BREAKER,
    TriggerRule.WHIPSAW_GUARD,
    TriggerRule.KILL_RS_SPY_CONFIRMED,
    TriggerRule.DELTA_COVERAGE_FLOOR,
    TriggerRule.DTE_PLANNED_EXIT,
    TriggerRule.JUICE_HURDLE_FAIL,
)
_ROLL_PRIORITY = (
    TriggerRule.DEFEND_BELOW_STRIKE,      # -> DEFEND
    TriggerRule.EARNINGS_WINDOW,          # -> ROLL_OUT (deep-ITM earnings strike)
    TriggerRule.DIVIDEND_ASSIGNMENT_RISK, # -> ROLL_OUT (re-establish time value)
    TriggerRule.ROLL_75PCT,               # -> ROLL_OUT (early juice capture)
    TriggerRule.ROLL_SCHEDULED_WEEKLY,    # -> ROLL_OUT (weekly cadence)
)
_TRIGGER_ACTION = {
    TriggerRule.DEFEND_BELOW_STRIKE: ActionType.DEFEND,
    TriggerRule.EARNINGS_WINDOW: ActionType.ROLL_OUT,
    TriggerRule.DIVIDEND_ASSIGNMENT_RISK: ActionType.ROLL_OUT,
    TriggerRule.ROLL_75PCT: ActionType.ROLL_OUT,
    TriggerRule.ROLL_SCHEDULED_WEEKLY: ActionType.ROLL_OUT,
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_until(action_type: str, now: datetime) -> str:
    hours = config.REC_VALID_HOURS.get(action_type, 24)
    return _iso(now + timedelta(hours=hours))


def _tk(market: dict, ticker: str) -> dict:
    return (market.get("tickers") or {}).get(ticker.upper(), {}) or {}


def _bs_premium(price, strike, dte_days, vol, q) -> float | None:
    """Best-effort Black-Scholes premium estimate for ticket net math. Any
    missing input returns None — proposed tickets may carry null estimates
    (price_source records it); the staged order re-prices from the live chain."""
    try:
        if None in (price, strike, vol) or dte_days is None or dte_days <= 0 or vol <= 0:
            return None
        t_years = max(float(dte_days), 1.0) / 365.0
        return round(indicators._bs_call_price(float(price), float(strike), t_years,
                                               config.RISK_FREE_RATE, float(vol), q or 0.0), 2)
    except (TypeError, ValueError):
        return None


def _short_mark(sc: dict, tk: dict, q: float) -> float | None:
    """Current per-share value of an open short: the stored/polled mark when
    present, else a BS estimate off the snapshot's price + trailing vol."""
    if sc.get("current_bid") is not None:
        return float(sc["current_bid"])
    return _bs_premium(tk.get("price") or tk.get("last_close"), sc.get("strike"),
                       sc.get("dte"), tk.get("hist_vol"), q)


def _net_bounds(net: float | None) -> tuple[float | None, float | None]:
    """(limit, min_acceptable_net_credit) for an estimated net. The bound is the
    same haircut the net-juice math assumes (REC_MAX_SLIPPAGE_PCT_OF_MID), so
    the fidelity grade later measures exactly the assumption that was priced."""
    if net is None:
        return None, None
    slip = config.REC_MAX_SLIPPAGE_PCT_OF_MID
    floor = net - abs(net) * slip
    return round(net, 2), round(floor, 2)


def _roll_ticket(position: dict, sc: dict, tk: dict, *, new_strike: float | None,
                 roll_dte: int | None, roll_reason: str, q: float) -> dict:
    """A defensive/scheduled short-roll ticket: BUY_TO_CLOSE the current short +
    SELL_TO_OPEN the policy strike, one NET ticket. Estimates only — the staged
    order re-prices from the live chain; the bounds are what fidelity grades."""
    contracts = int(sc.get("contracts") or 0)
    buyback = _short_mark(sc, tk, q)
    price = tk.get("price") or tk.get("last_close")
    new_premium = _bs_premium(price, new_strike, roll_dte, tk.get("hist_vol"), q)
    net = (round(new_premium - buyback, 2)
           if new_premium is not None and buyback is not None else None)
    limit, floor = _net_bounds(net)
    return {
        "action": "roll_short",
        "roll_reason": roll_reason,
        "ticker": position.get("ticker"),
        "contracts": contracts,
        "legs": [
            {"instruction": "BUY_TO_CLOSE", "role": "short",
             "strike": sc.get("strike"), "expiration": sc.get("expiration"),
             "quantity": contracts},
            {"instruction": "SELL_TO_OPEN", "role": "short",
             "strike": new_strike, "dte": roll_dte, "quantity": contracts},
        ],
        "order_type": ("NET_CREDIT" if (net or 0) >= 0 else "NET_DEBIT"),
        "limit_price": limit,
        "min_acceptable_net_credit": floor,
        "max_slippage_pct_of_mid": config.REC_MAX_SLIPPAGE_PCT_OF_MID,
        "estimates": {"buyback_per_share": buyback, "new_premium_per_share": new_premium,
                      "net_per_share": net},
        "price_source": "estimate" if net is not None else "unpriced",
    }


def _exit_ticket(position: dict, tk: dict, q: float, exit_reason_code: str | None) -> dict:
    """A full-exit ticket: SELL_TO_CLOSE every LEAP leg + BUY_TO_CLOSE every
    open short, one NET ticket (mirrors executor._build_exit_legs' shape)."""
    import logging_handler as log
    legs, leap_value, short_cost = [], 0.0, 0.0
    priced = True
    for leg in log.leap_legs(position):
        n = int(leg.get("contracts") or 0)
        if not n:
            continue
        per_share = (float(leg["current_bid"]) / (n * 100)
                     if leg.get("current_bid") is not None else None)
        if per_share is None:
            priced = False
        else:
            leap_value += per_share * n
        legs.append({"instruction": "SELL_TO_CLOSE", "role": "leap",
                     "strike": leg.get("strike"), "expiration": leg.get("expiration"),
                     "quantity": n})
    for sc in position.get("short_calls", []):
        n = int(sc.get("contracts") or 0)
        if not n:
            continue
        mark = _short_mark(sc, tk, q)
        if mark is None:
            priced = False
        else:
            short_cost += mark * n
        legs.append({"instruction": "BUY_TO_CLOSE", "role": "short",
                     "strike": sc.get("strike"), "expiration": sc.get("expiration"),
                     "quantity": n})
    net = round(leap_value - short_cost, 2) if priced and legs else None
    limit, floor = _net_bounds(net)
    return {
        "action": "close_position",
        "ticker": position.get("ticker"),
        "exit_reason_code": exit_reason_code,
        "legs": legs,
        "order_type": ("NET_CREDIT" if (net or 0) >= 0 else "NET_DEBIT"),
        "limit_price": limit,
        "min_acceptable_net_credit": floor,
        "max_slippage_pct_of_mid": config.REC_MAX_SLIPPAGE_PCT_OF_MID,
        "estimates": {"net_per_share": net},
        "price_source": "estimate" if net is not None else "unpriced",
    }


def _enter_ticket(candidate: dict, market: dict) -> dict:
    """An entry ticket: deep-ITM LEAP at the target delta/DTE + the policy short
    strike, one NET_DEBIT diagonal (the atomic open the executor already routes)."""
    tk = _tk(market, candidate.get("ticker", ""))
    price = tk.get("price") or tk.get("last_close")
    atr_value = tk.get("atr")
    regime = (market.get("regime") or {}).get("status")
    posture = market.get("posture") or config.DEFAULT_STRIKE_POSTURE
    short = (strike_policy.suggest_strike(price, atr_value, regime, posture)
             if price is not None and atr_value is not None else {})
    q = tk.get("q") or 0.0
    leap_strike = candidate.get("leap_strike")
    leap_cost = _bs_premium(price, leap_strike, config.LEAP_TARGET_DTE, tk.get("hist_vol"), q)
    short_premium = _bs_premium(price, short.get("strike"), 5, tk.get("hist_vol"), q)
    net = (round(short_premium - leap_cost, 2)
           if leap_cost is not None and short_premium is not None else None)
    limit, floor = _net_bounds(net)
    contracts = int(candidate.get("contracts") or 1)
    return {
        "action": "open_position_atomic",
        "ticker": candidate.get("ticker"),
        "contracts": contracts,
        "legs": [
            {"instruction": "BUY_TO_OPEN", "role": "leap", "strike": leap_strike,
             "target_delta": config.LEAP_TARGET_DELTA, "target_dte": config.LEAP_TARGET_DTE,
             "quantity": contracts},
            {"instruction": "SELL_TO_OPEN", "role": "short", "strike": short.get("strike"),
             "dte": 5, "quantity": contracts},
        ],
        "order_type": "NET_DEBIT",
        "limit_price": limit,
        "min_acceptable_net_credit": floor,
        "max_slippage_pct_of_mid": config.REC_MAX_SLIPPAGE_PCT_OF_MID,
        "estimates": {"leap_cost_per_share": leap_cost, "short_premium_per_share": short_premium,
                      "net_per_share": net, "strike_policy": short or None},
        "price_source": "estimate" if net is not None else "unpriced",
    }


# ---------------------------------------------------------------------------
# Condition-first-true helpers (timeliness inputs) — pure over snapshot bars.
# ---------------------------------------------------------------------------
def _first_close_below(bars, level: float | None) -> str | None:
    """The date of the FIRST close of the current below-``level`` streak (walking
    back from the latest bar). None when the latest close is at/above level."""
    if bars is None or level is None or getattr(bars, "empty", True):
        return None
    try:
        closes = bars["Close"] if "Close" in bars.columns else bars["close"]
        first = None
        for idx in reversed(closes.index):
            if float(closes.loc[idx]) < float(level):
                first = idx
            else:
                break
        return str(first)[:10] if first is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _first_rs_negative(bars, bench_bars, lookback: int) -> str | None:
    """The date RS3M first went negative in the current negative streak, replayed
    from the snapshot's own bars (same indicators.rs3m, truncated history)."""
    if bars is None or bench_bars is None:
        return None
    try:
        first = None
        for i in range(len(bars), 0, -1):
            rs = indicators.rs3m(bars.iloc[:i], bench_bars.iloc[:i], lookback)
            if rs is not None and rs < 0:
                first = str(bars.index[i - 1])[:10]
            else:
                break
        return first
    except (KeyError, TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Per-position trigger evaluation
# ---------------------------------------------------------------------------
def _evaluate_position(position: dict, market: dict, now: datetime) -> dict:
    """Run every trigger rule for one open position off frozen inputs. Returns
    {triggers: {rule: detail}, features: {...}} — emission policy is applied by
    evaluate() on top of this."""
    t = position.get("ticker", "")
    tk = _tk(market, t)
    q = tk.get("q") or 0.0
    today = now.date()
    price = tk.get("price")
    last_close = tk.get("last_close")
    triggers: dict[str, dict] = {}

    # Kill switch — the shared pure core over the snapshot's RS pair.
    ks = kill_switch.classify(t, tk.get("rs3m_vs_spy"), tk.get("rs3m_vs_sector"))
    if ks["status"] == "red":
        rule = (TriggerRule.KILL_RS_SECTOR
                if ks.get("rs3m_vs_sector") is not None and ks["rs3m_vs_sector"] < 0
                else TriggerRule.KILL_RS_SPY_CONFIRMED)
        first = _first_rs_negative(
            tk.get("bars"),
            tk.get("sector_bars") if rule == TriggerRule.KILL_RS_SECTOR else tk.get("spy_bars"),
            config.RS3M_LOOKBACK)
        triggers[rule] = {"kill_switch": ks, "condition_first_true_at": first}

    # Circuit breaker — the shared evaluator with the snapshot's bars injected.
    cb = circuit_breaker.evaluate(position, df=tk.get("bars")) if tk.get("bars") is not None else None
    if cb and cb.get("tripped"):
        triggers[TriggerRule.CIRCUIT_BREAKER] = {
            "circuit_breaker": {k: cb.get(k) for k in
                                ("status", "tripped_conditions", "headline", "suggested_action")},
            "exit_reason_code": circuit_breaker.exit_reason_code(cb),
        }

    # Whipsaw guard — shared pure core over the derived roll ledger.
    ws = position_manager.whipsaw_status(position, market.get("roll_ledger") or [], today=today)
    if ws["tripped"]:
        triggers[TriggerRule.WHIPSAW_GUARD] = {"whipsaw": ws}

    # Delta coverage floor — shared pure core.
    cov = position_manager.delta_coverage(position, price or last_close, q=q)
    if cov["assessable"] and (cov["floor_breach"] or cov["inverted"]):
        triggers[TriggerRule.DELTA_COVERAGE_FLOOR] = {"delta_coverage": cov}

    # LEAP at/below the planned-exit DTE (anti-zombie boundary).
    leap_dte = position.get("leap_dte")
    planned = position.get("planned_exit_dte", config.PLANNED_EXIT_DTE)
    if leap_dte is not None and planned is not None and leap_dte <= planned:
        triggers[TriggerRule.DTE_PLANNED_EXIT] = {"leap_dte": leap_dte, "planned_exit_dte": planned}

    # Juice hurdle — the runner computes the adequacy flag through
    # leap_policy.leap_health (same code the JUICE_INADEQUATE alert reads) and
    # freezes it per ticker; the engine only consumes the verdict.
    juice = tk.get("juice") or {}
    if juice.get("inadequate"):
        triggers[TriggerRule.JUICE_HURDLE_FAIL] = {"juice": juice}

    # Short-leg triggers — shared enrich_short signals per open short.
    for sc in position.get("short_calls", []):
        es = position_manager.enrich_short(sc, price if price is not None else last_close,
                                           position.get("dividend"), today=today)
        key = {"strike": sc.get("strike"), "expiration": sc.get("expiration"),
               "contracts": sc.get("contracts")}
        below = (last_close is not None and sc.get("strike") is not None
                 and float(last_close) < float(sc["strike"]))
        confirmed = below and (price is None or float(price) < float(sc["strike"]))
        if confirmed and TriggerRule.DEFEND_BELOW_STRIKE not in triggers:
            triggers[TriggerRule.DEFEND_BELOW_STRIKE] = {
                "short": key, "last_close": last_close, "price": price,
                "condition_first_true_at": _first_close_below(tk.get("bars"), sc.get("strike")),
            }
        earn = tk.get("earnings") or {}
        if earn.get("warning") and TriggerRule.EARNINGS_WINDOW not in triggers:
            triggers[TriggerRule.EARNINGS_WINDOW] = {"short": key, "earnings": earn}
        if es.get("assignment_risk") and TriggerRule.DIVIDEND_ASSIGNMENT_RISK not in triggers:
            triggers[TriggerRule.DIVIDEND_ASSIGNMENT_RISK] = {
                "short": key, "assignment_risk": es["assignment_risk"]}
        if es.get("roll_now") and TriggerRule.ROLL_75PCT not in triggers:
            triggers[TriggerRule.ROLL_75PCT] = {
                "short": key, "decay_pct": es.get("decay_pct"), "dte": sc.get("dte")}
        dte = sc.get("dte")
        if (dte is not None and int(dte) <= config.EXPIRY_WARN_DTE
                and TriggerRule.ROLL_SCHEDULED_WEEKLY not in triggers):
            triggers[TriggerRule.ROLL_SCHEDULED_WEEKLY] = {"short": key, "dte": dte}

    features = {
        "price": price, "last_close": last_close,
        "rs3m_vs_spy": tk.get("rs3m_vs_spy"), "rs3m_vs_sector": tk.get("rs3m_vs_sector"),
        "atr": tk.get("atr"), "atr_direction": tk.get("atr_direction"),
        "hist_vol": tk.get("hist_vol"), "iv_rank": tk.get("iv_rank"),
        "pct_above_ma21": tk.get("pct_above_ma21"),
        "regime": {k: (market.get("regime") or {}).get(k) for k in ("status", "lights")},
        "posture": market.get("posture"),
        "q": q, "leap_dte": leap_dte, "planned_exit_dte": planned,
        "whipsaw": {k: ws.get(k) for k in ("defensive_rolls", "roll_drag", "drag_pct", "tripped")},
        "delta_coverage": {k: cov.get(k) for k in ("min_leg_delta", "long_delta", "short_delta")},
        "shorts": [{"strike": sc.get("strike"), "dte": sc.get("dte"),
                    "expiration": sc.get("expiration"),
                    "decay_pct": position_manager.enrich_short(
                        sc, price if price is not None else last_close,
                        position.get("dividend"), today=today).get("decay_pct")}
                   for sc in position.get("short_calls", [])],
        "kill_switch_status": ks["status"],
    }
    return {"triggers": triggers, "features": features}


def _dominant(triggers: dict) -> tuple[str, str] | None:
    """(trigger_rule, action_type) of the dominant fired trigger, exits first."""
    for rule in _EXIT_PRIORITY:
        if rule in triggers:
            return rule, ActionType.EXIT
    for rule in _ROLL_PRIORITY:
        if rule in triggers:
            return rule, _TRIGGER_ACTION[rule]
    return None


_KILL_EXIT_CODE = {
    TriggerRule.KILL_RS_SECTOR: "KILL_SWITCH_SECTOR",
    TriggerRule.KILL_RS_SPY_CONFIRMED: "KILL_SWITCH_SPY",
    TriggerRule.WHIPSAW_GUARD: "WHIPSAW_BREAKER",
    TriggerRule.DELTA_COVERAGE_FLOOR: "DELTA_COVERAGE",
    TriggerRule.JUICE_HURDLE_FAIL: "OPERATOR_DISCRETION",
    TriggerRule.DTE_PLANNED_EXIT: "OPERATOR_DISCRETION",
}

_ROLL_REASON = {
    TriggerRule.DEFEND_BELOW_STRIKE: "defend",
    TriggerRule.EARNINGS_WINDOW: "earnings",
    TriggerRule.DIVIDEND_ASSIGNMENT_RISK: "defend",
    TriggerRule.ROLL_75PCT: "75%-rule",
    TriggerRule.ROLL_SCHEDULED_WEEKLY: "scheduled",
}


def _build_action_rec(position: dict, market: dict, now: datetime,
                      rule: str, action_type: str, triggers: dict,
                      features: dict) -> dict:
    t = position.get("ticker", "")
    tk = _tk(market, t)
    q = tk.get("q") or 0.0
    detail = triggers[rule]
    if action_type == ActionType.EXIT:
        code = detail.get("exit_reason_code") or _KILL_EXIT_CODE.get(rule)
        ticket = _exit_ticket(position, tk, q, code)
    else:
        sc = None
        want = (detail.get("short") or {})
        for cand in position.get("short_calls", []):
            if cand.get("strike") == want.get("strike") and cand.get("expiration") == want.get("expiration"):
                sc = cand
                break
        sc = sc or (position.get("short_calls") or [{}])[0]
        price = tk.get("price") or tk.get("last_close")
        regime = (market.get("regime") or {}).get("status")
        posture = market.get("posture") or config.DEFAULT_STRIKE_POSTURE
        if rule == TriggerRule.EARNINGS_WINDOW and price is not None and tk.get("atr") is not None:
            pol = strike_policy.suggest_earnings_strike(price, tk["atr"], regime, posture)
        elif price is not None and tk.get("atr") is not None:
            pol = strike_policy.suggest_strike(price, tk["atr"], regime, posture)
        else:
            pol = {}
        dte = sc.get("dte")
        # Same-week roll when the current expiry still has time, else next weekly
        # (mirrors executor.defend_recommendation's default).
        roll_dte = int(dte) if dte else 5
        if rule in (TriggerRule.ROLL_75PCT, TriggerRule.ROLL_SCHEDULED_WEEKLY,
                    TriggerRule.EARNINGS_WINDOW, TriggerRule.DIVIDEND_ASSIGNMENT_RISK):
            roll_dte = (int(dte) if dte else 0) + 7  # roll OUT to the next weekly
        ticket = _roll_ticket(position, sc, tk, new_strike=pol.get("strike"),
                              roll_dte=roll_dte, roll_reason=_ROLL_REASON[rule], q=q)
        ticket["strike_policy"] = pol or None
    snapshot = dict(features)
    snapshot["trigger_detail"] = detail
    snapshot["secondary_triggers"] = sorted(r for r in triggers if r != rule)
    snapshot["condition_first_true_at"] = detail.get("condition_first_true_at")
    return {
        "emitted_at": _iso(now),
        "position_id": t,
        "ticker": t,
        "action_type": action_type,
        "trigger_rule": rule,
        "proposed_ticket": ticket,
        "input_snapshot": snapshot,
        "valid_until": _valid_until(action_type, now),
        "supersedes": None,
        "engine_version": ENGINE_VERSION,
    }


def _all_clear_rec(position: dict, features: dict, now: datetime) -> dict:
    t = position.get("ticker", "")
    return {
        "emitted_at": _iso(now),
        "position_id": t,
        "ticker": t,
        "action_type": ActionType.NO_ACTION,
        "trigger_rule": TriggerRule.ALL_CLEAR,
        "proposed_ticket": None,
        "input_snapshot": features,
        "valid_until": _valid_until(ActionType.NO_ACTION, now),
        "supersedes": None,
        "engine_version": ENGINE_VERSION,
    }


def _enter_rec(candidate: dict, market: dict, now: datetime) -> dict:
    t = candidate.get("ticker", "")
    return {
        "emitted_at": _iso(now),
        "position_id": None,
        "ticker": t,
        "action_type": ActionType.ENTER,
        "trigger_rule": TriggerRule.GATE_ALL_PASS,
        "proposed_ticket": _enter_ticket(candidate, market),
        "input_snapshot": {
            "verdict": candidate.get("verdict"),
            "gate": candidate.get("gate"),
            "level5": candidate.get("level5"),
            "regime": (market.get("regime") or {}).get("status"),
            "juice_weekly_pct": candidate.get("juice_weekly_pct"),
            "blockers": candidate.get("blockers") or [],
        },
        "valid_until": _valid_until(ActionType.ENTER, now),
        "supersedes": None,
        "engine_version": ENGINE_VERSION,
    }


def _entry_blocked(candidate: dict, market: dict) -> list[str]:
    """Worst-signal-wins ENTER verdict: ANY blocking signal anywhere in the
    stack blocks the entry — scorecard verdict not GO, Level 1 regime not
    green, or any Level-5 blocking failure. Returns the blockers (empty ==
    clear to enter). This is the codified aggregation the audit called for:
    the scorecard's own worst-signal verdict + the two hard gates the executor
    actually enforces."""
    blockers = list(candidate.get("blockers") or [])
    verdict = (candidate.get("verdict") or "").upper()
    if verdict != "GO":
        blockers.append(f"scorecard verdict {verdict or 'UNKNOWN'} (worst signal wins)")
    if (market.get("regime") or {}).get("status") != "green":
        blockers.append("Level 1 regime not green — RED/YELLOW blocks new entries")
    l5 = candidate.get("level5") or {}
    for f in l5.get("blocking_failures") or []:
        blockers.append(f"Level 5 {f.get('id') or f}" if isinstance(f, dict) else f"Level 5 {f}")
    return blockers


def _same_proposal(a: dict | None, b: dict | None) -> bool:
    """Material equality of two proposed tickets: same action + same primary
    strike(s) within the $0.50 grid. Estimated prices are NOT material — they
    drift every pass; a re-evaluation that only re-prices does not supersede."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if a.get("action") != b.get("action"):
        return False
    def strikes(x):
        return [leg.get("strike") for leg in (x.get("legs") or [])]
    sa, sb = strikes(a), strikes(b)
    if len(sa) != len(sb):
        return False
    for va, vb in zip(sa, sb):
        if va is None and vb is None:
            continue
        if va is None or vb is None or abs(float(va) - float(vb)) > 0.25:
            return False
    return True


# ---------------------------------------------------------------------------
# The evaluation pass
# ---------------------------------------------------------------------------
def evaluate(market: dict, state: dict, now: datetime,
             open_recs: list[dict] | None = None) -> list[dict]:
    """One full evaluation pass. Returns the NEW Recommendation records to
    append (possibly empty when every open recommendation still stands).

    ``market``   — frozen snapshot (recommendation_runner.build_market_snapshot
                   or a test fixture). Never a live provider.
    ``state``    — the state dict (positions + derived roll ledger are read;
                   nothing is written).
    ``now``      — the injected clock.
    ``open_recs``— currently open (unresolved, unexpired, unsuperseded)
                   recommendations, from trust_derive.open_recommendations.
                   Emission dedup/supersession is decided against these.
    """
    open_recs = open_recs or []
    out: list[dict] = []
    open_by_pos: dict[str, list[dict]] = {}
    for r in open_recs:
        key = r.get("position_id") or f"~enter~{r.get('ticker')}"
        open_by_pos.setdefault(key, []).append(r)

    positions = [p for p in state.get("positions", []) if p.get("status") != "closed"]
    market = dict(market)
    market.setdefault("roll_ledger", (state.get("roll_ledger") or {}).get("rolls", []))

    for p in positions:
        t = p.get("ticker", "")
        evald = _evaluate_position(p, market, now)
        triggers, features = evald["triggers"], evald["features"]
        dom = _dominant(triggers)
        existing = open_by_pos.get(t, [])
        existing_actions = [r for r in existing if r.get("action_type") != ActionType.NO_ACTION]
        existing_clear = [r for r in existing if r.get("action_type") == ActionType.NO_ACTION]

        if dom:
            rule, action_type = dom
            rec = _build_action_rec(p, market, now, rule, action_type, triggers, features)
            same = [r for r in existing_actions
                    if r.get("action_type") == action_type and r.get("trigger_rule") == rule
                    and _same_proposal(r.get("proposed_ticket"), rec["proposed_ticket"])]
            if same:
                continue  # the open record is the claim; no duplicate in-window
            if existing_actions:
                # A re-evaluation replaces the open recommendation (new trigger,
                # new action, or a materially different ticket).
                rec["supersedes"] = max(existing_actions,
                                        key=lambda r: r.get("emitted_at") or "")["rec_id"]
            out.append(rec)
        else:
            if existing_actions:
                # Condition cleared while an action rec is open: supersede it
                # with the explicit all-clear so a later action can't match a
                # stale claim.
                rec = _all_clear_rec(p, features, now)
                rec["supersedes"] = max(existing_actions,
                                        key=lambda r: r.get("emitted_at") or "")["rec_id"]
                out.append(rec)
            elif not existing_clear:
                out.append(_all_clear_rec(p, features, now))
            # else: an unexpired ALL_CLEAR already covers this position.

    # ENTER — worst-signal-wins over the frozen candidate list.
    for candidate in market.get("entry_candidates") or []:
        t = (candidate.get("ticker") or "").upper()
        if not t or any(p.get("ticker", "").upper() == t for p in positions):
            continue
        if _entry_blocked(candidate, market):
            continue
        existing = open_by_pos.get(f"~enter~{t}", [])
        if any(r.get("trigger_rule") == TriggerRule.GATE_ALL_PASS for r in existing):
            continue
        out.append(_enter_rec(candidate, market, now))

    return out
