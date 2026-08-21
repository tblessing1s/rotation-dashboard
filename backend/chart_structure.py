"""Level-4 CHART-STRUCTURE metrics — SHADOW ONLY (TRAVIS_EXTENSION).

Level 4 ("right spot") measures QUIETNESS but not STRUCTURE. All three of its
live checks are local reads — ATR% of price, ATR/ATR_5EMA, and extension above
MA21 in ATR units (``stock_lights._right_spot_from``) — so two structurally
opposite charts produce identical gate readings:

  * the GOOD one: a tight coil a few percent under its recent highs, after an
    advance, MA21 rising under it, higher lows, volume drying up;
  * the BAD one: a name that ran months ago, rolled over, and now drifts
    mid-range near a flattening MA21 with overhead supply above it.

This module measures the difference. It computes FOUR metrics plus a derived
``structure_score``, and it has **ZERO AUTHORITY**:

  * nothing here is ever appended to the ``blocks`` list that feeds
    ``scan_triggers.compose_row_verdict`` — that list is what gives a finding
    verdict authority, and keeping this out of it is the load-bearing invariant
    of the whole feature (the same invariant ``scan_triggers.shadow_floor``
    states for the income floors);
  * nothing here is a check in ``stock_lights._right_spot_from``, whose
    ``checks`` list flows ``blocked_by`` -> ``right_spot.pass`` -> Level 4's
    pass -> ``gate_blocks`` -> the canonical verdict;
  * nothing here enters the shadow SCORE, the ranking, the suitability lens, or
    ``scan_triggers._KIND`` (an id there is by definition a block).

There is deliberately NO config switch that would turn any of this into a block.
Graduating a metric to blocking authority is a future work item CONTINGENT on
logged real-data calibration against Travis's manual compelling / not-compelling
labels, and it must be a deliberate code change reviewed on its own — the same
discipline the weekly-juice floor is held to.

PURE: no I/O, no clock, no provider calls, never mutates the frame. Every metric
returns ``None`` on insufficient history and the aggregate names it explicitly in
``insufficient`` — never a silent 0, and never a pass it could not measure.

DATA NOTE (audit §4.2). Schwab price history is split-adjusted; Alpha Vantage's
``TIME_SERIES_DAILY`` is raw/as-traded. Neither is dividend-adjusted, and the two
disagree across a split. That is harmless for every pre-existing consumer (all
short-window or ratio-based) but NOT for a 126/252-bar trailing high, where an
unadjusted pre-split print would read as a catastrophic drawdown for a year. So
the trailing high is taken over ``Close`` (not ``High``) and guarded by
``SPLIT_GAP_DROP``: a window straddling a single-bar drop the size of a split
ratio is reported as unmeasurable rather than as a number.
"""
from __future__ import annotations

import pandas as pd

import indicators

# ---------------------------------------------------------------------------
# Windows and constructive bands. Every constant here is PROPOSED_DEFAULT — a
# configurable first guess pending calibration, NOT CFM canon. None of them is a
# HARD_CFM_RULE and none may be read by a blocking path.
# ---------------------------------------------------------------------------
HIGH_WINDOW = 126              # PROPOSED_DEFAULT — ~6 months, the primary trailing-high window
HIGH_WINDOW_LONG = 252         # PROPOSED_DEFAULT — ~12 months, DISPLAY ONLY (see audit §4.3)
DIST_FROM_HIGH_MAX = 8.0       # PROPOSED_DEFAULT — within this % of the 126-bar high = constructive

MA21_SLOPE_LOOKBACK = 10       # PROPOSED_DEFAULT — trailing bars the MA21 slope is measured over
MA21_SLOPE_FLAT = 0.05         # PROPOSED_DEFAULT — |slope| below this (ATR/bar) is FLAT, not rising

TIGHTNESS_RECENT = 15          # PROPOSED_DEFAULT — the coil window (daily closes)
TIGHTNESS_PRIOR = 60           # PROPOSED_DEFAULT — the prior-advance window it is measured against
# The two bases are NOT on one scale, so they carry SEPARATE thresholds — see
# ``tightness_max_for``. A single threshold is what makes the atr_sum reading
# uninformative: measured over a synthetic population spanning drift, amplitude,
# period and noise, 0.35 admits 100.0% of atr_sum-basis charts against 60.2% of
# advance-basis ones. A bar everything clears is not a bar.
TIGHTNESS_MAX_ADVANCE = 0.35   # PROPOSED_DEFAULT — coil range / prior ADVANCE range
# PROPOSED_DEFAULT — coil range / prior 60-bar ATR SUM. Two independent estimates
# put it at the same place:
#   * random-walk scale. Summed true range is PATH LENGTH; over n bars a random
#     walk spans a range of order path/sqrt(n). So the scale-equivalent bar is
#     TIGHTNESS_MAX_ADVANCE / sqrt(60) = 0.045. Measured range/atr_sum on
#     non-advancing windows: median 0.131 vs the theoretical 1/sqrt(60) = 0.129.
#   * pass-rate matching. The threshold admitting the same FRACTION of atr_sum
#     charts that 0.35 admits of advance charts is 0.049 (60.3% vs 60.2%).
# 0.05 sits between them, is legible, and matches the pass rate to ~1 point.
TIGHTNESS_MAX_ATR_SUM = 0.05

