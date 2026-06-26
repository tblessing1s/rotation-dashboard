"""
Configuration & calibration knobs for the rotation dashboard.

Edit these to match your thinkorswim studies, then restart the backend.
"""

import os

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
TRACKED = SECTOR_SYMBOLS + ["AAPL"]  # AAPL is the default APP stock candidate.

# Volatility proxy used anywhere the dashboard labels the regime input as VIX.
# Use a traded ETF so Schwab/Yahoo can pull it like every other quote symbol.
# Override with VIX_PROXY_SYMBOL if you prefer another VIX ETF/ETN such as VXX.
VIX_PROXY_SYMBOL = (
    (os.environ.get("VIX_PROXY_SYMBOL") or "VIXY").strip().upper() or "VIXY"
)

QUOTE_SYMBOLS = SECTOR_SYMBOLS + ["AAPL", VIX_PROXY_SYMBOL, "SPY"]  # for the live ticker strip/API

# ---- 5 key indicator settings ----------------------------------------------
# Schwab daily bars line up with thinkorswim's daily studies, so defaults use
# the same common study settings:
# - RS3M: (close / close("SPY")) relative-strength ratio vs the same ratio
#   63 trading bars ago.
# - RS3M_MOM: percent change from current RS3M to RS3M[5]. With defaults,
#   current=(rs/rs[63]-1)*100 and prior=(rs[5]/rs[68]-1)*100.
# - RSI: 14-period Wilder average (thinkorswim RSI default).
# - MA21: 21-day simple moving average (thinkorswim SimpleMovingAvg).
# VolumeRatio uses latest volume / latest 20-day average volume * 100.
# VolumeAccel uses latest volume / latest 5-day average volume * 100.
RS3M_METHOD = "ratio"
RS3M_EMA_SPAN = 1
RS3M_LOOKBACK = 63
RS3M_MOM_WINDOW = 10  # legacy/window metadata; exact TOS momentum uses the two lag settings below.
RS3M_MOM_PAST_END_LAG = 68
RS3M_MOM_PAST_LOOKBACK = 131  # retained for API/config compatibility; TOS MOM uses the 5-bar lag above.
MOM_SMOOTH = 1
MOM_SCALE = 1.0
RSI_METHOD = "wilder"
MA21_METHOD = "sma"

# ---- Data / ingestion --------------------------------------------------------
HISTORY_DAYS = 320          # ~10 months of daily bars (enough for RS3M_MOM's 131-bar reference)

# Entry-watch candidate universe (CFM strategy only).
CFM_ENTRY_CANDIDATES = [
    "XLV", "XLP", "XLU", "XLRE",
    "LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE",
    "PG", "COST", "WMT", "PEP", "KO",
    "NEE", "SO", "DUK", "PLD", "AMT",
]
ENTRY_CANDIDATES = CFM_ENTRY_CANDIDATES
ENTRY_CANDIDATE_PROXY = {
    "XLV": "XLV", "XLP": "XLP", "XLU": "XLU", "XLRE": "XLRE",
    "LLY": "XLV", "UNH": "XLV", "JNJ": "XLV", "MRK": "XLV", "ABBV": "XLV", "PFE": "XLV",
    "PG": "XLP", "COST": "XLP", "WMT": "XLP", "PEP": "XLP", "KO": "XLP",
    "NEE": "XLU", "SO": "XLU", "DUK": "XLU", "PLD": "XLRE", "AMT": "XLRE",
    "XLK": "XLK", "XLY": "XLY", "XLC": "XLC", "XLI": "XLI",
    "NVDA": "XLK", "MSFT": "XLK", "AAPL": "XLK", "AVGO": "XLK", "AMD": "XLK", "CRM": "XLK", "NOW": "XLK",
    "META": "XLC", "GOOGL": "XLC", "NFLX": "XLC", "AMZN": "XLY", "TSLA": "XLY",
    "HD": "XLY", "CAT": "XLI", "GE": "XLI", "HON": "XLI", "DE": "XLI",
}

# If the newest successful ingest is older than this, an API hit kicks off a
# background catch-up run (the request itself is never blocked).
INGEST_STALE_AFTER_HOURS = 6

# ---- Validation / quarantine -------------------------------------------------
# A bar is quarantined when its close moves more than this fraction vs the
# prior close (plus null/negative/high<low checks). Per-symbol overrides for
# things that legitimately gap hard.
VALIDATION_MAX_MOVE = 0.25
VALIDATION_MAX_MOVE_PER_SYMBOL = {
    VIX_PROXY_SYMBOL: 1.00,  # VIX futures ETFs can gap hard during volatility shocks.
    "^VIX": 1.00,           # keep legacy/index support for older stored data or ad-hoc pulls.
}

