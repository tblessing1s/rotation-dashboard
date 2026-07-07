"""Alert engine — the conditions an operator with a day job cannot watch for.

Each condition is one evaluator: a pure-ish function over (state, cached market
data) that returns zero or more candidate alerts. ``run`` evaluates everything,
dedups against the *active* set persisted in state.json (a condition fires once
when it trips, not on every scheduled run), auto-resolves conditions that have
cleared, appends fired alerts to the capped log, and hands only the NEW alerts
to the notifier. All market inputs come from the parquet cache / state, so the
engine works identically in demo mode and offline.

Rule provenance for each condition is carried on the alert record (``rule``):
HARD_CFM_RULE conditions restate a CFM discipline; PROPOSED_DEFAULT ones are
operational guards with tunable thresholds (see config.py).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import data_handler
import earnings
import indicators
import kill_switch
import logging_handler as log
import notifier
import schwab_api

ET = ZoneInfo("America/New_York")

# type -> (severity, rule provenance)
ALERT_TYPES = {
    "KILL_SWITCH_SECTOR": ("CRITICAL", "HARD_CFM_RULE: RS3M vs Sector negative -> exit immediately"),
    "KILL_SWITCH_SPY": ("CRITICAL", "HARD_CFM_RULE: RS3M vs SPY negative on confirmed close -> exit within 1-2 days"),
    "CIRCUIT_BREAKER": ("CRITICAL", "HARD_CFM_RULE: line-in-the-sand exit price stored at entry"),
    "DELTA_UNCOVERED": ("HIGH", "HARD_CFM_RULE: LEAP below 0.50 delta (or below the short's delta) no longer covers the short"),
    "DEFEND_POSITION": ("HIGH", "HARD_CFM_RULE: underlying closed below the short strike -> defensive roll-down"),
    "WHIPSAW_EXIT": ("CRITICAL", "HARD_CFM_RULE: defend whipsaw (too many roll-downs / too much cumulative drag) -> exit, not another defend"),
    "ASSIGNMENT_RISK": ("HIGH", "HARD_CFM_RULE: short extrinsic below the coming dividend invites early assignment"),
    "TOKEN_EXPIRY": ("HIGH", "PROPOSED_DEFAULT: Schwab refresh token dies at ~7 days; re-auth by day 5"),
    "BUYBACK_75": ("MEDIUM", "HARD_CFM_RULE: 75% of the sale premium captured with >2 DTE -> roll early"),
    "EARNINGS_WINDOW": ("MEDIUM", "HARD_CFM_RULE: roll deep-ITM or exit before the report"),
    "EARNINGS_DATE_STALE": ("MEDIUM", "PROPOSED_DEFAULT: a held name's earnings date hasn't refreshed recently (or providers disagree) -> the guardrail may be running blind"),
    "EXPIRY_FRIDAY": ("MEDIUM", "HARD_CFM_RULE: weekly shorts are rolled, never left to expire unmanaged"),
    "DATA_STALE": ("MEDIUM", "PROPOSED_DEFAULT: cached OHLCV older than expected on a market day"),
    "LEAP_ROLL_DUE": ("HIGH", "PROPOSED_DEFAULT: LEAP DTE below the floor or extrinsic runway too short -> roll the long leg"),
    "CAPITAL_BURN": ("HIGH", "PROPOSED_DEFAULT: weekly juice not covering LEAP decay -> the flywheel is running backwards"),
    "JUICE_INADEQUATE": ("MEDIUM", "HARD_CFM_RULE: trailing weekly juice below the strategy's income target while capital is intact -> reassess/redeploy"),
    "BOOK_CORRELATION": ("MEDIUM", "PROPOSED_DEFAULT: two open underlyings too correlated / beta-adjusted book delta too concentrated -> the 1/sector diversification is thinner than it looks"),
    "DELTA_VELOCITY": ("MEDIUM", "PROPOSED_DEFAULT: LEAP delta bleeding fast while still above the 0.50 floor"),
    "SHORT_STOCK_DETECTED": ("CRITICAL", "HARD_CFM_RULE: assignment created short stock against the LEAP -> buy back the stock, never exercise the LEAP"),
    "RECONCILE_DIRTY": ("HIGH", "HARD_CFM_RULE: state.json diverged from the broker account -> freeze the position, resolve before trading it"),
    "RECONCILE_STALE": ("MEDIUM", "PROPOSED_DEFAULT: reconciliation has not run successfully within the expected window -> the safety check is silent"),
}


# Alert -> deep link into the app. Roll-type alerts open the roll ticket for the
# ticker (RollModal already pre-selects the policy strike); the rest focus the
# affected position card so the operator lands on it, not the tab. The value:
# the decision engine already decided — the tap shouldn't make you rebuild it.
_ROLL_ACTIONS = {
    "BUYBACK_75": "75%-rule",
    "EXPIRY_FRIDAY": "scheduled",
    "DEFEND_POSITION": "defend",
    "ASSIGNMENT_RISK": "defend",
    "EARNINGS_WINDOW": "earnings",
}
_FOCUS_ACTIONS = {
    "KILL_SWITCH_SECTOR", "KILL_SWITCH_SPY", "CIRCUIT_BREAKER", "SHORT_STOCK_DETECTED",
    "DELTA_UNCOVERED", "DELTA_VELOCITY", "LEAP_ROLL_DUE", "CAPITAL_BURN", "RECONCILE_DIRTY",
    "WHIPSAW_EXIT", "JUICE_INADEQUATE", "EARNINGS_DATE_STALE",
}


def _action_url(type_: str, ticker: str | None) -> str | None:
    if not ticker:
        return None
    from urllib.parse import quote
    t = quote(ticker)
    if type_ in _ROLL_ACTIONS:
        return f"/?action=roll&ticker={t}&reason={quote(_ROLL_ACTIONS[type_])}"
    if type_ in _FOCUS_ACTIONS:
        return f"/?action=focus&ticker={t}"
    return None


def _alert(type_: str, ticker: str | None, message: str, action: str,
           data: dict | None = None, key: str = "") -> dict:
    severity, rule = ALERT_TYPES[type_]
    fingerprint = "|".join(x for x in (type_, ticker or "", key) if x)
    return {
        "type": type_, "severity": severity, "rule": rule,
        "ticker": ticker, "message": message, "action": action,
        "data": data or {}, "fingerprint": fingerprint,
        "action_url": _action_url(type_, ticker),
    }


def _open_positions(state: dict) -> list[dict]:
    return [p for p in state.get("positions", []) if p.get("status") != "closed"]


def _last_close(ticker: str) -> float | None:
    return indicators.last(data_handler.get_daily(ticker))


def _short_sold_per_share(sc: dict) -> float | None:
    contracts = int(sc.get("contracts") or 0)
    total = sc.get("entry_premium_total")
    if total is None or not contracts:
        return None
    return float(total) / (contracts * 100)


def _short_expiry(sc: dict, today: date) -> date | None:
    exp = sc.get("expiration")
    if exp:
        try:
            return datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    dte = sc.get("dte")
    return today + timedelta(days=int(dte)) if dte is not None else None


# ---------------------------------------------------------------------------
# Condition evaluators — each returns a list of candidate alerts.
# ---------------------------------------------------------------------------
def check_kill_switch(state: dict) -> list[dict]:
    out = []
    for ev in kill_switch.evaluate_all(state):
        t = ev["ticker"]
        if ev.get("rs3m_vs_sector") is not None and ev["rs3m_vs_sector"] < 0:
            out.append(_alert(
                "KILL_SWITCH_SECTOR", t,
                f"{t} RS3M vs Sector turned negative ({ev['rs3m_vs_sector']}%).",
                f"EXIT {t} immediately.", {"kill_switch": ev}))
        elif ev.get("rs3m_vs_spy") is not None and ev["rs3m_vs_spy"] < 0:
            out.append(_alert(
                "KILL_SWITCH_SPY", t,
                f"{t} RS3M vs SPY negative on close ({ev['rs3m_vs_spy']}%).",
                f"Exit {t} within 1-2 days (confirm on close).", {"kill_switch": ev}))
    return out


def check_delta_uncovered(state: dict) -> list[dict]:
    out = []
    for p in _open_positions(state):
        t, leap = p.get("ticker", ""), p.get("leap") or {}
        contracts = int(leap.get("contracts") or 0)
        if not leap or not contracts:
            continue
        price = _last_close(t)
        leap_mark = (float(leap["current_bid"]) / (contracts * 100)
                     if leap.get("current_bid") is not None else None)
        leap_delta, _ = indicators.call_greeks(price, leap.get("strike"), leap.get("dte"), leap_mark)
        if leap_delta is None:
            continue
        if leap_delta < config.LEAP_DELTA_FLOOR:
            out.append(_alert(
                "DELTA_UNCOVERED", t,
                f"{t} LEAP delta {leap_delta:.2f} is below the {config.LEAP_DELTA_FLOOR:.2f} floor.",
                "The LEAP no longer tracks the stock — roll it down/out or exit the position.",
                {"leap_delta": leap_delta}, key="floor"))
        for sc in p.get("short_calls", []):
            short_delta, _ = indicators.call_greeks(price, sc.get("strike"), sc.get("dte"),
                                                    sc.get("current_bid"))
            if short_delta is not None and leap_delta < short_delta:
                out.append(_alert(
                    "DELTA_UNCOVERED", t,
                    f"{t} long delta {leap_delta:.2f} < short delta {short_delta:.2f} "
                    f"(short {sc.get('strike')}).",
                    "The diagonal is net-short deltas — roll the short up/out or deepen the LEAP.",
                    {"leap_delta": leap_delta, "short_delta": short_delta,
                     "short_strike": sc.get("strike")}, key=f"inverted:{sc.get('strike')}"))
    return out


def check_buyback_75(state: dict) -> list[dict]:
    out = []
    for p in _open_positions(state):
        t = p.get("ticker", "")
        for sc in p.get("short_calls", []):
            sold = _short_sold_per_share(sc)
            current = sc.get("current_bid")
            dte = sc.get("dte")
            if not sold or current is None or dte is None or dte <= config.BUYBACK_MIN_DTE:
                continue
            decayed = 1 - float(current) / sold
            if decayed >= config.BUYBACK_DECAY_PCT:
                out.append(_alert(
                    "BUYBACK_75", t,
                    f"{t} short {sc.get('strike')} has decayed {decayed * 100:.0f}% "
                    f"(sold {sold:.2f}, now {float(current):.2f}) with {dte} DTE.",
                    "Roll early to capture juice — buy it back and sell the next week now.",
                    {"strike": sc.get("strike"), "decayed_pct": round(decayed * 100, 1),
                     "sold": round(sold, 2), "current": float(current), "dte": dte},
                    key=f"{sc.get('strike')}:{sc.get('open_date')}"))
    return out


def check_defend_position(state: dict) -> list[dict]:
    import screening
    import strike_policy

    out = []
    regime_status = None
    for p in _open_positions(state):
        t = p.get("ticker", "")
        close = _last_close(t)
        if close is None:
            continue
        shorts = p.get("short_calls", [])
        # Close-confirmed rule: only positions whose short strike the last daily
        # close sits below are candidates. Skip (and don't spend a live quote on)
        # anything the close hasn't breached.
        if not any(sc.get("strike") is not None and close < float(sc["strike"]) for sc in shorts):
            continue
        # ...but the operator acts on the live price, so a stock that closed below
        # the strike and has since recovered above it intraday isn't breached now.
        # Require the live price below the strike too before flagging a defend.
        live = data_handler.live_price(t)
        price = live if live is not None else close
        atr_val = indicators.atr(data_handler.get_daily(t))
        for sc in shorts:
            strike = sc.get("strike")
            if strike is None or close >= float(strike) or price >= float(strike):
                continue
            sp = None
            if atr_val:
                if regime_status is None:
                    regime_status = screening.regime().get("status")
                sp = strike_policy.suggest_strike(price, atr_val, regime_status)
            suggestion = sp["strike"] if sp else None
            headline = (
                f"{t} at {live:.2f} (last close {close:.2f}), below the short strike {strike}."
                if live is not None and abs(live - close) >= 0.005
                else f"{t} closed at {close:.2f}, below the short strike {strike}.")
            out.append(_alert(
                "DEFEND_POSITION", t,
                headline,
                (f"Defensive roll-down: new strike ≈ {suggestion} "
                 f"({sp['atr_mult']:g}×ATR / {sp['itm_pct'] * 100:g}% ITM floor, "
                 f"{sp['posture']} posture)." if sp
                 else "Defensive roll-down: roll to a strike further below price."),
                {"price": round(price, 2), "last_close": round(close, 2),
                 "live_price": round(live, 2) if live is not None else None,
                 "short_strike": strike,
                 "suggested_strike": suggestion, "atr": round(atr_val, 2) if atr_val else None,
                 "atr_mult": sp["atr_mult"] if sp else None,
                 "itm_pct": sp["itm_pct"] if sp else None,
                 "posture": sp["posture"] if sp else None},
                key=str(strike)))
    return out


def check_whipsaw_exit(state: dict) -> list[dict]:
    """Cumulative defend-whipsaw guard: a position taking roll-down after
    roll-down in a slow grind bleeds via drag while neither the RS kill switch
    nor the price circuit breaker trips. Fires when too many defensive rolls
    landed in the trailing window OR cumulative roll drag has passed a fraction
    of the position's capital — recommend EXIT, not another defend."""
    import position_manager
    out = []
    rolls = (state.get("roll_ledger") or {}).get("rolls", [])
    for p in _open_positions(state):
        t = p.get("ticker", "")
        ws = position_manager.whipsaw_status(p, rolls)
        if not ws["tripped"]:
            continue
        out.append(_alert(
            "WHIPSAW_EXIT", t,
            f"{t} is in a defend whipsaw — " + "; ".join(ws["reasons"]) + ".",
            "Stop defending — the roll-down spiral is bleeding the position while the "
            "kill switch and circuit breaker stay quiet. EXIT and redeploy the capital; "
            "another roll-down just locks a lower strike.",
            ws, key="whipsaw"))
    return out