HIGHER_LOWS_WINDOW = 30        # PROPOSED_DEFAULT — trailing bars scanned for swing lows
HIGHER_LOWS_MIN = 2            # PROPOSED_DEFAULT — this many successive higher lows = constructive
PIVOT_SPAN = 1                 # PROPOSED_DEFAULT — bars each side of a pivot low (1 = a 3-bar pivot)

# PROPOSED_DEFAULT — split-contamination guard, see the module docstring.
#
# Detected by SIGNATURE, not by magnitude. A ratio test ("trailing max more than
# N x the last close") cannot work: an unadjusted 2:1 split and a genuine 50%
# drawdown produce the identical 2.0x ratio, so any threshold either misses real
# splits or discards real drawdowns. What actually distinguishes them is that a
# split lands in ONE bar with no trading in between, while a drawdown takes many.
# So the window is scanned for a single-bar close drop larger than this, which is
# what every common split ratio looks like (3:2 = -33%, 2:1 = -50%, 3:1 = -67%,
# 4:1 = -75%, 10:1 = -90%).
#
# A genuine one-day collapse of this size also trips it. That is deliberate and
# costs nothing: a name that just dropped a third in a session is not a
# constructive coil under any reading, and a trailing high measured across such a
# bar is not a number worth reporting. Only a split INSIDE the window matters —
# one before it leaves the whole window on one price basis, which reads fine.
SPLIT_GAP_DROP = 0.30

# The metric ids, in report order. Also the keys of ``constructive``.
METRICS = ("dist_from_high_pct", "ma21_slope", "tightness", "higher_lows")

INSUFFICIENT = "insufficient_data"


def _closes(df: pd.DataFrame | None) -> pd.Series | None:
    if df is None or df.empty or "Close" not in df:
        return None
    c = df["Close"].astype(float).dropna()
    return c if len(c) else None


# ---------------------------------------------------------------------------
# 1. Distance below the trailing high
# ---------------------------------------------------------------------------
def dist_from_high_pct(df: pd.DataFrame | None, window: int = HIGH_WINDOW) -> float | None:
    """Percent BELOW the trailing ``window``-bar closing high (0.0 = at the high,
    12.5 = twelve and a half percent under it). Never negative — the last close is
    part of the window, so it can never exceed its own trailing max.

    Taken over ``Close`` and split-guarded (see ``SPLIT_GAP_DROP``): a window that
    straddles a single-bar drop the size of a split ratio is reported as
    unmeasurable, because on an unadjusted frame the "high" on the far side of
    that bar is a different price basis. The honest answer there is None, not a
    fabricated 50% drawdown. Constructive when <= ``DIST_FROM_HIGH_MAX``."""
    c = _closes(df)
    if c is None or len(c) < window:
        return None
    win = c.iloc[-window:]
    hi, px = float(win.max()), float(win.iloc[-1])
    if hi <= 0 or px <= 0:
        return None
    if has_split_gap(win):
        return None
    return round((hi - px) / hi * 100, 2)


def has_split_gap(closes: pd.Series) -> bool:
    """Does this close series contain a single-bar drop the size of a split ratio?
    See ``SPLIT_GAP_DROP`` for why the signature, not the magnitude, is the test."""
    if closes is None or len(closes) < 2:
        return False
    ratio = closes.astype(float).pct_change().dropna()
    return bool((ratio <= -SPLIT_GAP_DROP).any())


