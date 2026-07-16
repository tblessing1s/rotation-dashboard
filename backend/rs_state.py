"""Two-speed relative-strength state — SHADOW ONLY.

Collapses an RS pairing (a name vs a benchmark — vs SPY or vs its sector ETF) into
one of four states from two speeds:

    * LEVEL — the 3-month relative strength (``indicators.rs3m``): is the name
      OUT-performing (>= 0) or UNDER-performing (< 0) the benchmark. The slow read.
    * SLOPE — the 21-day slope of the RS-line EMA (``indicators.rs_ema_slope``): is
      that relative performance improving (up) or deteriorating (down). The fast read.

    | State     | Level | Slope | Meaning                                   |
    |-----------|-------|-------|-------------------------------------------|
    | RISING    | >= 0  | up    | leading and still improving               |
    | FADING    | >= 0  | down  | leading but rolling over (distribution-   |
    |           |       |       | into-strength — the XLK Jul 6 shape)      |
    | TURNING   | < 0   | up    | lagging but recovering (a base forming)   |
    | FALLING   | < 0   | down  | lagging and getting worse                 |

LEVEL deliberately reuses ``indicators.rs3m`` — the SAME figure the drawer already
shows as "RS3M Sec" / "RS3M SPY" — so the state's level axis can never disagree
with the displayed RS3M number. SLOPE is the new RS-line-EMA direction (the ToS
RS-momentum read), so chart and app agree on "is it turning".

**SHADOW ONLY.** This never feeds ``scan_verdict.compose_verdict``, never blocks,
never sizes, never touches the kill switch. It is displayed and logged. The ONE
gated exception (Phase 0 audit): a ``TURNING`` vs-Sector state may append a WATCH
*reason string* to an already-non-READY row's ``verdict_reasons`` — an
informational annotation, never a verdict change.

Pure: no I/O, no clock. All thresholds ``PROPOSED_DEFAULT``.
"""
from __future__ import annotations

import indicators

RISING = "RISING"
FADING = "FADING"
TURNING = "TURNING"
FALLING = "FALLING"

# The four states in "best -> worst" order, for a sortable column key.
ORDER = {RISING: 0, TURNING: 1, FADING: 2, FALLING: 3}

# PROPOSED_DEFAULT — the level/slope zero boundaries. A level of exactly 0 counts
# as "not underperforming" (>= 0 -> RISING/FADING); a flat slope counts as up
# (>= 0), so a dead-flat RS line reads RISING rather than FALLING.
LEVEL_ZERO = 0.0               # PROPOSED_DEFAULT
SLOPE_ZERO = 0.0               # PROPOSED_DEFAULT


def collapse(level: float | None, slope: float | None) -> str | None:
    """Pure four-state collapse from a LEVEL (rs3m %) and a SLOPE (rs_ema_slope %).
    Returns None when either input is missing (insufficient history) — the state
    is never guessed, mirroring the classifier's INSUFFICIENT_DATA discipline."""
    if level is None or slope is None:
        return None
    up = slope >= SLOPE_ZERO
    if level >= LEVEL_ZERO:
        return RISING if up else FADING
    return TURNING if up else FALLING


# The gated Phase-0 exception, as a pure helper so the scan sweep and the tests
# agree on it. It NEVER changes a verdict — it only annotates.
WATCH_ANNOTATION = "rs:TURNING (vs sector recovering — watch)"


def turning_watch_reason(verdict: str | None, rs_state_vs_sector: str | None) -> str | None:
    """The informational WATCH annotation for an already-non-READY row whose
    vs-Sector RS is ``TURNING`` (relative strength recovering), else None. Pure.

    This is the ONE gated exception from the Phase 0 audit: a ``TURNING`` state may
    add a WATCH *reason string* to ``verdict_reasons`` on a row that is already NOT
    READY — never for a READY row, never as a verdict change, never a second
    verdict. ``verdict`` is the canonical ``scan_verdict`` value ("READY" clears)."""
    if verdict != "READY" and rs_state_vs_sector == TURNING:
        return WATCH_ANNOTATION
    return None


def rs_state(df, bench) -> dict:
    """The RS state for one pairing plus its raw inputs (for the drawer + the
    calibration log). ``{"state": <str|None>, "level": <rs3m %>, "slope": <ema %>}``.
    Level reuses ``indicators.rs3m`` (matches the displayed RS3M); slope is the
    RS-line-EMA direction. PURE."""
    level = indicators.rs3m(df, bench) if (df is not None and bench is not None) else None
    slope = indicators.rs_ema_slope(df, bench) if (df is not None and bench is not None) else None
    return {"state": collapse(level, slope), "level": level, "slope": slope}