def check_circuit_breaker(state: dict) -> list[dict]:
    out = []
    for p in _open_positions(state):
        t = p.get("ticker", "")
        cb = p.get("circuit_breaker") or {}
        line = cb.get("price")
        if line is None:
            continue
        price = _last_close(t)
        if price is not None and price <= float(line):
            out.append(_alert(
                "CIRCUIT_BREAKER", t,
                f"{t} at {price:.2f} has hit the line-in-the-sand ({float(line):.2f}).",
                f"EXIT {t} — the circuit-breaker price set at entry has been breached.",
                {"price": round(price, 2), "line_in_the_sand": float(line)}))
    return out


def check_earnings_window(state: dict) -> list[dict]:
    out = []
    for p in _open_positions(state):
        t = p.get("ticker", "")
        try:
            earn = earnings.next_earnings(t)
        except Exception:  # noqa: BLE001 — earnings lookup must not sink the run
            continue
        if earn.get("warning"):
            out.append(_alert(
                "EARNINGS_WINDOW", t,
                f"{t} reports earnings in {earn['days_until']}d ({earn['date']}).",
                "Roll the short deep-ITM for protection or exit before the report.",
                {"earnings": earn}, key=str(earn.get("date"))))
    return out


def check_earnings_date_stale(state: dict) -> list[dict]:
    """The earnings guardrail runs on a date from a free-tier calendar that's
    often wrong or late-updated, and a wrong date fails silently. Flag a held
    name whose earnings date hasn't refreshed within EARNINGS_STALE_DAYS (nightly
    maintenance refreshes held names daily, so a stale date means the refresh path
    is broken) OR whose providers disagree — so the silence itself pages."""
    if config.demo_enabled():  # ops condition about the real calendar, not the demo store
        return []
    out = []
    for p in _open_positions(state):
        t = p.get("ticker", "")
        try:
            info = earnings.cached_earnings(t)  # non-fetching read
        except Exception:  # noqa: BLE001 — a cache read must not sink the run
            continue
        stale, conflict = info.get("stale"), info.get("conflict")
        if not stale and not conflict:
            continue
        if conflict:
            msg = (f"{t} earnings date disagrees between providers "
                   f"(Alpha Vantage {info.get('av_date')} vs Schwab {info.get('schwab_date')}).")
            action = ("Confirm the real report date before the cycle spans it — a wrong date "
                      "silently disarms the earnings guardrail. Set an override if needed.")
        else:
            msg = (f"{t} earnings date hasn't refreshed in > {config.EARNINGS_STALE_DAYS}d "
                   f"(last {info.get('fetched_at') or 'never'}).")
            action = ("Refresh it (nightly maintenance / GET /api/earnings?refresh=1) and check "
                      "the provider — a stale date can let you roll into a report unprotected.")
        out.append(_alert(
            "EARNINGS_DATE_STALE", t, msg, action,
            {"earnings": info}, key="conflict" if conflict else "stale"))
    return out


