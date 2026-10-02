"""Day-trade strategy parameters: a catalog of every DAYTRADE_* tunable plus a
per-thread override layer for what-if replays.

``P`` is a thin proxy the signal engine reads its rule constants through
(``P.DAYTRADE_STOP_ATR_DIVISOR`` instead of ``config.DAYTRADE_STOP_ATR_DIVISOR``).
With no override active it is exactly ``config``; inside ``overriding({...})``
the named values change for THIS thread/context only, so a what-if replay can
never leak into the scheduler's live run (a ContextVar, not a mutated global).
Nothing here writes ``config`` or any persisted file — zero authority.
"""
from __future__ import annotations

import contextlib
import contextvars

import config

_overrides: contextvars.ContextVar[dict] = contextvars.ContextVar("daytrade_overrides", default={})


class _Params:
    def __getattr__(self, name: str):
        ov = _overrides.get()
        if name in ov:
            return ov[name]
        return getattr(config, name)


P = _Params()


@contextlib.contextmanager
def overriding(values: dict):
    token = _overrides.set(dict(values))
    try:
        yield
    finally:
        _overrides.reset(token)


# (group, config key, label, kind, replayable, help)
#   kind: "float" | "int" | "str"
#   replayable: True when the signal engine reads it, so a what-if replay of a
#   saved day honours it. False = screener / scheduling inputs that decided
#   WHICH names were picked and WHEN things run; shown for reference only
#   (the saved picks and bars are fixed history).
_CATALOG = [
    ("Setup & entry", "DAYTRADE_SETUP_VOLUME_MULT", "Setup volume multiple", "float", True,
     "Rule 3 — setup candle volume must be at least this multiple of the symbol's running average 5-min volume."),
    ("Setup & entry", "DAYTRADE_ENTRY_EXPIRY_CANDLES", "Entry expiry (candles)", "int", True,
     "Rule 4 — cancel the setup if the break doesn't trigger within this many candles."),
    ("Stop & targets", "DAYTRADE_STOP_ATR_DIVISOR", "Stop = ATR ÷", "float", True,
     "Rule 5 — stop distance is daily ATR(14) divided by this (1R). Higher = tighter stop."),
    ("Stop & targets", "DAYTRADE_HALF_TARGET_R", "Half-off target (R)", "float", True,
     "Rule 6 — take half the position at this many R, then move the stop to breakeven."),
    ("Stop & targets", "DAYTRADE_FULL_TARGET_R", "Tighten-trail point (R)", "float", True,
     "Once price reaches this many R the trailing stop tightens."),
    ("Stop & targets", "DAYTRADE_TRAIL_LOOSE_R", "Loose trail (R)", "float", True,
     "Trailing distance behind the best price after the half target, before the tighten point."),
    ("Stop & targets", "DAYTRADE_TRAIL_TIGHT_R", "Tight trail (R)", "float", True,
     "Trailing distance once the tighten point is reached."),
    ("Risk & sizing", "DAYTRADE_ACCOUNT_EQUITY", "Budget ($)", "float", True,
     "Capital the sleeve sizes against. Live runs use the primary book's dry powder; this is the replay's stand-in."),
    ("Risk & sizing", "DAYTRADE_RISK_PCT", "Risk per trade (%)", "float", True,
     "Rule 7 — % of budget risked per trade, sized off the stop distance."),
    ("Risk & sizing", "DAYTRADE_MAX_POSITION_PCT", "Max position (% of budget)", "float", True,
     "Capital cap for any single trade."),
    ("Risk & sizing", "DAYTRADE_MAX_DAILY_DEPLOY_PCT", "Max deployed per day (%)", "float", True,
     "Cap on the day's cumulative entry notional."),
    ("Daily guardrails", "DAYTRADE_MAX_TRADES_PER_DAY", "Max trades / day", "int", True,
     "Throughput cap on new entries per day."),
    ("Daily guardrails", "DAYTRADE_MAX_LOSSES_PER_DAY", "Max losing trades / day", "int", True,
     "Halts new entries after this many losing trades."),
    ("Daily guardrails", "DAYTRADE_DAILY_STOP_R", "Daily stop (+R)", "float", True,
     "Halts new entries once cumulative R reaches this for the day."),
    ("Screener", "DAYTRADE_MIN_PRICE", "Min price ($)", "float", False, "Rule 1 — nightly universe screen."),
    ("Screener", "DAYTRADE_MAX_PRICE", "Max price ($)", "float", False, "Rule 1 — nightly universe screen."),
    ("Screener", "DAYTRADE_MIN_AVG_VOLUME", "Min avg volume", "int", False, "Rule 1 — nightly universe screen."),
    ("Screener", "DAYTRADE_AVG_VOLUME_LOOKBACK_DAYS", "Avg volume lookback (days)", "int", False, "Rule 1."),
    ("Screener", "DAYTRADE_ATR_PCT_MIN", "Min ATR %", "float", False, "Rule 1 — nightly universe screen."),
    ("Screener", "DAYTRADE_ATR_PCT_MAX", "Max ATR %", "float", False, "Rule 1 — nightly universe screen."),
    ("Screener", "DAYTRADE_ATR_WINDOW", "ATR window (days)", "int", False, "Daily ATR length used for stops."),
    ("Screener", "DAYTRADE_UNIVERSE_MIN", "Universe min picks", "int", False, "Rule 1."),
    ("Screener", "DAYTRADE_UNIVERSE_MAX", "Universe max picks", "int", False, "Rule 1."),
    ("Schedule & trial", "DAYTRADE_WINDOW_START_ET", "Window start (ET)", "str", False, "Rule 2 — signal window."),
    ("Schedule & trial", "DAYTRADE_WINDOW_END_ET", "Window end (ET)", "str", False, "Rule 2 — also the time cutoff."),
    ("Schedule & trial", "DAYTRADE_BAR_INTERVAL_MINUTES", "Bar interval (min)", "int", False, "Bar ingestion cadence."),
    ("Schedule & trial", "DAYTRADE_SCREEN_ET", "Screener run time (ET)", "str", False, "Nightly/pre-market screen."),
    ("Schedule & trial", "DAYTRADE_TRIAL_TRADES", "Paper trial length (trades)", "int", False, "Trades before the trial verdict."),
    ("Schedule & trial", "DAYTRADE_DIGEST_ET", "Daily digest time (ET)", "str", False, "Performance digest send time."),
]

REPLAYABLE = {key: kind for _g, key, _l, kind, rep, _h in _CATALOG if rep}


def catalog() -> list[dict]:
    return [{"group": g, "key": key, "label": label, "kind": kind, "replayable": rep,
             "help": help_, "value": getattr(config, key)}
            for g, key, label, kind, rep, help_ in _CATALOG]


def coerce_overrides(raw: dict) -> dict:
    """Validate a {key: value} map from the UI: only replayable keys, numeric,
    non-negative. Unchanged-from-default values are fine (they're no-ops)."""
    out = {}
    for key, val in (raw or {}).items():
        kind = REPLAYABLE.get(key)
        if kind is None:
            raise ValueError(f"{key} is not a replayable parameter")
        try:
            num = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if num < 0:
            raise ValueError(f"{key} can't be negative")
        out[key] = int(num) if kind == "int" else num
    if out.get("DAYTRADE_STOP_ATR_DIVISOR", 1) <= 0:
        raise ValueError("DAYTRADE_STOP_ATR_DIVISOR must be greater than 0")
    return out
