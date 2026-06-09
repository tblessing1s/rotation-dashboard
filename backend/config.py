"""
Configuration & calibration knobs for the rotation dashboard.

Edit these to match your thinkorswim studies, then restart the backend.
"""

# Symbols the dashboard tracks. SPY is the RS3M benchmark (always needed).
SECTOR_UNIVERSE = [
    {"symbol": "XLK", "name": "Technology", "group": "growth"},
    {"symbol": "XLY", "name": "Consumer Discretionary", "group": "growth"},
    {"symbol": "XLC", "name": "Communication Services", "group": "growth"},
    {"symbol": "XLI", "name": "Industrials", "group": "cyclical"},
    {"symbol": "XLF", "name": "Financials", "group": "cyclical"},
    {"symbol": "XLE", "name": "Energy", "group": "inflation"},
    {"symbol": "XLB", "name": "Materials", "group": "inflation"},
    {"symbol": "XLV", "name": "Health Care", "group": "defensive"},
    {"symbol": "XLP", "name": "Consumer Staples", "group": "defensive"},
    {"symbol": "XLU", "name": "Utilities", "group": "defensive"},
    {"symbol": "XLRE", "name": "Real Estate", "group": "rates"},
]
SECTOR_SYMBOLS = [s["symbol"] for s in SECTOR_UNIVERSE]
BENCHMARK = "SPY"
TRACKED = SECTOR_SYMBOLS + ["AAPL"]  # ILMN remains as the default APP stock candidate.
QUOTE_SYMBOLS = SECTOR_SYMBOLS + ["AAPL", "^VIX", "SPY"]  # for the live ticker strip/API

# ---- 5 key indicator settings ----------------------------------------------
# RS3M uses the supplied raw close formula:
#   (symbol % change over 90 rows) - (SPY % change over 90 rows)
# RS3M_MOM uses the supplied acceleration formula:
#   ((current RS3M - average RS3M over latest 10 readings) / abs(average)) * 100
# VolumeRatio uses latest volume / prior 20-day average volume * 100.
# VolumeAccel uses latest 5-day average volume / previous 5-day average * 100.
# RSI uses a simple 14-period average gain/loss calculation.
RS3M_METHOD = "return_spread"
RS3M_EMA_SPAN = 1
RS3M_LOOKBACK = 90
RS3M_MOM_WINDOW = 10
MOM_SMOOTH = 1
MOM_SCALE = 1.0

# ---- Data / cache -----------------------------------------------------------
HISTORY_DAYS = 320          # ~10 months of daily bars (enough for 90d RS3M + momentum)
CACHE_TTL_MINUTES = 15      # re-fetch history at most this often
CACHE_DIR = ".cache"


# ---- Macro automation -------------------------------------------------------
# Level 1 inputs are derived from public, no-key sources where possible.
# Breadth is approximated as the percent of this broad ETF universe trading above
# its 50-day moving average. Adjust the universe if you prefer a different lens.
BREADTH_SYMBOLS = [
    "SPY", "QQQ", "IWM", "NYA",
    "XLK", "XLV", "XLF", "XLY", "XLC", "XLI", "XLP", "XLE", "XLU", "XLB", "XLRE",
]
BREADTH_MA_WINDOW = 50
MACRO_CACHE_TTL_MINUTES = 60

# FRED graph CSV downloads do not require an API key.
FRED_DFF_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"
FRED_CPI_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
FRED_GDPC1_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GDPC1"

# ---- Portfolio defaults (mirrors your framework) ----------------------------
CAPITAL = 35000
RESERVE = 13000
