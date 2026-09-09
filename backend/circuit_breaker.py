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

AUTO-EXIT PERMISSIONS (opt-in, per-condition, default OFF): the operator may
grant drawdown / ma_fast / ma_slow individually the authority to close the
position UNATTENDED the moment that specific condition trips — see
get_auto_exit_permissions / set_auto_exit_permission below and
recommendation_runner._check_circuit_breaker_auto_exit, which is the only
caller that acts on them. The manual line is deliberately excluded: it is
freeform, set once at entry, and was never scoped for this. Granting a
condition does not change what evaluate() computes or displays one bit —
it only decides whether recommendation_runner is ALLOWED to call
executor.exit_position() on the SAME rec a human would otherwise have to
click "Execute" on.
"""
from __future__ import annotations

import config
import data_handler
import indicators
import logging_handler as log


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
    ma_fast_value = indicators.sma(df, config.CIRCUIT_BREAKER_MA_FAST)
    fast = {
        "id": "ma_fast",
        "label": (f"{config.CIRCUIT_BREAKER_MA_FAST_CLOSES} closes below the "
                  f"{config.CIRCUIT_BREAKER_MA_FAST}-day MA"),
        "tripped": bool(below is not None and below >= config.CIRCUIT_BREAKER_MA_FAST_CLOSES),
        "detail": {"consecutive_closes_below": below,
                   "threshold": config.CIRCUIT_BREAKER_MA_FAST_CLOSES,
                   "price": _round(price), "ma": _round(ma_fast_value)},
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

    # Every condition's PRICE LEVEL, for the Positions card's "the spot where
    # this trips" readout — not just the pass/fail booleans above. Each is a
    # floor price recomputed fresh from today's data (the MAs move day to
    # day; the drawdown/manual lines are fixed once set). `nearest_trigger` is
    # whichever defined level sits HIGHEST — the level a falling price would
    # cross FIRST, by construction, since every level here is a floor below
    # the current price. It names the level, not a promise: ma_fast's line
    # only STARTS the 3-close clock, it does not trip alone on one touch.
    levels = {}
    if drop_line is not None:
        levels["drawdown"] = drop_line
    if ma_fast_value is not None:
        levels["ma_fast"] = _round(ma_fast_value)
    if ma_slow is not None:
        levels["ma_slow"] = _round(ma_slow)
    if line is not None:
        levels["manual_line"] = _round(float(line))
    nearest_id = max(levels, key=levels.get) if levels else None
    by_id = {c["id"]: c for c in conditions}
    nearest_trigger = ({"condition": nearest_id, "price": levels[nearest_id],
                        "label": by_id[nearest_id]["label"]}
                       if nearest_id else None)

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
        "levels": levels,
        "nearest_trigger": nearest_trigger,
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
    This evaluator itself never closes anything — see the module docstring's
    AUTO-EXIT PERMISSIONS section for the one, explicitly opt-in path that can
    act on the code this returns without a human clicking Execute."""
    import exit_reasons
    for cid in evaluation.get("tripped_conditions") or []:
        code = _CONDITION_EXIT_CODE.get(cid)
        if code and exit_reasons.is_valid(code):
            return code
    return None


# ---------------------------------------------------------------------------
# Auto-exit permissions — see the module docstring. Persisted like
# strike_policy's posture: state.metadata, per-store (live/demo never share
# a grant), surviving restarts. Default OFF for every condition.
# ---------------------------------------------------------------------------
AUTO_EXIT_CONDITIONS = ("drawdown", "ma_fast", "ma_slow")
_AUTO_EXIT_KEY = "circuit_breaker_auto_exit"


def get_auto_exit_permissions(state: dict | None = None) -> dict:
    """{"drawdown": bool, "ma_fast": bool, "ma_slow": bool} — always all three
    keys, defaulting False for any condition never explicitly granted."""
    state = state if state is not None else log.load_state()
    stored = (state.get("metadata") or {}).get(_AUTO_EXIT_KEY) or {}
    return {c: bool(stored.get(c)) for c in AUTO_EXIT_CONDITIONS}


def set_auto_exit_permission(condition: str, on: bool) -> dict:
    """Grant or revoke ONE condition's auto-exit permission. Raises on an
    unrecognized condition rather than silently no-op-ing a typo."""
    if condition not in AUTO_EXIT_CONDITIONS:
        raise ValueError(f"condition must be one of {AUTO_EXIT_CONDITIONS}")
    state = log.load_state()
    perms = state.setdefault("metadata", {}).setdefault(_AUTO_EXIT_KEY, {})
    perms[condition] = bool(on)
    log.save_state(state)
    return get_auto_exit_permissions(state)
