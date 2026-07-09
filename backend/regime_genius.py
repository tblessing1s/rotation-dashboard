"""The Genius four-light market regime (CFM course canon) — a PURE module.

The Cash Flow Machine "Genius System" reads four binary indicator "lights" off
the market index (SPY daily bars) and votes them to a green/yellow/red condition,
then holds a YELLOW condition for a minimum dwell so it can't flap:

    1. close vs slow MA        — close above the slow average  = GREEN light
    2. fast MA vs slow MA      — fast above slow (trend up)     = GREEN light
    3. Parabolic SAR vs close  — SAR dots under price           = GREEN light
    4. momentum vs zero        — ROC above zero                 = GREEN light

    vote:  >=3 GREEN -> GREEN ; 2 GREEN / 2 RED -> YELLOW ; >=3 RED -> RED   [HARD]

Dwell (HARD_CFM_RULE): once the *published* regime becomes YELLOW it stays YELLOW
for a minimum of ``GENIUS_YELLOW_DWELL_DAYS`` consecutive TRADING days (the entry
day counts as day 1), regardless of the raw vote — the course's anti-flap rule.

Secondary indicators: breadth and VIX are SECONDARY, informational confirmation
signals only. They are reported alongside the regime (whether they confirm or
diverge from a green tape) for the operator's own read, but they do NOT change
the traffic light — the four lights and the dwell decide it on their own.

This module does NO I/O and reads NO clock: bars, the prior published series, the
breadth/VIX scalars, and any timestamp are all passed in. Every indicator
parameter is resolved from provenance-tagged config (``GENIUS_*``) but overridable
per call, so ``calibration.regime_series`` can replay history under alternative
parameter sets. Persistence lives in ``regime_history.py``; the live recompose
+ legacy fields live in ``screening.regime()``.
"""
from __future__ import annotations

import config
import indicators

GREEN = "green"
YELLOW = "yellow"
RED = "red"


# ---------------------------------------------------------------------------
# Parameters (config defaults, per-call overridable for calibration)
# ---------------------------------------------------------------------------
def default_params() -> dict:
    """The provenance-tagged Genius parameter set from config. A calibration
    sweep passes a modified copy of this into ``compute_trace`` — nothing here
    reads config directly past this point, so an alternative parameter set fully
    determines the output."""
    return {
        "slow_ma": config.GENIUS_SLOW_MA,
        "fast_ma": config.GENIUS_FAST_MA,
        "sar_af_step": config.GENIUS_SAR_AF_STEP,
        "sar_af_max": config.GENIUS_SAR_AF_MAX,
        "roc_window": config.GENIUS_MOMENTUM_ROC,
        "vote_green_min": config.GENIUS_VOTE_GREEN_MIN,
        "dwell_days": config.GENIUS_YELLOW_DWELL_DAYS,
        "breadth_confirm_min": config.BREADTH_CONFIRM_MIN_PCT,
        "vix_elevated": config.VIX_ELEVATED_THRESHOLD,
    }


def _params(overrides: dict | None) -> dict:
    p = default_params()
    if overrides:
        p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# The four lights — each returns its signal (GREEN/RED, or None on missing data)
# plus the underlying values, for UI + snapshot provenance.
# ---------------------------------------------------------------------------
def _signal(is_green: bool | None) -> str | None:
    if is_green is None:
        return None
    return GREEN if is_green else RED


def light_close_vs_ma(close: float | None, slow_ma: float | None) -> dict:
    """Light 1 — close above the slow MA is a GREEN light."""
    green = None if close is None or slow_ma is None else close > slow_ma
    return {"signal": _signal(green), "close": close, "slow_ma": slow_ma}


def light_fast_vs_slow(fast_ma: float | None, slow_ma: float | None) -> dict:
    """Light 2 — fast MA above slow MA (primary trend up) is a GREEN light."""
    green = None if fast_ma is None or slow_ma is None else fast_ma > slow_ma
    return {"signal": _signal(green), "fast_ma": fast_ma, "slow_ma": slow_ma}