def check_assignment_risk(state: dict) -> list[dict]:
    """Early-assignment risk. The base trigger is an EXTRINSIC one: an ITM short
    whose remaining time value has collapsed below ASSIGNMENT_EXTRINSIC_FLOOR is
    assignable any time (the counterparty forfeits nothing by exercising), no
    ex-date required. The coming dividend is an ESCALATION: extrinsic below the
    dividend before ex-div makes early exercise rational on a specific date. At
    most one alert per short — the dividend escalation preempts the bare floor."""
    out = []
    today = datetime.now(ET).date()
    for p in _open_positions(state):
        t = p.get("ticker", "")
        price = _last_close(t)
        if price is None:
            continue
        div = p.get("dividend") or {}
        ex_date, amount = div.get("ex_date"), div.get("amount")
        ex = None
        if ex_date and amount:
            try:
                ex = datetime.strptime(str(ex_date)[:10], "%Y-%m-%d").date()
            except ValueError:
                ex = None
            if ex is not None and ex < today:
                ex = None  # already gone ex — no longer a dividend-capture trigger
        for sc in p.get("short_calls", []):
            strike, current, dte = sc.get("strike"), sc.get("current_bid"), sc.get("dte")
            if strike is None or current is None:
                continue
            intrinsic = max(price - float(strike), 0.0)
            extrinsic = max(float(current) - intrinsic, 0.0)
            itm = price > float(strike)
            expiry = _short_expiry(sc, today)

            # Escalation: the short spans a dividend its extrinsic no longer covers.
            if (ex is not None and amount is not None and expiry is not None
                    and ex <= expiry and extrinsic < float(amount)):
                out.append(_alert(
                    "ASSIGNMENT_RISK", t,
                    (f"{t} short {strike} extrinsic {extrinsic:.2f}/sh is below the "
                     f"{float(amount):.2f} dividend going ex {ex_date}."),
                    ("Roll the short before the ex-div date (or accept assignment: the short "
                     "is covered by a LEAP, not stock, so assignment creates SHORT STOCK that "
                     "owes the dividend — usually roll)."),
                    {"strike": strike, "extrinsic": round(extrinsic, 2), "trigger": "dividend",
                     "dividend": float(amount), "ex_date": ex_date},
                    key=f"{strike}:{ex_date}"))
                continue

            # Base: an ITM short with near-zero extrinsic is assignable any time.
            if (itm and dte is not None and dte > 0
                    and extrinsic < config.ASSIGNMENT_EXTRINSIC_FLOOR):
                out.append(_alert(
                    "ASSIGNMENT_RISK", t,
                    (f"{t} short {strike} extrinsic {extrinsic:.2f}/sh has collapsed below "
                     f"the {config.ASSIGNMENT_EXTRINSIC_FLOOR:.2f} floor (deep ITM, {dte} DTE) "
                     f"— assignable any time, no ex-div required."),
                    ("Roll the short up/out to re-establish time value (or accept assignment "
                     "deliberately: the short is covered by a LEAP, not stock, so assignment "
                     "creates SHORT STOCK — never exercise the LEAP to cover)."),
                    {"strike": strike, "extrinsic": round(extrinsic, 2), "trigger": "extrinsic",
                     "floor": config.ASSIGNMENT_EXTRINSIC_FLOOR, "dte": dte},
                    key=f"{strike}:extrinsic"))
    return out


