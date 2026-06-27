"""
CFM dashboard configuration & calibration.

Data sources are Schwab (primary) and Alpha Vantage (fallback) only — no FRED,
no Yahoo. Daily bars are cached to parquet under DATA_DIR/cache; persistent
state lives in DATA_DIR/state.json (mirrors the Fly volume mount at /data).
"""
from __future__ import annotations

import os

# ---- Paths -----------------------------------------------------------------
# On Fly a persistent volume is mounted at /data (DATA_DIR=/data in fly.toml).
# Locally it falls back to the backend directory so nothing needs configuring.
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BACKEND_DIR)
DATA_DIR = os.environ.get("DATA_DIR") or BACKEND_DIR
STATE_PATH = os.path.join(DATA_DIR, "state.json")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
# The sector universe ships with the repo (root-level), read-only reference data.
# A data/ fallback is kept for older checkouts.
TICKERS_BY_SECTOR_CANDIDATES = [
    os.path.join(REPO_DIR, "tickers_by_sector.txt"),
    os.path.join(REPO_DIR, "data", "tickers_by_sector.txt"),
]
TICKERS_BY_SECTOR_PATH = next(
    (p for p in TICKERS_BY_SECTOR_CANDIDATES if os.path.exists(p)),
    TICKERS_BY_SECTOR_CANDIDATES[0],
)

# Sector group classification (the file itself carries only ETF + name).
SECTOR_GROUPS = {
    "XLK": "growth", "XLY": "growth", "XLC": "growth",
    "XLI": "cyclical", "XLF": "cyclical",
    "XLE": "inflation", "XLB": "inflation",
    "XLV": "defensive", "XLP": "defensive", "XLU": "defensive",
    "XLRE": "rates",
}

# ---- Benchmark / regime ----------------------------------------------------
BENCHMARK = "SPY"
VIX_SYMBOL = (os.environ.get("VIX_SYMBOL") or "^VIX").strip().upper() or "^VIX"

# Breadth = percent of this broad universe trading above its 50-day MA.
BREADTH_SYMBOLS = [
    "SPY", "QQQ", "IWM",
    "XLK", "XLY", "XLC", "XLI", "XLF", "XLE", "XLB", "XLV", "XLP", "XLU", "XLRE",
]
BREADTH_MA_WINDOW = 50

# Regime gate thresholds (Level 1). VIX is the index level, not an ETF proxy.
REGIME_BREADTH_GREEN = 60      # % of universe above 50-DMA for a green tape
REGIME_BREADTH_RED = 40
VIX_CALM = 18                  # below = calm
VIX_ELEVATED = 24             # above = risk-off

# ---- Sector gate (Level 2) -------------------------------------------------
SECTOR_RS3M_MIN = 10.0         # sector RS3M vs SPY must clear +10%
SECTOR_BREADTH_MIN = 60.0     # % of sector constituents above 50-DMA

# ---- Stock gate (Levels 3 & 4) ---------------------------------------------
STOCK_RS_VS_SPY_MIN = 5.0      # stock RS3M vs SPY > +5%
STOCK_RS_VS_SECTOR_MIN = 0.0  # stock RS3M vs Sector > 0
CONSOLIDATION_ATR_PCT_MAX = 5.0   # daily ATR% of price below this = consolidating
CONSOLIDATION_MA21_DIST_MAX = 4.0  # within this % of MA21 = near the mean

# ---- Indicator calibration (matches thinkorswim daily studies) -------------
RS3M_LOOKBACK = 63            # ~3 months of trading days
ATR_WINDOW = 9               # CFM uses a 9-day ATR for strike spacing
RSI_WINDOW = 14
MA_WINDOW = 21
VOL_AVG_WINDOW = 20
HISTORY_DAYS = 320           # daily bars pulled / cached per symbol

# ---- CFM mechanics ---------------------------------------------------------
LEAP_CONTRACTS = 5            # 5 deep-ITM LEAP calls per stock
LEAP_TARGET_DELTA = 0.90
LEAP_DELTA_MIN = 0.88        # preferred LEAP delta band (offer strikes to choose)
LEAP_DELTA_MAX = 0.91
LEAP_TARGET_DTE = 180
RISK_FREE_RATE = 0.04        # for Black–Scholes greeks (delta recomputed to match TOS)
SHORT_ATR_MULT = 1.5         # short strike = stock - 1.5 * ATR
SHARE_CAP = 500              # accumulate to 500 shares per stock, then rotate
LEAP_ROLL_DTE = 30           # roll/replace LEAP when it nears this DTE

# ---- Capital ---------------------------------------------------------------
CAPITAL = 35000
RESERVE_REQUIRED = 13000

# Income milestones (monthly net juice) used by the position tracker.
MILESTONE_HALF_NUT = 2150
MILESTONE_QUIT_SAFE = 7500