# ---------------------------------------------------------------------------
# 2. MA21 slope, ATR-normalized
# ---------------------------------------------------------------------------
def ma21_slope(df: pd.DataFrame | None, lookback: int = MA21_SLOPE_LOOKBACK,
               ma_window: int = 21) -> float | None:
    """Slope of the MA21 over the trailing ``lookback`` bars, in **ATR per bar** —
    so a $400 name and a $40 name are directly comparable.

        (MA21_today - MA21_lookback_bars_ago) / lookback / ATR

    Positive = rising. Constructive when > 0; ``|slope| < MA21_SLOPE_FLAT`` is the
    FLAT band (the rolled-over-and-drifting shape this metric exists to catch),
    reported by ``ma21_slope_state``. ATR is the same 9-day Wilder figure the
    right-spot gate uses (``indicators._atr_series``), so the two can't disagree."""
    c = _closes(df)
    if c is None or len(c) < ma_window + lookback:
        return None
    ma = c.rolling(ma_window).mean().dropna()
    if len(ma) < lookback + 1:
        return None
    atr = indicators.atr(df)
    if not atr:                       # None or 0 — no scale to normalize against
        return None
    return round(float(ma.iloc[-1] - ma.iloc[-1 - lookback]) / lookback / atr, 4)


def ma21_slope_state(slope: float | None) -> str | None:
    """``rising`` / ``flat`` / ``falling`` for one slope value (None passes through).
    FLAT is the band that a bare sign test would misread as constructive."""
    if slope is None:
        return None
    if abs(slope) < MA21_SLOPE_FLAT:
        return "flat"
    return "rising" if slope > 0 else "falling"


# ---------------------------------------------------------------------------
# 3. Tightness of the coil
# ---------------------------------------------------------------------------
def tightness_max_for(basis: str | None) -> float | None:
    """The constructive ceiling for one tightness ``basis``.

    Split deliberately. The denominators measure different things — a RANGE for
    an advancing prior window, summed true range (PATH LENGTH) when it did not
    advance — and path length is always >= the range it spans, so one number
    cannot bar both. Calibrated so each basis discriminates at a comparable rate
    rather than to the same nominal value; see the constants above."""
    if basis == "advance":
        return TIGHTNESS_MAX_ADVANCE
    if basis == "atr_sum":
        return TIGHTNESS_MAX_ATR_SUM
    return None


def tightness(df: pd.DataFrame | None, recent: int = TIGHTNESS_RECENT,
              prior: int = TIGHTNESS_PRIOR) -> tuple[float | None, str | None]:
    """``(ratio, basis)`` — the range of the last ``recent`` closes over the size
    of the ``prior`` bars that preceded them. Lower = tighter coil; constructive
    below the ceiling for its OWN basis (``tightness_max_for``).

    ``basis`` is ``"advance"`` when the prior window genuinely advanced (its last
    close above its first) and the spread of that advance is the denominator, or
    ``"atr_sum"`` when it did not and the summed true range over the same window
    stands in as the volatility scale.

    THE RATIO IS ONLY COMPARABLE WITHIN A BASIS. Summed true range is path
    length, always >= the range it spans, so an ``atr_sum`` denominator is
    systematically larger and its ratios systematically smaller. That is why the
    thresholds are split rather than shared: under one shared 0.35 the atr_sum
    reading admitted 100% of its population and carried no information at all.
    Never compare two tightness values across bases, and never pool them in a
    calibration — which is exactly why ``basis`` is returned, carried on the row,
    and persisted per scan alongside the value."""
    c = _closes(df)
    if c is None or len(c) < recent + prior:
        return None, None
    coil = c.iloc[-recent:]
    before = c.iloc[-(recent + prior):-recent]
    coil_range = float(coil.max() - coil.min())

    advanced = float(before.iloc[-1]) > float(before.iloc[0])
    denom = float(before.max() - before.min())
    basis = "advance"
    if not advanced or denom <= 0:
        atr_series = indicators._atr_series(df).dropna()
        if len(atr_series) < prior:
            return None, None
        denom = float(atr_series.iloc[-(recent + prior):-recent].sum())
        basis = "atr_sum"
    if denom <= 0:
        return None, None
    return round(coil_range / denom, 4), basis


# ---------------------------------------------------------------------------
# 4. Successive higher swing lows
# ---------------------------------------------------------------------------
def higher_lows(df: pd.DataFrame | None, window: int = HIGHER_LOWS_WINDOW,
                span: int = PIVOT_SPAN) -> int | None:
    """Count of SUCCESSIVE higher swing lows over the trailing ``window`` bars.

    A pivot low is a bar whose Low is strictly below the ``span`` bars on each
    side (``span=1`` is the simple 3-bar pivot). The count is the length of the
    trailing run of rising pivots: with pivots [10, 12, 11, 13, 14] the answer is
    2 (13 -> 14 is one rise, 11 -> 13 another; the 12 -> 11 break ends the run).
    Zero pivots is a genuine 0, not missing data — the window was measurable and
    contained no swing low. Constructive when >= ``HIGHER_LOWS_MIN``."""
    if df is None or df.empty or "Low" not in df:
        return None
    low = df["Low"].astype(float).dropna()
    if len(low) < window:
        return None
    win = low.iloc[-window:].to_numpy()
    pivots = [float(win[i]) for i in range(span, len(win) - span)
              if all(win[i] < win[i - k] and win[i] < win[i + k]
                     for k in range(1, span + 1))]
    run = 0
    for prev, cur in zip(pivots, pivots[1:]):
        run = run + 1 if cur > prev else 0
    return run


