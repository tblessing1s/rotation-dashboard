"""Circuit breaker — the per-position exit rule (the line-in-the-sand).

This module is the single source of truth for what a position's circuit breaker
IS. A position is a hard EXIT on WHICHEVER of these trips first:

  1. Drawdown    — the underlying has fallen >= CIRCUIT_BREAKER_DROP_PCT (15%)
                   from the price it was entered at.
  2. Fast-MA     — CIRCUIT_BREAKER_MA_FAST_CLOSES (3) consecutive daily closes
                   below the CIRCUIT_BREAKER_MA_FAST-day (50) moving average.
  3. Slow-MA     — a single close below the CIRCUIT_BREAKER_MA_SLOW-day (200) MA.
  4. Manual line — the operator's line-in-the-sand stored at entry, if any.

Any one of these is a breach. ``evaluate`` returns every condition's state plus
the overall verdict; the alert engine (alerts.check_circuit_breaker) and the
Positions view read the verdict from here so the definition lives in one place,
exactly like kill_switch.py owns the RS exit rule.
"""
from __future__ import annotations

import config
import data_handler
import indicators


def _round(v) -> float | None:
    return round(float(v), 2) if v is not None else None


def entry_price(position: dict) -> float | None:
    """The underlying's price when the position was opened — the reference the
    drawdown leg measures against. Stored on the circuit_breaker at entry (and
    backfilled onto older positions by the state migration). None when it can't
    be resolved, in which case the drawdown leg simply stays inert."""
    cb = position.get("circuit_breaker") or {}
    ep = cb.get("entry_price")
    return float(ep) if ep is not None else None


def evaluate(position: dict, df=None) -> dict:
    """Evaluate every circuit-breaker condition for one position.

    ``df`` (daily OHLCV) is loaded from the cache when not supplied, so this
    works offline / in demo mode like the rest of the app. Best-effort: missing
    price data leaves the affected condition untripped rather than raising.
    """
    ticker = position.get("ticker", "")
    if df is None:
        df = data_handler.get_daily(ticker)
    price = indicators.last(df)
    cb = position.get("circuit_breaker") or {}

    # 1. Drawdown from entry.
    entry = entry_price(position)
    drop_line = _round(entry * (1 - config.CIRCUIT_BREAKER_DROP_PCT)) if entry else None
    drop_pct = _round((price - entry) / entry * 100) if entry and price is not None else None
    drawdown = {
        "id": "drawdown",
        "label": f"{config.CIRCUIT_BREAKER_DROP_PCT * 100:g}% drop from entry",
        "tripped": bool(drop_line is not None and price is not None and price <= drop_line),
        "detail": {"entry_price": _round(entry), "line": drop_line,
                   "price": _round(price), "change_pct": drop_pct},
    }

    # 2. Consecutive closes below the fast MA.
    below = indicators.consecutive_closes_below_sma(df, config.CIRCUIT_BREAKER_MA_FAST)
    fast = {
        "id": "ma_fast",
        "label": (f"{config.CIRCUIT_BREAKER_MA_FAST_CLOSES} closes below the "
                  f"{config.CIRCUIT_BREAKER_MA_FAST}-day MA"),
        "tripped": bool(below is not None and below >= config.CIRCUIT_BREAKER_MA_FAST_CLOSES),
        "detail": {"consecutive_closes_below": below,
                   "threshold": config.CIRCUIT_BREAKER_MA_FAST_CLOSES},
    }

    # 3. Close below the slow MA.
    ma_slow = indicators.sma(df, config.CIRCUIT_BREAKER_MA_SLOW)
    slow = {
        "id": "ma_slow",
        "label": f"close below the {config.CIRCUIT_BREAKER_MA_SLOW}-day MA",
        "tripped": bool(price is not None and ma_slow is not None and price < ma_slow),
        "detail": {"price": _round(price), "ma": _round(ma_slow)},
    }

    # 4. Operator line-in-the-sand set at entry (whichever comes first).
    line = cb.get("price")
    manual = {
        "id": "manual_line",
        "label": "operator line-in-the-sand",
        "tripped": bool(line is not None and price is not None and price <= float(line)),
        "detail": {"price": _round(price), "line": _round(line)},
    }

    conditions = [drawdown, fast, slow, manual]
    breached = [c for c in conditions if c["tripped"]]
    tripped = bool(breached)

    # A soft "approaching" band so the Positions card can warn before the breach:
    # one close away from the fast-MA trip, or already two-thirds of the way to
    # the drawdown line.
    approaching = []
    if not tripped:
        if below is not None and below == config.CIRCUIT_BREAKER_MA_FAST_CLOSES - 1:
            approaching.append(fast["id"])
        if (drop_pct is not None
                and drop_pct <= -config.CIRCUIT_BREAKER_DROP_PCT * 100 * (2 / 3)):
            approaching.append(drawdown["id"])

    status = "red" if tripped else ("yellow" if approaching else "green")
    reasons = [c["label"] for c in breached]
    if tripped:
        headline = "circuit breaker breached — " + "; ".join(reasons)
        action = f"EXIT {ticker} — {' and '.join(reasons)}."
    elif approaching:
        headline = "approaching the circuit breaker"
        action = f"Watch {ticker} — a circuit-breaker condition is one step from tripping."
    else:
        headline = "circuit breaker intact"
        action = "Hold — no circuit-breaker condition tripped."

    return {
        "ticker": ticker,
        "price": _round(price),
        "status": status,
        "tripped": tripped,
        "alert": tripped,
        "conditions": conditions,
        "tripped_conditions": [c["id"] for c in breached],
        "approaching": approaching,
        "headline": headline,
        "suggested_action": action,
    }


def evaluate_all(state: dict) -> list[dict]:
    out = []
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        out.append(evaluate(p))
    return out


# Circuit-breaker condition id -> coded exit reason (exit_reasons.ExitReason).
# One member per real condition, including the operator line-in-the-sand.
_CONDITION_EXIT_CODE = {
    "drawdown": "CB_DRAWDOWN_15",
    "ma_fast": "CB_MA50_3CLOSE",
    "ma_slow": "CB_MA200_CLOSE",
    "manual_line": "CB_MANUAL_LINE",
}


def exit_reason_code(evaluation: dict) -> str | None:
    """The coded exit reason a breach implies, or None when nothing is tripped.
    Takes the FIRST tripped condition in evaluation order (drawdown, fast-MA,
    slow-MA, manual line) so the reason is set at the point the breaker fires.
    Advisory: circuit_breaker never closes on its own."""
    import exit_reasons
    for cid in evaluation.get("tripped_conditions") or []:
        code = _CONDITION_EXIT_CODE.get(cid)
        if code and exit_reasons.is_valid(code):
            return code
    return None