def light_sar(close: float | None, sar: float | None) -> dict:
    """Light 3 — Parabolic SAR under price (dots below the bar) is a GREEN light."""
    green = None if close is None or sar is None else sar < close
    return {"signal": _signal(green), "sar": sar, "close": close}


def light_momentum(roc: float | None) -> dict:
    """Light 4 — momentum (ROC) above zero is a GREEN light."""
    green = None if roc is None else roc > 0
    return {"signal": _signal(green), "roc": roc}


def compute_lights(df, params: dict | None = None) -> dict:
    """The four lights for the latest bar of `df` (an ascending OHLCV frame).
    Every indicator is computed here from `df` alone — no clock, no I/O."""
    p = _params(params)
    close = indicators.last(df)
    slow = indicators.sma(df, p["slow_ma"]) if df is not None else None
    fast = indicators.ema(df, p["fast_ma"])
    sar = indicators.parabolic_sar_last(df, p["sar_af_step"], p["sar_af_max"])
    roc = indicators.roc(df, p["roc_window"])
    return {
        "close_vs_ma": light_close_vs_ma(close, slow),
        "fast_vs_slow": light_fast_vs_slow(fast, slow),
        "sar": light_sar(close, sar),
        "momentum": light_momentum(roc),
    }


# ---------------------------------------------------------------------------
# Vote (HARD_CFM_RULE)
# ---------------------------------------------------------------------------
def vote(lights: dict, params: dict | None = None) -> dict:
    """Vote the four lights to a raw condition. >=vote_green_min GREEN -> GREEN;
    a 2/2 split -> YELLOW; >=vote_green_min RED -> RED. If any light is missing
    (insufficient history) the vote can't be trusted, so it degrades to YELLOW
    and flags ``insufficient`` — real SPY always has all four."""
    p = _params(params)
    signals = [lights[k]["signal"] for k in ("close_vs_ma", "fast_vs_slow", "sar", "momentum")]
    green = sum(1 for s in signals if s == GREEN)
    red = sum(1 for s in signals if s == RED)
    insufficient = any(s is None for s in signals)
    green_min = p["vote_green_min"]
    # Canon: >=green_min GREEN -> GREEN; >=green_min RED -> RED; else YELLOW.
    if insufficient:
        condition = YELLOW
    elif green >= green_min:
        condition = GREEN
    elif red >= green_min:
        condition = RED
    else:
        condition = YELLOW
    return {"raw_condition": condition, "green_count": green, "red_count": red,
            "insufficient": insufficient}


# ---------------------------------------------------------------------------
# Yellow dwell (HARD_CFM_RULE) — anti-flap hysteresis on TRADING days
# ---------------------------------------------------------------------------
def _trailing_yellow_streak(prior_published: list[str]) -> int:
    """Consecutive published-YELLOW trading days counting back from the most
    recent prior day (the length of the current yellow episode as of yesterday)."""
    n = 0
    for pub in reversed(prior_published or []):
        if pub == YELLOW:
            n += 1
        else:
            break
    return n