# Level 1 regime inputs cross-checked across two providers when both are
# available. Divergence beyond tolerance is flagged in the data-issues panel
# instead of silently trusting one source.
CROSS_CHECK_SYMBOLS = [VIX_PROXY_SYMBOL, "SPY"]
CROSS_CHECK_TOLERANCE = 0.01            # 1% on close
CROSS_CHECK_TOLERANCE_PER_SYMBOL = {
    VIX_PROXY_SYMBOL: 0.03,  # volatility feeds can snapshot at slightly different times
    "^VIX": 0.03,           # legacy/index support
}

# ---- Macro automation -------------------------------------------------------
# Level 1 inputs are derived from public, no-key sources where possible.
# Breadth is approximated as the percent of this broad ETF universe trading above
# its 50-day moving average. Adjust the universe if you prefer a different lens.
# (^NYA was previously listed as "NYA", which Yahoo doesn't recognize, so the
# NYSE Composite was silently missing from breadth.)
BREADTH_SYMBOLS = [
    "SPY", "QQQ", "IWM", "^NYA",
    "XLK", "XLV", "XLF", "XLY", "XLC", "XLI", "XLP", "XLE", "XLU", "XLB", "XLRE",
]
BREADTH_MA_WINDOW = 50

# FRED series ingested daily. Fetched via the official FRED API when the
# FRED_API_KEY env var is set (free key from https://fred.stlouisfed.org/docs/api/api_key.html),
# falling back to the keyless graph CSV. The keyless endpoint has started
# returning HTTP 403 to programmatic requests, so a key is strongly recommended.
FRED_SERIES = ["DFF", "CPIAUCSL", "GDPC1", "UNRATE"]

# ---- Daily screener (Alpha Vantage) -----------------------------------------
# Alpha Vantage has no server-side market screener, so the screener scans a
# *universe* and applies the price/volume/ATR% filters locally. The ≥10M-volume
# floor already collapses the entire US market to a few hundred names, so a
# curated high-liquidity list — plus the day's TOP_GAINERS_LOSERS most-actives
# as a discovery layer — covers effectively everything in range. Tune this list
# freely; symbols that fall out of range are simply filtered out each run.
SCREENER_UNIVERSE = [
    # Broad / sector / thematic ETFs that routinely trade heavy volume.
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLY", "XLI", "XLV",
    "XLP", "XLU", "XLB", "XLC", "XLRE", "SMH", "SOXL", "SOXS", "TQQQ", "SQQQ",
    "ARKK", "XBI", "KRE", "GDX", "SLV", "USO", "TLT", "HYG", "FXI", "EEM",
    # Mega-cap / high-liquidity single names.
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO",
    "AMD", "INTC", "MU", "QCOM", "TXN", "CRM", "ORCL", "ADBE", "NOW", "PLTR",
    "NFLX", "DIS", "CMCSA", "T", "VZ", "PYPL", "SQ", "SHOP", "UBER", "ABNB",
    "COIN", "MSTR", "SNOW", "CRWD", "PANW", "DDOG", "NET", "SMCI", "ARM", "DELL",
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "V", "MA", "AXP",
    "BX", "KKR", "COF", "USB", "PNC",
    "XOM", "CVX", "COP", "SLB", "OXY", "MRO", "DVN", "HAL", "FANG", "MPC",
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "BMY", "GILD", "AMGN", "CVS",
    "MRNA", "TMO", "DHR", "ISRG", "VRTX",
    "WMT", "COST", "HD", "LOW", "TGT", "NKE", "SBUX", "MCD", "PG", "KO",
    "PEP", "CAT", "DE", "BA", "GE", "HON", "UPS", "FDX", "LMT", "RTX",
    "F", "GM", "RIVN", "LCID", "NIO", "DAL", "AAL", "UAL", "CCL", "NCLH",
    "BABA", "PDD", "JD", "MARA", "RIOT", "CLSK", "AFRM", "SOFI", "HOOD", "DKNG",
    "ROKU", "ZM", "DOCU", "TTD", "ENPH", "FSLR", "RUN", "PLUG", "CHPT", "NEE",
]


# ---- Portfolio defaults (mirrors your framework) ----------------------------
CAPITAL = 35000
RESERVE = 13000