# ---------------------------------------------------------------------------
# The aggregate — the four metrics, their constructive verdicts, the score
# ---------------------------------------------------------------------------
def structure_metrics(df: pd.DataFrame | None) -> dict:
    """Every Level-4 structure metric for one symbol's bars, plus the derived
    ``structure_score``. SHADOW: the caller attaches this to the row as purely
    ADDITIVE keys and never lets it reach ``blocks``.

    ``structure_score`` counts the metrics IN their constructive band;
    ``structure_score_of`` is how many were measurable, and ``insufficient`` names
    the rest. A partial read is therefore reported as "2 of 3, dist_from_high_pct
    unmeasurable" rather than collapsing to a bare 2/4 that reads like a failure —
    the same "unmeasured is not failed" rule ``shadow_floor`` applies with its
    ``pass: None``."""
    dist = dist_from_high_pct(df, HIGH_WINDOW)
    dist_long = dist_from_high_pct(df, HIGH_WINDOW_LONG)   # display only
    slope = ma21_slope(df)
    tight, tight_basis = tightness(df)
    hl = higher_lows(df)

    values = {"dist_from_high_pct": dist, "ma21_slope": slope,
              "tightness": tight, "higher_lows": hl}
    constructive = {
        "dist_from_high_pct": None if dist is None else bool(dist <= DIST_FROM_HIGH_MAX),
        # Rising, not merely non-negative: the FLAT band is the drifting chart.
        "ma21_slope": None if slope is None else bool(slope > 0 and abs(slope) >= MA21_SLOPE_FLAT),
        # Judged against its OWN basis's ceiling — the two are not interchangeable.
        "tightness": None if tight is None else bool(tight < tightness_max_for(tight_basis)),
        "higher_lows": None if hl is None else bool(hl >= HIGHER_LOWS_MIN),
    }
    insufficient = [k for k in METRICS if values[k] is None]
    measurable = [k for k in METRICS if values[k] is not None]
    return {
        **values,
        "dist_from_high_252_pct": dist_long,
        "ma21_slope_state": ma21_slope_state(slope),
        "tightness_basis": tight_basis,
        # The ceiling this reading was actually judged against, carried so the
        # display and the calibration log never have to re-derive which basis
        # applied — and so a ratio is never read against the wrong bar.
        "tightness_max": tightness_max_for(tight_basis),
        "constructive": constructive,
        "structure_score": sum(1 for k in measurable if constructive[k]),
        "structure_score_of": len(measurable),
        "insufficient": insufficient,
        # An explicit marker so a reader never mistakes a partial read for a full
        # one, and never mistakes any of this for a gate result.
        "status": INSUFFICIENT if insufficient else "ok",
        "shadow": True,
    }


# ---------------------------------------------------------------------------
# Phase awareness for the volume check (TRAVIS_EXTENSION)
# ---------------------------------------------------------------------------
def consolidation_phase(right_spot: dict | None) -> bool:
    """Is this name IN a consolidation right now? Derived from the ALREADY-COMPUTED
    Level-4 check results — a pure read of ``right_spot["checks"]``, never a
    re-derivation and never a new threshold.

    The two live checks that together mean "quiet and not stretched from the mean"
    are ``atr_5d_ema`` (ATR contracting or flat vs its 5-EMA) and ``extension``
    (close within SPOT_ATR_EXTENSION_MAX ATRs of MA21). Both must pass.

    NOTE (audit §1.4 / §8.2): the natural-language spec for this flag says "ATR
    contracting AND price within the MA21 proximity band". There is no live MA21
    *percent* proximity check to reuse — ``config.CONSOLIDATION_MA21_DIST_MAX`` is
    read only by ``indicators.consolidating()``, which has ZERO call sites and was
    superseded by the three-check right spot. Reusing it would resurrect a dead
    constant and introduce a fourth Level-4 input. ``extension`` is the live
    check that means the same thing in ATR units, so that is what is read here.

    Fails CLOSED: no gate, no right spot, or a missing/unmeasured check all give
    False, which preserves today's volume behavior exactly."""
    checks = (right_spot or {}).get("checks") or []
    by_id = {c.get("id"): c for c in checks}
    needed = ("atr_5d_ema", "extension")
    if not all(cid in by_id for cid in needed):
        return False
    return all(bool(by_id[cid].get("pass")) for cid in needed)
