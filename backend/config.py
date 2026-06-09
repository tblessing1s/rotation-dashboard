"""
Configuration & calibration knobs for the rotation dashboard.

Edit these to match your thinkorswim studies, then restart the backend.
"""

# Symbols the dashboard tracks. SPY is the RS3M benchmark (always needed).
TRACKED = ["XLV", "ILMN"]
BENCHMARK = "SPY"
QUOTE_SYMBOLS = ["XLV", "ILMN", "^VIX", "SPY"]  # for the live ticker strip

# ---- RS3M calibration -------------------------------------------------------
# RS3M_LOOKBACK: trading days in the relative-strength window. 63 ~ 3 months.
# MOM_SMOOTH:    EMA span applied to the RS3M series before momentum (1 = none).
#                Increase (e.g. 5-10) to smooth, matching an EMA-based TOS study.
# MOM_SCALE:     multiplier on the 5-day RS3M change. Tune so the magnitude lines
#                up with your thinkorswim RS3M_MOM (you reference +500/+884/+1128).
#                Start at 100 and adjust after comparing a few readings.
RS3M_LOOKBACK = 63
MOM_SMOOTH = 5
MOM_SCALE = 100.0

# ---- Data / cache -----------------------------------------------------------
HISTORY_DAYS = 320          # ~10 months of daily bars (enough for 63d RS3M + EMAs)
CACHE_TTL_MINUTES = 15      # re-fetch history at most this often
CACHE_DIR = ".cache"


# ---- Macro automation -------------------------------------------------------
# Level 1 inputs are derived from public, no-key sources where possible.
# Breadth is approximated as the percent of this broad ETF universe trading above
# its 50-day moving average. Adjust the universe if you prefer a different lens.
BREADTH_SYMBOLS = [
    "SPY", "QQQ", "IWM",
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