def check_expiry_friday(state: dict) -> list[dict]:
    out = []
    for p in _open_positions(state):
        t = p.get("ticker", "")
        for sc in p.get("short_calls", []):
            dte = sc.get("dte")
            if dte is not None and dte <= config.EXPIRY_WARN_DTE:
                out.append(_alert(
                    "EXPIRY_FRIDAY", t,
                    f"{t} short {sc.get('strike')} expires in {dte} day(s) and is not rolled.",
                    "Roll to next week (or let it expire deliberately and sell the next short).",
                    {"strike": sc.get("strike"), "dte": dte},
                    key=f"{sc.get('strike')}:{sc.get('expiration') or dte}"))
    return out


def check_token_expiry(state: dict) -> list[dict]:
    if config.demo_enabled():  # ops condition about the real provider, not the demo store
        return []
    status = schwab_api.token_status()
    if not status.get("present") or status.get("daysLeft") is None:
        return []
    days_left = float(status["daysLeft"])
    age_days = schwab_api.REFRESH_TOKEN_TTL_DAYS - days_left
    if age_days < config.TOKEN_WARN_AGE_DAYS:
        return []
    return [_alert(
        "TOKEN_EXPIRY", None,
        f"Schwab refresh token is {age_days:.1f} days old — {max(days_left, 0):.1f} days left.",
        "Re-authorize now (Schwab card -> Reconnect) before market data goes dark.",
        {"token": status}, key=str(status.get("mintedAt")))]


