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
# The ticker universe lives here as an editable JSON store on the volume, seeded
# once from the read-only repo file (TICKERS_BY_SECTOR_PATH) so it can be managed
# at runtime (add/remove/fix tickers) and survives deploys.
UNIVERSE_PATH = os.path.join(DATA_DIR, "universe.json")

# ---- Demo mode -------------------------------------------------------------
# A self-contained "fake data" view, kept entirely separate from the live store
# so toggling it on/off never touches real positions or the live cache. When
# demo mode is on the app reads the demo state + a synthetic price cache (seeded
# by seed_demo_data.py); when off it uses the live store + real providers. The
# flag is persisted to mode.json so it survives a restart within a DATA_DIR.
DEMO_STATE_PATH = os.path.join(DATA_DIR, "state.demo.json")
DEMO_CACHE_DIR = os.path.join(DATA_DIR, "cache_demo")
MODE_PATH = os.path.join(DATA_DIR, "mode.json")

_demo_mode: bool | None = None


def demo_enabled() -> bool:
    global _demo_mode
    if _demo_mode is None:
        try:
            import json
            with open(MODE_PATH, encoding="utf-8") as fh:
                _demo_mode = bool(json.load(fh).get("demo", False))
        except (OSError, ValueError):
            _demo_mode = False
    return _demo_mode


def set_demo_enabled(on: bool) -> None:
    global _demo_mode
    import json
    _demo_mode = bool(on)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = MODE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"demo": _demo_mode}, fh)
    os.replace(tmp, MODE_PATH)


def active_state_path() -> str:
    """state.json path for the current mode (demo store stays separate)."""
    return DEMO_STATE_PATH if demo_enabled() else STATE_PATH


def active_cache_dir() -> str:
    """Parquet cache dir for the current mode."""
    return DEMO_CACHE_DIR if demo_enabled() else CACHE_DIR
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
SHORT_ATR_MULT = 1.5         # short strike = stock - 1.5 * ATR (legacy flat default;
                              # superseded by STRIKE_TABLE below for regime/posture-aware picks)
SHARE_CAP = 500              # accumulate to 500 shares per stock, then rotate

# ---- Weekly short strike selection: regime x posture table -----------------
# HARD_CFM_RULE ("Genius System" market-timing table). The weekly short strike
# distance below spot is set by BOTH an ATR multiplier and a minimum ITM% floor,
# keyed by market regime (green/yellow/red) and the operator's risk posture
# (aggressive/conservative, an editable persisted setting — see strike_policy.py).
# The strike used is whichever candidate sits FURTHER below price (max
# protection wins):
#   atr_strike = price - atr_mult * ATR
#   itm_strike = price * (1 - itm_pct)
#   strike     = min(atr_strike, itm_strike), rounded to $0.50
# Values are (atr_mult, itm_pct as a decimal). RED entries are still blocked
# (Level 1 regime gate, unchanged); the RED row here only feeds the defend /
# roll-down strike selector for an already-open position during a red tape.
STRIKE_TABLE = {
    "green":  {"aggressive": (0.0, 0.00), "conservative": (0.5, 0.01)},
    "yellow": {"aggressive": (0.5, 0.02), "conservative": (1.0, 0.03)},
    "red":    {"aggressive": (1.0, 0.04), "conservative": (1.5, 0.05)},
}
STRIKE_POSTURES = ("aggressive", "conservative")
DEFAULT_STRIKE_POSTURE = "conservative"  # PROPOSED_DEFAULT until the operator picks
LEAP_ROLL_DTE = 30           # (legacy, unused) superseded by LEAP_ROLL_DTE_FLOOR
                              # in the LEAP capital-preservation section below
ROLL_MAX_DTE = 45            # short-roll picker offers expirations out to this DTE

# CFM sells a weekly short, so a monthly-only chain can't run the strategy. The
# scorecard flags names without weeklies; the status is near-static so it's cached
# for a week (override the TTL via WEEKLIES_TTL; disable via SCORECARD_CHECK_WEEKLIES=0).
WEEKLIES_CACHE_TTL = 7 * 24 * 3600

# ---- Earnings --------------------------------------------------------------
# Around earnings we either roll the short deep-ITM for protection or exit the
# position entirely, so the next report date is surfaced on every open position.
EARNINGS_WARN_DAYS = 7       # flag a position when earnings is within this many days

# ---- Alerting --------------------------------------------------------------
# The operator works a day job — "exit immediately" rules are only followable
# if the app notifies. Threshold provenance is labelled per constant:
#   HARD_CFM_RULE     — a stated CFM rule; changing it changes the strategy.
#   PROPOSED_DEFAULT  — a sensible default pending calibration / preference.

