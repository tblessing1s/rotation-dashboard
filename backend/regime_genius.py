"""The Genius four-light market regime (CFM course canon) — a PURE module.

The Cash Flow Machine "Genius System" reads four binary indicator "lights" off
the market index (SPY daily bars) and votes them to a green/yellow/red condition,
then holds a YELLOW condition for a minimum dwell so it can't flap:

    1. close vs slow MA        — close above the slow average  = GREEN light
    2. fast MA vs slow MA      — fast above slow (trend up)     = GREEN light
    3. Parabolic SAR vs close  — SAR dots under price           = GREEN light
    4. momentum vs zero        — ROC above zero                 = GREEN light

    vote:  >=3 GREEN -> GREEN ; 2 GREEN / 2 RED -> YELLOW ; >=3 RED -> RED   [HARD]

The four lights and the raw vote now live in the SHARED engine ``genius_lights``
(one indicator system, fractal across market and stock). This module owns ONLY
the market-specific layers on top — the yellow dwell and the secondary
indicators — and re-exports the shared light functions so existing callers /
tests keep their ``regime_genius.*`` names. There is zero duplicated light logic:
``compute_trace`` calls ``genius_lights.compute_lights`` + ``genius_lights.vote``.

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
import genius_lights

# Re-export the shared light engine's colors + light math under the historical
# ``regime_genius`` names (callers and tests import ``rg.light_sar`` etc.). These
# ARE the shared functions — no wrapping, so the market regime and the stock
# lights compute the four lights identically, byte for byte.
GREEN = genius_lights.GREEN
YELLOW = genius_lights.YELLOW
RED = genius_lights.RED

_signal = genius_lights._signal
light_close_vs_ma = genius_lights.light_close_vs_ma
light_fast_vs_slow = genius_lights.light_fast_vs_slow
light_sar = genius_lights.light_sar
light_momentum = genius_lights.light_momentum
compute_lights = genius_lights.compute_lights
vote = genius_lights.vote


# ---------------------------------------------------------------------------
# Parameters (config defaults, per-call overridable for calibration)
# ---------------------------------------------------------------------------
def default_params() -> dict:
    """The provenance-tagged Genius parameter set from config. A calibration
    sweep passes a modified copy of this into ``compute_trace`` — nothing here
    reads config directly past this point, so an alternative parameter set fully
    determines the output. This is a SUPERSET of ``genius_lights.default_params``:
    the shared light + vote keys plus the market-only dwell / breadth / VIX keys."""
    return {
        **genius_lights.default_params(),
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