def check_data_stale(state: dict) -> list[dict]:
    if config.demo_enabled():  # demo cache is synthetic; staleness is meaningless
        return []
    now = datetime.now(ET)
    if now.weekday() >= 5:  # weekend — an old cache is expected
        return []
    age = data_handler.cache_age_hours(config.BENCHMARK)
    if age is None or age <= config.DATA_STALE_HOURS:
        return []
    return [_alert(
        "DATA_STALE", None,
        f"Cached OHLCV for {config.BENCHMARK} is {age:.0f}h old on a market day.",
        "Check provider health (/api/data-status) — kill-switch math is running on stale data.",
        {"symbol": config.BENCHMARK, "cache_age_hours": age},
        key=now.strftime("%Y-%m-%d"))]


def _cur_iso_week() -> str:
    now = datetime.now(ET)
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def _completed_week_juice(state: dict, ticker: str) -> list[float]:
    """Net juice per COMPLETED week for a ticker (oldest→newest) from the derived
    theta ledger — the weekly series behind the juice-vs-burn maintenance check."""
    cur = _cur_iso_week()
    rows = [r for r in (state.get("theta_ledger") or {}).get("weeks", [])
            if r.get("ticker") == ticker and r.get("week", "") < cur]
    rows.sort(key=lambda r: r["week"])
    return [float(r.get("net_juice") or 0) for r in rows]


def check_leap_roll_due(state: dict) -> list[dict]:
    """The long leg needs rolling: DTE below the floor OR extrinsic runway worth
    less than a few weeks of juice (leap_policy.roll_policy)."""
    import leap_policy
    out = []
    for p in _open_positions(state):
        if not (p.get("leap") or {}):
            continue
        t = p.get("ticker", "")
        health = leap_policy.leap_health(p)
        if not health.get("roll_due"):
            continue
        est = leap_policy.roll_cost_estimate(t, position=p, state=state)
        debit = est.get("net_debit")
        reserve_ok = est.get("reserve_ok")
        note = "" if reserve_ok is not False else " (⚠ breaches the 2×ATR cash reserve)"
        out.append(_alert(
            "LEAP_ROLL_DUE", t,
            (f"{t} LEAP roll due — " + "; ".join(health["roll_reasons"]) + "."),
            (f"Roll the LEAP into a fresh ~{config.LEAP_TARGET_DTE}-DTE / "
             f"~{config.LEAP_TARGET_DELTA:.2f}-delta long"
             + (f"; est. net debit ${debit:,.0f}{note}." if debit is not None else ".")),
            {"leap_dte": health.get("leap_dte"),
             "extrinsic_weeks_remaining": health.get("leap_extrinsic_weeks_remaining"),
             "reasons": health["roll_reasons"], "roll_cost": est},
            key="roll"))
    return out


def check_capital_burn(state: dict) -> list[dict]:
    """Weekly juice has not covered the LEAP's own decay for
    MAINTENANCE_NEGATIVE_WEEKS consecutive completed weeks — the diagonal is
    losing time value faster than the shorts collect it. Uses the current BS
    weekly burn as the decay proxy across those recent weeks (burn moves slowly
    week to week)."""
    import leap_policy
    out = []
    for p in _open_positions(state):
        if not (p.get("leap") or {}):
            continue
        t = p.get("ticker", "")
        health = leap_policy.leap_health(p)
        burn = health.get("leap_weekly_burn")
        if burn is None:
            continue
        weekly = _completed_week_juice(state, t)
        n = config.MAINTENANCE_NEGATIVE_WEEKS
        if len(weekly) < n:
            continue
        recent = weekly[-n:]
        if all(j - burn < 0 for j in recent):
            shortfall = round(burn - (sum(recent) / len(recent)), 2)
            out.append(_alert(
                "CAPITAL_BURN", t,
                (f"{t} juice has not covered LEAP decay for {n} weeks "
                 f"(avg ${sum(recent) / len(recent):,.0f}/wk vs ${burn:,.0f}/wk burn)."),
                "The flywheel is running backwards — roll the LEAP deeper/longer "
                "or reassess the position; check juice adequacy.",
                {"trailing_avg_weekly_juice": health.get("trailing_avg_weekly_juice"),
                 "leap_weekly_burn": burn, "shortfall_per_week": shortfall,
                 "weeks": n, "recent_weekly_juice": recent},
                key="burn"))
    return out


