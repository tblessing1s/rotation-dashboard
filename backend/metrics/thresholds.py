"""Scorecard thresholds — every magic number the metric/verdict logic uses, in
one place so nothing is scattered through the math.

Provenance is labelled per constant:

  [HARD RULE]   — a stated CFM rule (Travis's CFM entry criteria / Golden Rules).
                  Changing it changes the strategy, not just calibration.
  [CALIBRATE]   — a proposed default, pending calibration against trade history.
                  Expect to tune these once paper-trading data accumulates.

This is a CFM (income / consolidation) lens, NOT an APP (breakout) lens. For CFM,
expanding ATR / extension is a NEGATIVE signal — the opposite of how an APP
breakout screen would read it.
"""
from __future__ import annotations

# ---- Extension -------------------------------------------------------------
# Primary "is it stretched" gate, measured in ATR units above MA21. Beyond this
# the stock is too extended to sell premium into a mean-reversion safely.
# [CALIBRATE] 3 ATRs is a sensible first guess, not a measured rule — this is the
# single threshold most in need of recalibration against closed CFM trades.
ATR_EXTENSION_MAX = 3.0

# ---- Volume ----------------------------------------------------------------
# Today's volume vs its 20-day average. Below this the tape is too thin to trust
# the consolidation read (no participation = no conviction).
# [CALIBRATE] proposed default.
VOLUME_RATIO_MIN = 0.8

# ---- Money Flow Index ------------------------------------------------------
# CFM wants a stock coiling in the middle of its money-flow range — not
# overbought (sellers about to step in) and not oversold (knife still falling).
# [HARD RULE] the 40–60 MFI band is from Travis's own CFM entry criteria.
MFI_MIN = 40.0
MFI_MAX = 60.0

# ---- ATR momentum ----------------------------------------------------------
# ATR / ATR_5EMA. >1 = volatility expanding. For CFM that is a CAUTION: an
# expanding-ATR name wants the APP (breakout) playbook, not the CFM income one.
# [HARD RULE] >1 is definitional (expanding vs contracting), not a tuned number.
ATR_MOMENTUM_MAX = 1.0

# ---- Relative strength -----------------------------------------------------
# RS3M vs Sector must be positive — a stock weaker than its own sector is an
# immediate AVOID. [HARD RULE] mirrors config.STOCK_RS_VS_SECTOR_MIN (the entry
# gate's Level-3 sector leg); kept here so the verdict reads from one place.
RS3M_VS_SECTOR_MIN = 0.0

# ---- Trend filters (boolean, no tunable threshold) -------------------------
# below_ma200 -> AVOID, below_ma50 -> CAUTION, ma50_slope < 0 -> CAUTION.
# [HARD RULE] structural trend rules; the only "threshold" is zero (the MA / the
# slope sign), so there is nothing to calibrate.
MA50_SLOPE_LOOKBACK = 5  # trading days back to measure the MA50 slope over
