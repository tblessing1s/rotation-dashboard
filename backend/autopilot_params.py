"""The operator-adjustable parameters autopilot works from.

Defaults come from config (read at CALL time, so tests and env overrides still
apply); anything the operator saves overrides them for THIS book only (persisted in
state.metadata, like the grants and the master switch). ``resolve`` is what the
engine, the circuit breaker, the event runner and the roll picker read.

Only the knobs the operator actually tunes are here. Structural rules (the 50/200-day
MA windows, shadow-mode metrics, order safety) are shown read-only by ``describe`` and
are not editable from this module.
"""
from __future__ import annotations

import config
import logging_handler as log

_KEY = "autopilot_params"

# key -> (group, label, kind, unit, min, max, help, default_fn)
SPEC = [
    ("cb_live_first_level", "Exit", "Gap/live exit on the first breaker", "bool", "", None, None,
     "Exit the moment the live price is at or through the first of the drawdown / 200-day MA / "
     "manual levels, without waiting for the close.",
     lambda: bool(config.CIRCUIT_BREAKER_FIRST_LEVEL_LIVE)),
    ("cb_drop_pct", "Exit", "Drawdown trigger", "float", "%", 5.0, 40.0,
     "Exit when the stock falls this far below its highest close since entry.",
     lambda: round(config.CIRCUIT_BREAKER_DROP_PCT * 100, 4)),
    ("cb_ma_fast_closes", "Exit", "Closes below the 50-day MA", "int", "closes", 1, 5,
     "Consecutive daily closes below the 50-day MA before it counts as a breach. "
     "Close-based only; a live price never trips it.",
     lambda: int(config.CIRCUIT_BREAKER_MA_FAST_CLOSES)),
    ("extrinsic_capture_pct", "Roll", "Extrinsic captured", "float", "%", 50.0, 100.0,
     "Roll the short once this much of the extrinsic sold has been captured.",
     lambda: float(config.ROLL_EXTRINSIC_CAPTURED_PCT)),
    ("same_week_min_dte", "Roll", "Stay in the same week with at least", "int", "days left", 1, 5,
     "With at least this many days left a roll stays in the same expiration; with fewer it goes "
     "out to the next weekly.",
     lambda: int(config.ROLL_UP_SAME_WEEK_MIN_DTE)),
    ("near_strike_band_pct", "Defend", "Defend when the stock is within", "float", "% of the strike",
     0.5, 10.0,
     "Roll down once the live price is this close above the short strike (or already below it).",
     lambda: float(config.SHORT_ATM_APPROACH_PCT)),
    ("juice_low_pct", "New strike", "Weekly extrinsic target, low", "float", "% of stock / week",
     0.1, 5.0, "Lower end of the weekly extrinsic the new call should earn.",
     lambda: float(config.AUTOPILOT_JUICE_LOW_PCT)),
    ("juice_high_pct", "New strike", "Weekly extrinsic target, high", "float", "% of stock / week",
     0.1, 5.0, "Upper end of that band.",
     lambda: float(config.AUTOPILOT_JUICE_HIGH_PCT)),
]
_BY_KEY = {s[0]: s for s in SPEC}


def defaults() -> dict:
    return {k: spec[-1]() for k, spec in _BY_KEY.items()}


def _coerce(key: str, value):
    _, _, label, kind, _, lo, hi, _, _ = _BY_KEY[key]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{label} must be on or off")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if kind == "int":
        if num != int(num):
            raise ValueError(f"{label} must be a whole number")
        num = int(num)
    if not (lo <= num <= hi):
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}")
    return num


def _merged(stored: dict) -> dict:
    out = defaults()
    for k, v in (stored or {}).items():
        if k in _BY_KEY:
            try:
                out[k] = _coerce(k, v)
            except ValueError:
                pass            # a bad stored value falls back to the default
    return out


def resolve(state: dict | None = None) -> dict:
    """Effective values for this book: defaults overlaid with the operator's saves."""
    state = state if state is not None else log.load_state()
    return _merged((state.get("metadata") or {}).get(_KEY))


def set_params(values: dict) -> dict:
    """Validate and save ``values`` (a partial dict). Raises ValueError on an unknown
    key, an out-of-range value, or a juice band whose low is not below its high."""
    if not isinstance(values, dict):
        raise ValueError("values must be an object")
    clean = {}
    for k, v in values.items():
        if k not in _BY_KEY:
            raise ValueError(f"unknown parameter: {k}")
        clean[k] = _coerce(k, v)

    def _apply(st):
        stored = dict(st.setdefault("metadata", {}).get(_KEY) or {})
        stored.update(clean)
        merged = _merged(stored)
        if merged["juice_low_pct"] >= merged["juice_high_pct"]:
            raise ValueError("the weekly extrinsic target's low must be below its high")
        st["metadata"][_KEY] = {k: merged[k] for k in stored if k in _BY_KEY}
        return merged

    log.mutate_state(_apply)
    return resolve()


def reset() -> dict:
    log.mutate_state(lambda st: (st.setdefault("metadata", {}).pop(_KEY, None), None)[1])
    return resolve()


def describe(state: dict | None = None) -> dict:
    """Everything the settings pop-up shows: each editable parameter (value, default,
    unit, range, help), the strike posture, the armed triggers, and the fixed rules."""
    import autopilot
    import strike_policy
    state = state if state is not None else log.load_state()
    values = resolve(state)
    dflt = defaults()
    params = [{"key": k, "group": g, "label": label, "kind": kind, "unit": unit,
               "min": lo, "max": hi, "help": help_, "value": values[k], "default": dflt[k]}
              for k, g, label, kind, unit, lo, hi, help_, _ in SPEC]
    st = autopilot.status(state)
    return {
        "params": params,
        "posture": {"value": strike_policy.get_posture(state),
                    "options": list(config.STRIKE_POSTURES)},
        "enabled": st["enabled"],
        "granted": st["granted"],
        "fixed": [
            "Moving averages: 50-day and 200-day",
            "Roll direction: down (in place or out), strike never shallower than the posture's "
            "regime strike",
            f"Unfilled orders are cancelled after {config.PENDING_ORDER_STALE_SECONDS:g}s; "
            f"max {config.MAX_RESUBMIT_ATTEMPTS} attempts per position",
            "Acts only when your book agrees with Schwab, and only on triggers you've granted",
        ],
    }