def check_juice_inadequate(state: dict) -> list[dict]:
    """Ongoing juice-vs-target check. Juice adequacy is gated once at entry, but
    if IV collapses mid-cycle a position can keep rolling while its realized
    weekly juice falls below the strategy's income target. This owns the wide band
    between "still covers LEAP theta" (self-funding — capital_burn owns the
    below-theta extreme) and "no longer clears the income target": the position
    quietly underperforms while its capital is still intact to redeploy. Warms up
    once there are enough completed weeks of trailing juice."""
    import leap_policy
    out = []
    for p in _open_positions(state):
        if not (p.get("leap") or {}):
            continue
        t = p.get("ticker", "")
        health = leap_policy.leap_health(p)
        if health.get("juice_adequate") is not False:  # None (warming up) / True -> skip
            continue
        if health.get("maintenance_status") == "burning":  # capital_burn owns this regime
            continue
        yld, tgt = health.get("weekly_juice_yield_pct"), health.get("juice_target_pct")
        if yld is None or tgt is None:
            continue
        out.append(_alert(
            "JUICE_INADEQUATE", t,
            (f"{t} trailing weekly juice is {yld:g}% of LEAP capital, below the {tgt:g}% "
             f"income target (last {config.JUICE_TRAILING_WEEKS} completed weeks)."),
            ("This position still funds its own decay but no longer clears the strategy's "
             "income target — roll to a better strike/week, or redeploy the capital into a "
             "candidate that pays before it erodes."),
            {"weekly_juice_yield_pct": yld, "juice_target_pct": tgt,
             "trailing_avg_weekly_juice": health.get("trailing_avg_weekly_juice"),
             "trailing_weeks": config.JUICE_TRAILING_WEEKS,
             "maintenance_status": health.get("maintenance_status")},
            key="income"))
    return out


def check_delta_velocity(state: dict) -> list[dict]:
    """LEAP delta has dropped more than DELTA_VELOCITY_DROP over the last
    DELTA_VELOCITY_WINDOW sessions while still ABOVE the 0.50 floor (below the
    floor, DELTA_UNCOVERED owns it — don't double-fire). A warning tier, not a
    directive: points at the kill-switch / circuit-breaker panels."""
    import leap_policy
    out = []
    for p in _open_positions(state):
        if not (p.get("leap") or {}):
            continue
        t = p.get("ticker", "")
        health = leap_policy.leap_health(p)
        vel = health.get("delta_velocity") or {}
        drop, end = vel.get("drop"), vel.get("end")
        leap_delta = health.get("leap_delta")
        if drop is None or end is None or leap_delta is None:
            continue
        if leap_delta <= config.LEAP_DELTA_FLOOR:  # floor alert owns this regime
            continue
        if drop > config.DELTA_VELOCITY_DROP:
            to_floor = round(leap_delta - config.LEAP_DELTA_FLOOR, 4)
            out.append(_alert(
                "DELTA_VELOCITY", t,
                (f"{t} LEAP delta fell {drop:.2f} over {vel['window']} sessions "
                 f"({vel['start']:.2f}→{vel['end']:.2f}), {to_floor:.2f} above the "
                 f"{config.LEAP_DELTA_FLOOR:.2f} floor."),
                "Delta is bleeding fast — review the kill-switch and circuit-breaker "
                "panels; a LEAP roll-down may be needed before the floor is hit.",
                {"start_delta": vel["start"], "end_delta": vel["end"],
                 "window": vel["window"], "drop": drop,
                 "distance_to_floor": to_floor},
                key="velocity"))
    return out


def _open_recon_diffs(state: dict) -> list[dict]:
    """Still-open diffs from the last reconciliation report (unresolved, unacked,
    non-benign). Empty when the last run failed or the report is clean."""
    import reconcile
    report = (state.get("reconciliation") or {}).get("last") or {}
    if not report.get("broker_ok"):
        return []
    return [d for d in report.get("diffs", []) if reconcile._diff_open(d)]


def check_short_stock_detected(state: dict) -> list[dict]:
    """Assignment happened: the broker holds SHORT STOCK against an open LEAP.
    Highest severity, its OWN fingerprint so it escalates even when
    RECONCILE_DIRTY has already fired for the same ticker."""
    import reconcile
    out = []
    for d in _open_recon_diffs(state):
        if d["classification"] != reconcile.SHORT_STOCK_DETECTED:
            continue
        t = d["ticker"]
        out.append(_alert(
            "SHORT_STOCK_DETECTED", t,
            (f"{t}: {d['broker_qty']} short shares detected against an open LEAP — "
             f"assignment likely occurred."),
            ("Assignment likely occurred. Do NOT exercise the LEAP to cover — buy back "
             "the short stock or close the position. Exercising forfeits all remaining "
             "LEAP extrinsic."),
            {"diff": d}, key=d["id"]))
    return out