# HARD_CFM_RULE — 75% buyback: CFM roll guideline. When the short has lost >=75%
# of its sale premium with meaningful time left, roll early to capture juice.
BUYBACK_DECAY_PCT = 0.75
BUYBACK_MIN_DTE = 2            # HARD_CFM_RULE — ">2 days to expiration" leg of the rule

# HARD_CFM_RULE — coverage floor: a LEAP below 0.50 delta no longer behaves like
# stock, so the short call is effectively uncovered risk.
LEAP_DELTA_FLOOR = 0.50

# PROPOSED_DEFAULT — Schwab refresh tokens die at 7 days (no programmatic
# renewal); alert at day 5 so re-auth happens before data goes dark.
TOKEN_WARN_AGE_DAYS = 5

# PROPOSED_DEFAULT — cached daily OHLCV older than this on a market day means a
# silent fetch failure (the cache normally refreshes every ~12h trading day).
DATA_STALE_HOURS = 30.0

# PROPOSED_DEFAULT — shorts expiring within this many days and not yet rolled.
EXPIRY_WARN_DTE = 1

# PROPOSED_DEFAULT — evaluator schedule, ET, market days only: pre-market,
# ~30 min after open, mid-day, ~30 min before close.
ALERT_SCHEDULE_ET = ["08:30", "10:00", "12:30", "15:30"]
ALERT_LOG_MAX = 500            # PROPOSED_DEFAULT — alert history cap in state.json

# PROPOSED_DEFAULT — nightly maintenance slot (ET, every calendar day): refresh
# the earnings/dividend caches for held names and sync position snapshots.
MAINTENANCE_ET = "17:30"

# ---- Durability / backups --------------------------------------------------
# state.json is the single source of truth on a single Fly volume, so the
# nightly job keeps rotating local copies AND ships one copy off the machine.

# PROPOSED_DEFAULT — how many rotating nightly backups to retain under
# DATA_DIR/backups; older ones are pruned. Pre-migration snapshots are exempt
# from this rotation (kept forever — rare and small).
BACKUP_RETENTION = 30

# PROPOSED_DEFAULT — max state-file size (bytes) to attach to the nightly backup
# email. Above this the job emails a warning instead of the attachment and
# relies on the S3 off-machine path. ~5 MB.
BACKUP_EMAIL_MAX_BYTES = 5 * 1024 * 1024

# PROPOSED_DEFAULT — optional S3-compatible off-machine upload (Tigris/S3/B2),
# OFF by default. When on, boto3 is imported lazily and the target comes from
# env: BACKUP_S3_BUCKET, BACKUP_S3_ENDPOINT, BACKUP_S3_KEY_PREFIX, plus AWS_*
# credentials. Lets an operator point backups off-machine without a code change.
BACKUP_S3_ENABLED = os.environ.get("CFM_BACKUP_S3", "").strip() in ("1", "true", "yes")


# ---- LEAP capital preservation (long-leg lifecycle) ------------------------
# In a PMCC the LEAP *is* the deployed capital. The short side has a full
# management engine; these thresholds give the long leg the same discipline —
# when to roll it, whether juice is still covering its decay, and an early
# warning that its delta is bleeding before the 0.50 floor fires.

# PROPOSED_DEFAULT — roll the LEAP when it drops below this DTE. Theta on the
# long leg steepens meaningfully under ~90 DTE (a LEAP stops behaving like a
# calm stock proxy and starts decaying like a shorter-dated option), so roll
# before that. Supersedes the older, unused LEAP_ROLL_DTE constant above.
LEAP_ROLL_DTE_FLOOR = 90

# PROPOSED_DEFAULT — roll the LEAP when its remaining extrinsic is worth less
# than this many weeks of the position's own trailing juice: at that point the
# leg's decay is about to outrun what the shorts collect against it.
LEAP_MIN_EXTRINSIC_WEEKS = 4

# PROPOSED_DEFAULT — trailing window (completed weeks) for a position's average
# weekly juice, the denominator of leap_extrinsic_weeks_remaining and the
# juice-vs-burn maintenance number.
JUICE_TRAILING_WEEKS = 4

# PROPOSED_DEFAULT — consecutive completed weeks of net-negative maintenance
# (juice < LEAP decay) before the capital_burn alert fires. One bad week is
# noise; a sustained run means the flywheel is running backwards.
MAINTENANCE_NEGATIVE_WEEKS = 2

