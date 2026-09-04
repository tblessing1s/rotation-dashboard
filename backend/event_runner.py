"""Event-driven recommendation passes — run the engine when a condition FLIPS,
not only at the scheduled slots.

The tiered quote poller already fetches every open position's price on a
2-minute cadence during market hours and already watches for a price crossing
a defense level or the index moving hard. This module closes the last gap:
when one of those events fires, or a short call crosses a roll threshold on
the fresh print, the recommendation engine runs NOW for the account instead
of at the next slot, so the card is on the phone within minutes of the
condition turning true.

What it is not:

* Not a new rule set. The signals watched here are the SAME position-manager
  signals the engine reads (``enrich_short`` — the 75% buyback rule, the
  extrinsic-captured threshold, assignment risk) plus the poller's own
  escalations. An event run calls the same ``recommendation_runner.run`` the
  scheduled pass calls, over a fresh snapshot; dedup against the open claims
  is the engine's, unchanged, so an extra pass never duplicates a card.
* Not a level trigger. A run fires on an EDGE — a signal newly true since the
  last cycle for that name — never on "still true", and a per-ticker cooldown
  plus a global minimum gap bound the cost even on a whipsawing tape.
* Not intraday authority for close-only rules. The kill switch confirms on the
  close and the circuit breaker reads daily bars; an event run evaluates them
  off the same cached bars an intraday scheduled slot would, so nothing here
  can trip them on an intraday print that a slot could not.

The scheduled slots stay as the floor: an event run is an early pass, never a
replacement. ``CFM_EVENT_RUNS=0`` turns it off.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")

logger = logging.getLogger("cfm.eventrun")

# Signal keys, per ticker. Named for the engine rule they lead to.
ROLL_75 = "roll_75"                         # -> ROLL_75PCT
EXTRINSIC_CAPTURED = "extrinsic_captured"   # -> ROLL_EXTRINSIC_CAPTURED
ASSIGNMENT_RISK = "assignment_risk"         # -> DIVIDEND_ASSIGNMENT_RISK
DEFENSE = "defense"                         # poller defense escalation (level breached)
MARKET = "market"                           # poller market escalation (SPY / sector move)


def enabled() -> bool:
    """Event runs on by default; CFM_EVENT_RUNS=0 leaves the scheduled slots alone."""
    return os.environ.get("CFM_EVENT_RUNS", "1").strip() not in ("0", "false", "no")


def detect_signals(state: dict, quotes: dict, today=None) -> dict[str, set[str]]:
    """The roll-family signals that are TRUE right now for each open position
    that got a fresh quote this cycle, read through the same enrich_short the
    engine and the position card use. Pure over its inputs.

    Only tickers present in ``quotes`` are reported — a name that was not polled
    this cycle has nothing new to say, and the gate keeps its previous read.
    """
    import position_manager
    out: dict[str, set[str]] = {}
    for pos in state.get("positions", []) or []:
        if pos.get("status") == "closed":
            continue
        t = (pos.get("ticker") or "").upper()
        q = quotes.get(t) or {}
        price = q.get("price")
        if not t or price is None:
            continue
        sig: set[str] = set()
        for sc in pos.get("short_calls", []) or []:
            try:
                es = position_manager.enrich_short(sc, float(price), pos.get("dividend"),
                                                   today=today,
                                                   position_type=pos.get("position_type"))
            except Exception as e:  # noqa: BLE001 — one leg must not sink the cycle
                logger.debug("enrich_short failed for %s: %s", t, e)
                continue
            if es.get("roll_now"):
                sig.add(ROLL_75)
            captured = es.get("extrinsic_captured_pct")
            dte = sc.get("dte")
            if (captured is not None and dte is not None and int(dte) >= 1
                    and float(captured) >= config.ROLL_EXTRINSIC_CAPTURED_PCT):
                sig.add(EXTRINSIC_CAPTURED)
            if es.get("assignment_risk"):
                sig.add(ASSIGNMENT_RISK)
        out[t] = sig
    return out


class EventGate:
    """Edge detection + rate limiting, process-local (like the poller's own
    cadence state: a restart primes on its first cycle and runs nothing).

    ``observe`` returns the reasons that warrant a run now: signals newly true
    for a name since its last read, plus escalations this cycle, minus names
    inside their cooldown; and nothing at all inside the global minimum gap.
    """

    def __init__(self) -> None:
        self._seen: dict[str, set[str]] = {}
        self._primed = False
        self._last_run_by_ticker: dict[str, datetime] = {}
        self._last_run_at: datetime | None = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._primed = False
            self._last_run_by_ticker.clear()
            self._last_run_at = None

    def observe(self, signals: dict[str, set[str]], escalation_symbols=(),
                market_escalation: bool = False, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(ET)
        with self._lock:
            edges: list[tuple[str, str]] = []
            for t, sig in signals.items():
                before = self._seen.get(t, set())
                for s in sorted(sig - before):
                    edges.append((t, s))
                self._seen[t] = set(sig)
            for t in escalation_symbols or ():
                edges.append(((t or "").upper(), DEFENSE))
            if market_escalation:
                edges.append(("MARKET", MARKET))
            if not self._primed:
                # First cycle after a start: record where things stand, run
                # nothing — the scheduled slots already covered the day so far,
                # and "everything currently true" is not an event.
                self._primed = True
                return []
            if not edges:
                return []
            gap = float(config.EVENT_RUN_MIN_GAP_SECONDS)
            if self._last_run_at is not None and (now - self._last_run_at).total_seconds() < gap:
                return []
            cooldown = float(config.EVENT_RUN_COOLDOWN_SECONDS)
            due = []
            for t, s in edges:
                last = self._last_run_by_ticker.get(t)
                if last is not None and (now - last).total_seconds() < cooldown:
                    continue
                due.append(f"{t}:{s}")
            return due

    def mark_ran(self, reasons: list[str], now: datetime) -> None:
        with self._lock:
            self._last_run_at = now
            for r in reasons:
                self._last_run_by_ticker[r.split(":", 1)[0]] = now


_gate = EventGate()


def reset() -> None:
    _gate.reset()


def maybe_run(state: dict, quotes: dict, escalation_symbols=(), market_escalation: bool = False,
              now: datetime | None = None) -> dict | None:
    """One poller cycle's worth of event detection; runs the engine when an
    edge is due. Returns the run summary when a pass ran, else None. Best-effort:
    a failure is logged and never reaches the poller."""
    if not enabled() or config.demo_enabled():
        return None
    try:
        signals = detect_signals(state, quotes, today=(now.date() if now else None))
        reasons = _gate.observe(signals, escalation_symbols, market_escalation, now)
        if not reasons:
            return None
        # Hand the poller's prints to the quote cache so the engine's snapshot
        # reads the SAME price the event was detected on, without a re-fetch.
        import data_handler
        for sym, q in (quotes or {}).items():
            if q and q.get("price") is not None:
                data_handler._remember_quote(
                    sym.upper(), {"symbol": sym.upper(), "price": float(q["price"]),
                                  "source": q.get("source") or "schwab"})
        import recommendation_runner
        logger.info("event-driven recommendation pass: %s", ", ".join(reasons))
        summary = recommendation_runner.run(trigger={"kind": "event", "reasons": reasons})
        _gate.mark_ran(reasons, now or datetime.now(ET))
        return summary
    except Exception as e:  # noqa: BLE001
        logger.warning("event-driven recommendation pass failed: %s", e)
        return None


def status() -> dict:
    with _gate._lock:
        return {
            "enabled": enabled(),
            "primed": _gate._primed,
            "last_run_at": _gate._last_run_at.isoformat() if _gate._last_run_at else None,
            "cooldown_seconds": config.EVENT_RUN_COOLDOWN_SECONDS,
            "min_gap_seconds": config.EVENT_RUN_MIN_GAP_SECONDS,
            "watching": {t: sorted(s) for t, s in _gate._seen.items()},
        }