def check_reconcile_dirty(state: dict) -> list[dict]:
    """state.json diverged from the broker on at least one non-benign diff. One
    alert per frozen ticker; the payload carries the per-diff one-liners."""
    import reconcile
    by_ticker: dict[str, list[dict]] = {}
    for d in _open_recon_diffs(state):
        by_ticker.setdefault(d["ticker"], []).append(d)
    out = []
    for t, diffs in by_ticker.items():
        # Non-short-stock diffs still fire RECONCILE_DIRTY; the short-stock ones
        # ALSO get SHORT_STOCK_DETECTED. Keep the count honest — include all.
        lines = [d["summary"] for d in diffs]
        classes = sorted({d["classification"] for d in diffs})
        out.append(_alert(
            "RECONCILE_DIRTY", t,
            f"{t}: reconciliation found {len(diffs)} unresolved diff(s) — " + "; ".join(lines),
            "Position frozen for review — resolve each diff (book expiry / record an "
            "adjustment / acknowledge) before trading it again.",
            {"diffs": diffs, "classifications": classes},
            key="|".join(d["id"] for d in diffs)))
    return out


def check_book_correlation(state: dict) -> list[dict]:
    """Two positions can satisfy the 1-per-sector cap while being ~0.9 correlated
    (e.g. a mega-cap in XLK and one in XLC) — the book is really one bet. Warn on
    high trailing correlation between open underlyings, or when the beta-adjusted
    book delta says the 'spread' is one directional bet. Book-level (no ticker)."""
    import portfolio_risk
    conc = portfolio_risk.concentration(state)
    if not conc.get("warn"):
        return []
    out = []
    for hp in conc.get("high_correlation_pairs", []):
        out.append(_alert(
            "BOOK_CORRELATION", None,
            (f"{hp['a']} and {hp['b']} are {hp['correlation']:.2f} correlated — the book's "
             f"1-per-sector diversification is thinner than it looks."),
            "Two open underlyings move together, so a single shock hits both — trim or "
            "avoid adding more correlated exposure.",
            {"pair": hp, "correlation_threshold": conc.get("correlation_threshold")},
            key=f"corr:{hp['a']}:{hp['b']}"))
    lev, bar = conc.get("beta_adj_leverage"), conc.get("beta_adj_leverage_threshold")
    if lev is not None and bar is not None and lev >= bar:
        out.append(_alert(
            "BOOK_CORRELATION", None,
            (f"Beta-adjusted book delta is {lev:g}× deployed capital — the book is "
             f"effectively one directional (index-beta) bet."),
            "Beta-adjusted delta concentration is high: the positions aren't diversifying — "
            "reassess sizing / net directional exposure.",
            {"net_beta_adj_delta_dollars": conc.get("net_beta_adj_delta_dollars"),
             "beta_adj_leverage": lev, "threshold": bar}, key="leverage"))
    return out


