"""Why autopilot did — or did not — act: a capped decision log plus a live diagnosis.

Autopilot used to decline silently in several places (a trigger not granted, no real
expiration or priced strike found, an unreadable chain), leaving "I turned it on and no
trade happened" with nothing to look at. Every recommendation the unattended paths
consider now lands here with an outcome and a plain reason, and ``diagnose`` lines the
standing preconditions up next to what each open position is currently measuring.

Read-only with respect to trading: nothing here places, cancels or changes an order.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import logging_handler as log

_KEY = "autopilot_log"
_CAP = 100


def record(ticker: str | None, rule: str | None, rec_id: str | None, result: str,
           reason: str = "", **extra) -> None:
    """Append one decision (result: placed / exited / skipped / failed). A repeat of the
    SAME outcome for the same recommendation (every event pass re-checks it) is not
    re-logged. Never raises — a logging problem must not disturb the pass."""
    entry = {"at": log.utcnow(), "ticker": ticker, "trigger_rule": rule, "rec_id": rec_id,
             "result": result, "reason": reason, **extra}

    def _apply(st):
        rows = st.setdefault("metadata", {}).setdefault(_KEY, [])
        if rows and all(rows[-1].get(k) == entry[k] for k in ("rec_id", "result", "reason")):
            return
        rows.append(entry)
        del rows[:-_CAP]

    try:
        log.mutate_state(_apply)
    except Exception as e:  # noqa: BLE001
        log.logger.warning("autopilot decision log write failed: %s", e)


def recent(state: dict | None = None, limit: int = 30) -> list[dict]:
    state = state if state is not None else log.load_state()
    return list(reversed((state.get("metadata") or {}).get(_KEY) or []))[:limit]


def _check(id_: str, label: str, ok, detail: str, *, level: str | None = None) -> dict:
    return {"id": id_, "label": label, "ok": ok, "detail": detail,
            "level": level or ("ok" if ok else "bad")}


def _age(ts, now) -> str:
    try:
        t = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "unknown"
    s = int((now - t).total_seconds())
    return f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 172800 else f"{s // 86400} d ago"


def _watch_rows(state: dict, params: dict) -> list[dict]:
    latest: dict[str, dict] = {}
    for r in state.get("recommendations") or []:
        t = (r.get("ticker") or "").upper()
        if t and (t not in latest or str(r.get("emitted_at")) >= str(latest[t].get("emitted_at"))):
            latest[t] = r
    rows = []
    for p in state.get("positions") or []:
        if p.get("status") == "closed":
            continue
        t = (p.get("ticker") or "").upper()
        rec = latest.get(t)
        snap = (rec or {}).get("input_snapshot") or {}
        price = snap.get("price")
        shorts = []
        for s in snap.get("shorts") or []:
            strike = s.get("strike")
            dist = ((float(price) / float(strike) - 1) * 100
                    if price is not None and strike else None)
            shorts.append({
                "strike": strike, "dte": s.get("dte"),
                "extrinsic_captured_pct": s.get("extrinsic_captured_pct"),
                "mark_source": s.get("mark_source"),
                "extrinsic_threshold_pct": params["extrinsic_capture_pct"],
                "distance_pct": None if dist is None else round(dist, 2),
                "band_pct": params["near_strike_band_pct"]})
        rows.append({"ticker": t, "as_of": (rec or {}).get("emitted_at"), "price": price,
                     "regime": (snap.get("regime") or {}).get("status"),
                     "latest_call": (rec or {}).get("trigger_rule"), "shorts": shorts})
    return rows


def diagnose(state: dict | None = None, now: datetime | None = None) -> dict:
    """Standing preconditions, per-position readings and the recent decisions."""
    import alert_scheduler
    import autopilot
    import autopilot_params
    import circuit_breaker
    import executor
    import recommendation_auto_execute as auto_exec
    import recommendation_runner
    import trust_derive
    from rec_types import ActionType, TriggerRule

    state = state if state is not None else log.load_state()
    now = now or datetime.now(timezone.utc)
    params = autopilot_params.resolve(state)
    st = autopilot.status(state)
    checks = [
        _check("master", "Autopilot switch", st["enabled"],
               "On." if st["enabled"] else "OFF — nothing acts unattended."),
        _check("granted", "Triggers granted", bool(st["granted"]),
               f"{len(st['granted'])} armed." if st["granted"]
               else "None. Grant the triggers you want in the lists on this tab — "
                    "without a grant autopilot never acts."),
    ]
    live = executor.live_transmit()
    checks.append(_check(
        "mode", "Live or paper", True,
        "Live — orders are sent to Schwab." if live else
        "PAPER — an auto-trade is only logged in this app; nothing is sent to Schwab. "
        "Turn on live trading in Settings for real orders.",
        level="ok" if live else "warn"))
    sched = alert_scheduler.enabled() and alert_scheduler.recommendations_enabled()
    checks.append(_check(
        "scheduler", "Evaluation passes enabled", sched,
        "Scheduled and event-driven passes are on." if sched else
        "Scheduled recommendation passes are switched off on this server "
        "(CFM_ALERTS_SCHEDULER / CFM_RECOMMENDATIONS)."))
    last = recommendation_runner.last_run() or {}
    if not last:
        checks.append(_check("pass", "Last evaluation pass", False,
                             "No pass has run yet on this server since it started."))
    elif last.get("reconcile_frozen"):
        checks.append(_check(
            "pass", "Last evaluation pass", False,
            f"BLOCKED by the reconciliation freeze ({', '.join(last.get('frozen_tickers') or [])}): "
            f"{last.get('freeze_reason') or 'the book disagrees with Schwab'}. "
            "No recommendations are produced until it's resolved."))
    else:
        checks.append(_check(
            "pass", "Last evaluation pass", True,
            f"{_age(last.get('at'), now)}; {last.get('emitted', 0)} new recommendation(s), "
            f"{last.get('positions_evaluated', 0)} position(s) evaluated."))

    perms_exit = circuit_breaker.get_auto_exit_permissions(state)
    perms_roll = auto_exec.get_permissions(state)
    open_actionable = [r for r in trust_derive.open_recommendations(state, now)
                       if r.get("action_type") != ActionType.NO_ACTION]
    pending = []
    for r in open_actionable:
        rule = r.get("trigger_rule")
        if rule == TriggerRule.CIRCUIT_BREAKER:
            cond = ((((r.get("input_snapshot") or {}).get("trigger_detail") or {})
                     .get("circuit_breaker") or {}).get("nearest_trigger") or {}).get("condition")
            eligible, why = (bool(cond and perms_exit.get(cond)),
                             f"exit level {cond or '?'}" + ("" if cond and perms_exit.get(cond)
                                                            else " is not granted for auto-exit"))
        elif rule in auto_exec.AUTO_EXECUTE_TRIGGERS:
            eligible = bool(perms_roll.get(rule))
            why = "granted" if eligible else "not granted"
        else:
            eligible, why = False, "autopilot never acts on this trigger — it only recommends"
        pending.append({"ticker": r.get("ticker"), "trigger_rule": rule,
                        "action_type": r.get("action_type"), "emitted_at": r.get("emitted_at"),
                        "autopilot_will_act": eligible and st["enabled"], "why": why})
    checks.append(_check(
        "open", "Open recommendations", True,
        f"{len(open_actionable)} open, {sum(1 for p in pending if p['autopilot_will_act'])} "
        "that autopilot will act on." if open_actionable else
        "None — no trigger is currently met on any open position, so there is nothing to trade.",
        level="ok" if open_actionable else "info"))
    try:
        import accounts
        acct = accounts.active()
        account = {"id": acct.get("id"), "label": acct.get("label") or acct.get("id")}
    except Exception:  # noqa: BLE001 — the label is informational only
        account = None
    return {"account": account, "checks": checks, "open": pending,
            "watch": _watch_rows(state, params), "decisions": recent(state),
            "mode": "live" if live else "paper"}