# PROPOSED_DEFAULT — days of per-position daily LEAP delta retained (the nightly
# maintenance job appends one point/day) to power the delta-velocity warning.
DELTA_HISTORY_DAYS = 30

# PROPOSED_DEFAULT — delta-velocity early warning: fire when the LEAP delta has
# fallen by more than this much over DELTA_VELOCITY_WINDOW sessions, while still
# ABOVE the 0.50 floor (below the floor, DELTA_UNCOVERED owns it). The existing
# floor alert fires late — most convexity damage is done by 0.50; this is the
# rate-based earlier tier.
DELTA_VELOCITY_DROP = 0.08
DELTA_VELOCITY_WINDOW = 5


def alerts_dry_run_default() -> bool:
    """Dry-run (log instead of send) via env; per-store override lives in the
    alert settings persisted to state."""
    return os.environ.get("CFM_ALERTS_DRY_RUN", "").strip() in ("1", "true", "yes")


# ---- Level 5 entry gate: Account & Juice ------------------------------------
# The 4-level gate checks the market/sector/stock/chart; Level 5 checks the
# ACCOUNT (cash, concentration) and the TRADE's income math before entry.

# HARD_CFM_RULE — CFM runs at most 2 concurrent positions at this capital tier;
# a third position leaves no reserve to defend either of the first two.
MAX_CFM_POSITIONS = 2

# PROPOSED_DEFAULT — max capital deployed into LEAPs (CFM sizing: $35-40K band).
MAX_DEPLOYED_CAPITAL = 38000

# PROPOSED_DEFAULT — the entry filters funnel into the hottest sector, so cap
# same-sector positions to keep one correlated tail from hitting the whole book.
MAX_POSITIONS_PER_SECTOR = 1

# PROPOSED_DEFAULT — post-trade free cash must cover a defensive reserve of
# 2xATR (in dollars) per share-equivalent for every open position:
#   reserve = sum over positions of RESERVE_ATR_MULT * ATR * contracts * 100
# (equivalently stock_price x contracts x 100 x 2 x ATR%, ATR as a fraction of
# price) — enough to buy back / roll every short through a 2-ATR adverse move.
RESERVE_ATR_MULT = 2.0

# HARD_CFM_RULE — the CFM cycle targets a 15-25% return on deployed capital
# over a 4-8 week cycle; the implied weekly juice floor is
# CYCLE_RETURN_MIN / CYCLE_WEEKS_MAX (~1.9%/week of LEAP cost basis).
CYCLE_RETURN_MIN = 0.15
CYCLE_RETURN_MAX = 0.25
CYCLE_WEEKS_MIN = 4
CYCLE_WEEKS_MAX = 8

# PROPOSED_DEFAULT — implied weekly yield more than this multiple of the
# ticker's own history-implied extrinsic = the market is pricing risk, not
# income ("juice too rich"). Warn, don't block.
JUICE_RICH_FACTOR = 1.75

# PROPOSED_DEFAULT — circuit-breaker (line-in-the-sand) default suggestion:
# max(MA50, entry - CIRCUIT_BREAKER_ATR_MULT * ATR). Operator-editable at entry;
# storing SOME line is required (HARD_CFM_RULE), only the formula is tunable.
CIRCUIT_BREAKER_ATR_MULT = 2.0

# HARD_CFM_RULE (candidate — OFF by default, pending confirmation): block
# pullback share-accumulation on any ticker whose kill switch reads non-green
# (red = exit in progress, yellow = RS3M thinning toward the kill line). The
# accumulation play buys weakness, the kill switch sells it — without this
# guard the two rules can add to a name the strategy is 1-2 days from exiting.
# Flip to True to enforce; the Positions tab surfaces the block either way.
BLOCK_ACCUMULATION_ON_RS_DETERIORATION = False

# ---- Capital ---------------------------------------------------------------
CAPITAL = 35000
RESERVE_REQUIRED = 13000

# Income milestones (monthly net juice) used by the position tracker.
MILESTONE_HALF_NUT = 2150
MILESTONE_QUIT_SAFE = 7500

# HARD_CFM_RULE — the weekly net-juice target band: 1-2% of deployed capital
# per week (the History tab draws this band on the weekly juice chart).
WEEKLY_JUICE_TARGET_PCT_MIN = 1.0
WEEKLY_JUICE_TARGET_PCT_MAX = 2.0

# PROPOSED_DEFAULT — wash-sale window (IRS: 30 days either side of a loss
# sale). Not tax software: the app only FLAGS re-entries inside the window so
# year-end isn't a surprise.
WASH_SALE_WINDOW_DAYS = 30
