"""Keep each position in step with the trade log — automatically only when
Schwab agrees.

A position (shares count + open short calls) is a MIRROR updated one trade at a
time; the append-only execution log is the truth. The two drift when trades
reach the log out of order, or when one is voided / restored / adopted /
corrected afterwards. CONFIRMED LIVE: after voiding and importing trades this
week, both books needed "Rebuild from log" by hand, and a stale mirror showed
two short calls on one lot.

A blind rebuild from the log is not safe either: some corrections set the
mirror WITHOUT a trade behind them (an option adjustment, a rebuild from the
broker's holdings), and replaying the log would undo them. So, per ticker:

  * mirror == log            -> nothing to do;
  * mirror != log, and the log's version matches what Schwab last reported
    holding (the last successful reconciliation)  -> rebuilt automatically,
    through the audited ``rebuild_*_from_log`` (a ``position_rebuild`` marker);
  * otherwise                -> HELD for the operator: a POSITION_DRIFT alert,
    and the History tab's Rebuild buttons as before.

Runs right after every operation that changes the log (see ``after_log_change``)
and every morning after reconciliation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import logging_handler as log

logger = log.logger


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _calls(legs) -> list[tuple]:
    out = []
    for l in legs or []:
        c = int(round(abs(_f(l.get("contracts", l.get("quantity"))) or 0)))
        if c > 0:
            out.append((round(_f(l.get("strike")) or 0, 2), c,
                        str(l.get("expiration") or l.get("expiry") or "")[:10] or None))
    return sorted(out, key=lambda t: (t[0], t[2] or "", t[1]))


def _calls_equal(a: list[tuple], b: list[tuple]) -> bool:
    """Same legs by strike + contracts; an expiry must agree only when BOTH
    sides know it (many booked legs never recorded theirs)."""
    rest = list(b)
    for strike, c, exp in a:
        hit = next((x for x in rest if x[0] == strike and x[1] == c
                    and (not exp or not x[2] or exp == x[2])), None)
        if hit is None:
            return False
        rest.remove(hit)
    return not rest


def broker_holdings(state: dict, ticker: str, now: datetime | None = None) -> dict | None:
    """What Schwab last reported holding for ``ticker`` (the last successful,
    reasonably fresh reconciliation), or None when there is no such report."""
    rep = (state.get("reconciliation") or {}).get("last") or {}
    view = rep.get("broker_view")
    if not rep.get("broker_ok") or view is None:
        return None
    try:
        as_of = datetime.strptime(str(rep.get("as_of"))[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        age_h = ((now or datetime.now(timezone.utc)) - as_of).total_seconds() / 3600
        if age_h > float(config.RECONCILE_STALE_HOURS):
            return None
    except ValueError:
        return None
    t = ticker.upper()
    shares = sum(int(round(_f(i.get("quantity")) or 0)) for i in view
                 if (i.get("underlying") or "").upper() == t and i.get("instrument_type") == "EQUITY")
    calls = _calls([i for i in view
                    if (i.get("underlying") or "").upper() == t and i.get("instrument_type") == "OPTION"
                    and i.get("put_call") in ("CALL", "C", None) and (_f(i.get("quantity")) or 0) < 0])
    return {"shares": shares, "calls": calls}


def check(state: dict, tickers=None, now: datetime | None = None) -> list[dict]:
    """Every position part (shares / short calls) whose mirror differs from the
    log, with the verdict: ``heal`` (log matches Schwab) or ``hold``."""
    import executor
    want = {t.upper() for t in tickers} if tickers else None
    out = []
    for p in state.get("positions") or []:
        t = (p.get("ticker") or "").upper()
        if not t or (want is not None and t not in want):
            continue
        broker = broker_holdings(state, t, now)
        mirror_shares = int((p.get("shares") or {}).get("count") or 0)
        log_shares = int(executor.replay_shares_from_log(t, state)["count"])
        if mirror_shares != log_shares:
            ok = broker is not None and broker["shares"] == log_shares
            out.append({"ticker": t, "part": "shares", "mirror": mirror_shares, "log": log_shares,
                        "broker": None if broker is None else broker["shares"],
                        "verdict": "heal" if ok else "hold"})
        mirror_calls = _calls(p.get("short_calls"))
        log_calls = _calls(executor.replay_short_calls(t, state))
        if not _calls_equal(mirror_calls, log_calls):
            ok = broker is not None and _calls_equal(log_calls, broker["calls"])
            out.append({"ticker": t, "part": "short_calls", "mirror": mirror_calls, "log": log_calls,
                        "broker": None if broker is None else broker["calls"],
                        "verdict": "heal" if ok else "hold"})
    return out


def heal(tickers=None, reason: str = "position drifted from the trade log; Schwab agrees with the log") -> dict:
    """Rebuild every drifted part whose log version Schwab confirms; record the
    rest as HELD (``state["position_drift"]``) for the POSITION_DRIFT alert."""
    import executor
    drifts = check(log.load_state(), tickers)
    healed, held = [], []
    for d in drifts:
        if d["verdict"] != "heal":
            held.append(d)
            continue
        try:
            if d["part"] == "shares":
                executor.rebuild_shares_from_log(d["ticker"], reason)
            else:
                executor.rebuild_short_calls_from_log(d["ticker"], reason)
            healed.append(d)
        except Exception as e:  # noqa: BLE001 — a failed heal is held, never raised
            logger.error("position heal failed for %s %s: %s", d["ticker"], d["part"], e)
            held.append(dict(d, verdict="hold", error=str(e)))

    def _store(state: dict) -> None:
        prev = (state.get("position_drift") or {}).get("held") or []
        scope = {t.upper() for t in tickers} if tickers else None
        # A partial run only replaces what it looked at.
        kept = [h for h in prev if scope is not None and h["ticker"] not in scope]
        state["position_drift"] = {"as_of": log.utcnow(), "held": kept + held,
                                   "healed": healed}
    log.mutate_state(_store)
    return {"healed": healed, "held": held}


def after_log_change(tickers) -> None:
    """Best-effort heal after an operation changed the log for ``tickers``.
    Never raises into (or undoes) the operation that called it."""
    tickers = sorted({str(t).upper() for t in (tickers or []) if t})
    if not tickers:
        return
    try:
        heal(tickers)
    except Exception as e:  # noqa: BLE001
        logger.error("position heal after log change failed for %s: %s", tickers, e)


def held(state: dict) -> list[dict]:
    return (state.get("position_drift") or {}).get("held") or []