def check_reconcile_stale(state: dict) -> list[dict]:
    """Reconciliation hasn't run successfully within RECONCILE_STALE_HOURS while
    Schwab is connected and positions are open. Silence is itself a failure
    signal (the positions call failing, the scheduler wedged, etc.)."""
    if config.demo_enabled():  # ops condition about the real provider / scheduler
        return []
    if not schwab_api.configured():
        return []
    open_pos = _open_positions(state)
    if not open_pos:
        return []
    recon = state.get("reconciliation") or {}
    last_success = recon.get("last_success")
    age_h = None
    if last_success:
        try:
            ts = datetime.strptime(str(last_success)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        except ValueError:
            age_h = None
    if last_success and age_h is not None and age_h <= config.RECONCILE_STALE_HOURS:
        return []
    detail = (f"last successful run {age_h:.0f}h ago" if age_h is not None
              else "no successful run recorded")
    return [_alert(
        "RECONCILE_STALE", None,
        f"Position reconciliation is stale — {detail} (threshold {config.RECONCILE_STALE_HOURS}h).",
        "Check the Schwab connection and the scheduler — the state-vs-broker safety "
        "check is not running. Trigger it from the Checklist tab (Reconcile now).",
        {"last_success": last_success, "age_hours": round(age_h, 1) if age_h is not None else None},
        key="stale")]


EVALUATORS = [
    check_kill_switch,
    check_circuit_breaker,
    check_whipsaw_exit,
    check_delta_uncovered,
    check_defend_position,
    check_buyback_75,
    check_assignment_risk,
    check_earnings_window,
    check_earnings_date_stale,
    check_expiry_friday,
    check_token_expiry,
    check_data_stale,
    check_leap_roll_due,
    check_capital_burn,
    check_juice_inadequate,
    check_delta_velocity,
    check_short_stock_detected,
    check_reconcile_dirty,
    check_reconcile_stale,
    check_book_correlation,
]


# ---------------------------------------------------------------------------
# Settings, dedup and the run loop
# ---------------------------------------------------------------------------
def get_settings(state: dict) -> dict:
    s = (state.get("alerts") or {}).get("settings") or {}
    return {
        "enabled": s.get("enabled") or {},      # type -> bool, missing = enabled
        "channels": s.get("channels") or {},    # channel name -> bool, missing = enabled
        "dry_run": s.get("dry_run") if s.get("dry_run") is not None
                   else config.alerts_dry_run_default(),
    }


def evaluate(state: dict) -> list[dict]:
    """All candidate alerts for the current state. One evaluator failing (e.g. a
    provider hiccup inside kill_switch) never sinks the others."""
    enabled = get_settings(state)["enabled"]
    candidates = []
    for fn in EVALUATORS:
        try:
            for a in fn(state):
                if enabled.get(a["type"], True):
                    candidates.append(a)
        except Exception as e:  # noqa: BLE001 — keep evaluating the rest
            notifier.logger.error("alert evaluator %s failed: %s", fn.__name__, e)
    return candidates


_run_lock = threading.Lock()


def run(notify: bool = True, dry_run: bool | None = None) -> dict:
    """One evaluator pass: evaluate -> dedup -> persist -> notify new alerts.

    Safe to call repeatedly (scheduler, HTTP trigger, restarts): an already-active
    fingerprint only refreshes last_seen. Serialized so a scheduled run and a
    manual HTTP trigger can't interleave their read-modify-write on state.
    Returns a summary for the API/UI.
    """
    with _run_lock:
        return _run_locked(notify, dry_run)


def _run_locked(notify: bool, dry_run: bool | None) -> dict:
    state = log.load_state()
    alerts_state = state.setdefault("alerts", {})
    active: dict = alerts_state.setdefault("active", {})
    log_list: list = alerts_state.setdefault("log", [])
    settings = get_settings(state)
    if dry_run is None:
        dry_run = settings["dry_run"]

    now = log.utcnow()
    candidates = evaluate(state)
    current_fps = {a["fingerprint"] for a in candidates}

    new_alerts = []
    for a in candidates:
        fp = a["fingerprint"]
        if fp in active:
            active[fp]["last_seen"] = now
            active[fp]["data"] = a["data"]  # refresh the numbers behind the alert
            continue
        record = {**a, "id": f"alert_{len(log_list) + 1:04d}",
                  "first_seen": now, "last_seen": now,
                  "status": "active", "acknowledged": False}
        active[fp] = record
        log_list.append(record)
        new_alerts.append(record)

    # Conditions that stopped firing resolve automatically.
    resolved = []
    for fp in list(active.keys()):
        if fp not in current_fps:
            rec = active.pop(fp)
            rec["status"] = "resolved"
            rec["resolved_at"] = now
            resolved.append(rec)
            for entry in log_list:  # mirror resolution onto the log entry
                if entry.get("id") == rec.get("id"):
                    entry.update({"status": "resolved", "resolved_at": now})
                    break

    del log_list[:-config.ALERT_LOG_MAX]  # cap history, keep newest
    alerts_state["last_run"] = {"at": now, "evaluated": len(candidates),
                                "fired": len(new_alerts), "resolved": len(resolved),
                                "dry_run": bool(dry_run)}
    log.save_state(state)

    delivery = notifier.dispatch(new_alerts, settings, dry_run=dry_run) if notify else []
    return {"at": now, "fired": new_alerts, "resolved": resolved,
            "active_count": len(active), "delivery": delivery, "dry_run": bool(dry_run)}


def acknowledge(alert_id: str) -> dict:
    state = log.load_state()
    alerts_state = state.setdefault("alerts", {})
    hit = None
    for rec in alerts_state.get("active", {}).values():
        if rec.get("id") == alert_id:
            rec["acknowledged"] = True
            hit = rec
    for rec in alerts_state.get("log", []):
        if rec.get("id") == alert_id:
            rec["acknowledged"] = True
            hit = hit or rec
    if not hit:
        raise ValueError(f"unknown alert id '{alert_id}'")
    log.save_state(state)
    return hit


def update_settings(patch: dict) -> dict:
    state = log.load_state()
    alerts_state = state.setdefault("alerts", {})
    settings = alerts_state.setdefault("settings", {})
    if "enabled" in patch:
        settings.setdefault("enabled", {}).update(
            {k: bool(v) for k, v in (patch["enabled"] or {}).items() if k in ALERT_TYPES})
    if "channels" in patch:
        settings.setdefault("channels", {}).update(
            {k: bool(v) for k, v in (patch["channels"] or {}).items()})
    if "dry_run" in patch:
        settings["dry_run"] = bool(patch["dry_run"]) if patch["dry_run"] is not None else None
    log.save_state(state)
    return get_settings(state)


def view(state: dict | None = None) -> dict:
    state = state or log.load_state()
    alerts_state = state.get("alerts") or {}
    active = sorted(alerts_state.get("active", {}).values(),
                    key=lambda a: (notifier.SEVERITY_ORDER.get(a.get("severity"), 9),
                                   a.get("first_seen", "")))
    return {
        "active": active,
        "log": list(reversed(alerts_state.get("log", []))),  # newest first for the UI
        "settings": get_settings(state),
        "last_run": alerts_state.get("last_run"),
        "types": {k: {"severity": v[0], "rule": v[1]} for k, v in ALERT_TYPES.items()},
    }
