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
from datetime import date, datetime, timedelta
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
    "ASSIGNMENT_RISK": ("HIGH", "HARD_CFM_RULE: short extrinsic below the coming dividend invites early assignment"),
    "TOKEN_EXPIRY": ("HIGH", "PROPOSED_DEFAULT: Schwab refresh token dies at ~7 days; re-auth by day 5"),
    "BUYBACK_75": ("MEDIUM", "HARD_CFM_RULE: 75% of the sale premium captured with >2 DTE -> roll early"),
    "EARNINGS_WINDOW": ("MEDIUM", "HARD_CFM_RULE: roll deep-ITM or exit before the report"),
    "EXPIRY_FRIDAY": ("MEDIUM", "HARD_CFM_RULE: weekly shorts are rolled, never left to expire unmanaged"),
    "DATA_STALE": ("MEDIUM", "PROPOSED_DEFAULT: cached OHLCV older than expected on a market day"),
}


def _alert(type_: str, ticker: str | None, message: str, action: str,
           data: dict | None = None, key: str = "") -> dict:
    severity, rule = ALERT_TYPES[type_]
    fingerprint = "|".join(x for x in (type_, ticker or "", key) if x)
    return {
        "type": type_, "severity": severity, "rule": rule,
        "ticker": ticker, "message": message, "action": action,
        "data": data or {}, "fingerprint": fingerprint,
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
        price = _last_close(t)
        if price is None:
            continue
        atr_val = indicators.atr(data_handler.get_daily(t))
        for sc in p.get("short_calls", []):
            strike = sc.get("strike")
            if strike is None or price >= strike:
                continue
            sp = None
            if atr_val:
                if regime_status is None:
                    regime_status = screening.regime().get("status")
                sp = strike_policy.suggest_strike(price, atr_val, regime_status)
            suggestion = sp["strike"] if sp else None
            out.append(_alert(
                "DEFEND_POSITION", t,
                f"{t} closed at {price:.2f}, below the short strike {strike}.",
                (f"Defensive roll-down: new strike ≈ {suggestion} "
                 f"({sp['atr_mult']:g}×ATR / {sp['itm_pct'] * 100:g}% ITM floor, "
                 f"{sp['posture']} posture)." if sp
                 else "Defensive roll-down: roll to a strike further below price."),
                {"price": round(price, 2), "short_strike": strike,
                 "suggested_strike": suggestion, "atr": round(atr_val, 2) if atr_val else None,
                 "atr_mult": sp["atr_mult"] if sp else None,
                 "itm_pct": sp["itm_pct"] if sp else None,
                 "posture": sp["posture"] if sp else None},
                key=str(strike)))
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


def check_assignment_risk(state: dict) -> list[dict]:
    out = []
    today = datetime.now(ET).date()
    for p in _open_positions(state):
        t = p.get("ticker", "")
        div = p.get("dividend") or {}
        ex_date, amount = div.get("ex_date"), div.get("amount")
        if not ex_date or not amount:
            continue
        try:
            ex = datetime.strptime(str(ex_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if ex < today:
            continue
        price = _last_close(t)
        for sc in p.get("short_calls", []):
            expiry = _short_expiry(sc, today)
            if expiry is None or ex > expiry:
                continue
            strike, current = sc.get("strike"), sc.get("current_bid")
            if strike is None or current is None or price is None:
                continue
            extrinsic = max(float(current) - max(price - float(strike), 0.0), 0.0)
            if extrinsic < float(amount):
                out.append(_alert(
                    "ASSIGNMENT_RISK", t,
                    (f"{t} short {strike} extrinsic {extrinsic:.2f}/sh is below the "
                     f"{float(amount):.2f} dividend going ex {ex_date}."),
                    ("Roll the short before the ex-div date (or accept assignment: the short "
                     "is covered by a LEAP, not stock, so assignment creates SHORT STOCK that "
                     "owes the dividend — usually roll)."),
                    {"strike": strike, "extrinsic": round(extrinsic, 2),
                     "dividend": float(amount), "ex_date": ex_date},
                    key=f"{strike}:{ex_date}"))
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


EVALUATORS = [
    check_kill_switch,
    check_circuit_breaker,
    check_delta_uncovered,
    check_defend_position,
    check_buyback_75,
    check_assignment_risk,
    check_earnings_window,
    check_expiry_friday,
    check_token_expiry,
    check_data_stale,
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