def apply_dwell(raw_today: str, prior_published: list[str],
                params: dict | None = None) -> dict:
    """Hold a YELLOW published regime for a minimum of ``dwell_days`` consecutive
    trading days, measured from the first day the episode turned yellow (the entry
    day is day 1). Release to the raw vote on the first day where raw != YELLOW AND
    at least ``dwell_days`` yellow days have already published.

    Defined edge behaviour (all tested):
      * enter yellow -> raw flips green next day -> published stays yellow through
        day ``dwell_days``; releases on day ``dwell_days`` + 1.
      * a re-yellow (raw yellow again) inside the window does NOT reset the clock:
        the minimum is measured from the original entry, so consecutive
        published-yellow days is the only counter.
      * a raw GREEN or RED that arrives during the window is HELD to yellow — the
        course rule is "a yellow condition cannot change for at least N days
        regardless of the raw vote."
      * cold start (no prior history) publishes the raw vote with no dwell debt;
        a raw-yellow cold start simply starts the clock at day 1.

    ``prior_published`` is the chronological list of prior *published* regimes
    (oldest -> newest); its last element is yesterday's published regime.
    """
    p = _params(params)
    dwell_min = p["dwell_days"]
    prior = list(prior_published or [])
    streak = _trailing_yellow_streak(prior)
    yesterday = prior[-1] if prior else None

    if raw_today == YELLOW:
        published = YELLOW
        dwell_day = streak + 1
        held = False
    elif yesterday == YELLOW and streak < dwell_min:
        # Within the minimum window -> hold yellow regardless of the raw vote.
        published = YELLOW
        dwell_day = streak + 1
        held = True
    else:
        # Not currently yellow, or the minimum is already satisfied -> follow raw.
        published = raw_today
        dwell_day = 0
        held = False

    return {
        "regime": published,
        "raw_condition": raw_today,
        "held_by_dwell": held,               # True only when raw wanted out but dwell held yellow
        "dwell_day": dwell_day,              # which published-yellow day this is (0 when not yellow)
        "dwell_min": dwell_min,
        "dwell_active": published == YELLOW and dwell_day < dwell_min,  # still inside the minimum
        "cold_start": not prior,
    }


# ---------------------------------------------------------------------------
# Secondary indicators (breadth + VIX) — INFORMATIONAL ONLY, never change the light
# ---------------------------------------------------------------------------
def secondary_indicators(breadth: float | None, vix: float | None,
                         params: dict | None = None) -> dict:
    """Breadth + VIX as SECONDARY confirmation indicators. These do NOT determine
    the regime traffic light (only the four lights + the yellow dwell do) — they
    are reported alongside it for the operator's own read. Each carries its value,
    its reference level, and whether it is diverging from / confirming a green
    tape. Purely informational: nothing here changes the published regime."""
    p = _params(params)
    breadth_diverging = bool(breadth is not None and breadth < p["breadth_confirm_min"])
    vix_elevated = bool(vix is not None and vix > p["vix_elevated"])
    return {
        "note": "informational — breadth/VIX do not change the regime light",
        "breadth": {
            "value": breadth,
            "confirm_min": p["breadth_confirm_min"],
            "diverging": breadth_diverging,
        },
        "vix": {
            "value": vix,
            "elevated_above": p["vix_elevated"],
            "elevated": vix_elevated,
        },
    }


# ---------------------------------------------------------------------------
# Full decision trace — the single entry point
# ---------------------------------------------------------------------------
def compute_trace(df, breadth: float | None, vix: float | None,
                  prior_published: list[str], params: dict | None = None) -> dict:
    """The complete, pure regime decision for one day: lights -> raw vote ->
    dwell -> published regime, with every intermediate recorded so calibration and
    the entry-context snapshot capture full provenance. Breadth + VIX ride along
    as SECONDARY informational indicators — they never change the published regime.

    ``prior_published`` — chronological prior *published* regimes (oldest first).
    Returns a dict whose ``published_regime`` is the app-facing regime (four lights
    + dwell) and whose ``status`` mirrors it for backward compatibility.
    """
    p = _params(params)
    lights = compute_lights(df, p)
    v = vote(lights, p)
    dwell = apply_dwell(v["raw_condition"], prior_published, p)
    secondary = secondary_indicators(breadth, vix, p)
    published = dwell["regime"]                 # the light: four lights + dwell only
    return {
        "status": published,                    # backward-compat: the app-facing regime
        "published_regime": published,          # four lights + yellow dwell (the light)
        "dwell_regime": dwell["regime"],        # == published (kept for compatibility)
        "raw_condition": v["raw_condition"],    # today's raw vote
        "lights": lights,
        "vote": v,
        "dwell": dwell,
        "secondary": secondary,                 # breadth + VIX (informational only)
        "breadth": breadth,
        "vix": vix,
    }
