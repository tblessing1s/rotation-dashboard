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


# ---- Live trading toggle ---------------------------------------------------
# Whether executed orders may be transmitted to the broker. Controlled either by
# the CFM_LIVE_TRADING env var (an ops override that force-enables it and locks
# the UI) or a persisted UI toggle stored on the volume so it survives restarts.
# Off by default — the honest paper path. Live transmission ALSO requires not
# being in demo mode; that combined gate lives in executor.live_transmit().
LIVE_TRADING_PATH = os.path.join(DATA_DIR, "live_trading.json")

_live_trading: bool | None = None


def live_trading_env() -> bool:
    """The CFM_LIVE_TRADING env override. When truthy it force-enables live
    trading and the UI toggle can't turn it off — a deliberate ops lock."""
    return os.environ.get("CFM_LIVE_TRADING", "").strip().lower() in ("1", "true", "yes")


def live_trading_enabled() -> bool:
    """Live trading is on when the env override is set OR the persisted UI toggle
    is on. Single source of truth for executor.live_enabled()."""
    if live_trading_env():
        return True
    global _live_trading
    if _live_trading is None:
        try:
            import json
            with open(LIVE_TRADING_PATH, encoding="utf-8") as fh:
                _live_trading = bool(json.load(fh).get("live", False))
        except (OSError, ValueError):
            _live_trading = False
    return _live_trading


def set_live_trading_enabled(on: bool) -> None:
    """Persist the UI toggle to the volume. Raises if the env override is forcing
    it on — that lock is deliberate and must be cleared at the deploy level."""
    if live_trading_env():
        raise RuntimeError(
            "CFM_LIVE_TRADING is set in the environment — live trading is locked on "
            "at the deploy level and can't be changed from the UI.")
    global _live_trading
    import json
    _live_trading = bool(on)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = LIVE_TRADING_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"live": _live_trading}, fh)
    os.replace(tmp, LIVE_TRADING_PATH)


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
STOCK_RS_VS_SPY_MIN = 5.0      # stock RS3M vs SPY > +5% (growth-leader bar)
# ETFs run as an income sleeve, not growth leaders, so the "beat SPY" leg uses a
# lower bar: merely leading SPY (> 0%) is enough. This mirrors the lower ETF
# juice bar (weekly_yield_target_pct) — an income ETF shouldn't have to outrun
# SPY by 5% to be entry-ready. (Sector ETFs already waive the beats-sector leg;
# non-sector ETFs still beat their assigned sector.)
STOCK_RS_VS_SPY_MIN_ETF = 0.0
STOCK_RS_VS_SECTOR_MIN = 0.0  # stock RS3M vs Sector > 0


def rs_vs_spy_min(is_etf: bool = False) -> float:
    """The RS3M-vs-SPY floor for the Level 3 'beats SPY' leg: the lower ETF bar
    for an ETF, the growth-leader bar for a stock."""
    return STOCK_RS_VS_SPY_MIN_ETF if is_etf else STOCK_RS_VS_SPY_MIN
CONSOLIDATION_ATR_PCT_MAX = 5.0   # daily ATR% of price below this = consolidating
CONSOLIDATION_MA21_DIST_MAX = 4.0  # within this % of MA21 = near the mean

# ---- Indicator calibration (matches thinkorswim daily studies) -------------
RS3M_LOOKBACK = 63            # ~3 months of trading days
ATR_WINDOW = 9               # CFM uses a 9-day ATR for strike spacing
RSI_WINDOW = 14
MA_WINDOW = 21
VOL_AVG_WINDOW = 20
HISTORY_DAYS = 320           # daily bars pulled / cached per symbol

# ---- Smart intraday refresh (hot tiering) ----------------------------------
# The whole universe is fetched once pre-open (the warm-up) and then frozen in
# the parquet cache for the day. That's right for the long tail, but the handful
# of names carrying live risk — open positions, entry candidates, earnings-
# imminent — should stay current intraday. refresh_policy picks that small "hot"
# set and the scheduler force-refreshes it on this cadence during market hours.
HOT_REFRESH_MINUTES = 15     # market-hours cadence for refreshing the hot set
HOT_TICKERS_MAX = 40         # cap on the hot set (open positions are never dropped)
# Ignore a scorecard memo older than this (seconds) when reading the GO/earnings
# candidate pool — so an overnight-stale sweep never drives the intraday picks.
HOT_CANDIDATE_MAX_AGE = 2 * 3600

# ---- CFM mechanics ---------------------------------------------------------
# Default LEAP position size (deep-ITM calls per stock). Pre-fills the entry
# ticket's quantity and sizes the capital/reserve gate when no quantity is
# passed; it's editable per trade. Override the default via the LEAP_CONTRACTS
# env var (e.g. on Fly) without touching code.
LEAP_CONTRACTS = int(os.environ.get("LEAP_CONTRACTS") or 1)
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
# PROPOSED_DEFAULT — deep-ITM protective roll THROUGH an earnings report: when a
# candidate roll week spans the next report, the picker suggests a strike this
# deep (the further-below-spot of the ATR-distance and ITM%-floor), beyond any
# regime/posture cell, so the short keeps intrinsic cover across the gap.
EARNINGS_ROLL_ATR_MULT = 2.5
EARNINGS_ROLL_ITM_PCT = 0.08

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

# PROPOSED_DEFAULT — evaluator schedule, ET, market days only. Fixed anchor
# slots: pre-market, ~30 min after open, mid-day, ~30 min before close, and a
# post-close sweep. The full ALERT_SCHEDULE_ET below merges these with the
# post-open gap-cadence slots.
ALERT_SCHEDULE_ANCHORS_ET = ["08:30", "10:00", "12:30", "15:30", "16:15"]

# PROPOSED_DEFAULT — post-close sweep (the 16:15 anchor above). The kill switch's
# "confirmed close" condition and an end-of-day circuit-breaker breach can only
# be evaluated AFTER the 16:00 close, so the 15:30 slot is too early to see them;
# without a post-close slot their earliest fire is the next morning's 08:30 —
# i.e. "exit immediately" degrades to "exit at tomorrow's open". A ~16:15 slot
# pages the same evening instead. The scheduler force-refreshes the hot set at
# this slot first so the official close is in the cache before evaluation.
POST_CLOSE_SLOT_ET = "16:15"

# PROPOSED_DEFAULT — gap-risk cadence. The open (09:30) to the first fixed slot
# (10:00) is a 30-min blind window: a gap straight through a position's circuit
# breaker at 09:31 isn't seen until 10:00, and the post-open window is
# statistically high-volatility. CFM deliberately uses alerts, not resting stop
# orders, so the evaluation cadence IS the only tripwire — tighten it over the
# first OPEN_GAP_WINDOW_MIN minutes after the open, every OPEN_GAP_CADENCE_MIN.
MARKET_OPEN_ET = "09:30"
OPEN_GAP_WINDOW_MIN = 30
OPEN_GAP_CADENCE_MIN = 10


def _open_gap_slots() -> list[str]:
    """Extra ET evaluation slots across the post-open gap window — open+cadence,
    open+2·cadence, … up to and including open+window. With the defaults
    (cadence 10, window 30) that's 09:40, 09:50, 10:00 (10:00 is already an
    anchor, so the merge dedups it). Empty when the cadence/window are disabled."""
    from datetime import datetime as _dt, timedelta as _td
    if OPEN_GAP_CADENCE_MIN <= 0 or OPEN_GAP_WINDOW_MIN <= 0:
        return []
    open_t = _dt.strptime(MARKET_OPEN_ET, "%H:%M")
    out, step = [], OPEN_GAP_CADENCE_MIN
    while step <= OPEN_GAP_WINDOW_MIN:
        out.append((open_t + _td(minutes=step)).strftime("%H:%M"))
        step += OPEN_GAP_CADENCE_MIN
    return out


# The evaluator slots the scheduler actually runs: the fixed anchors plus the
# post-open gap-cadence slots, sorted and deduped. The first slot (08:30) stays
# the pre-market anchor everything else keys off (e.g. morning reconciliation).
ALERT_SCHEDULE_ET = sorted(set(ALERT_SCHEDULE_ANCHORS_ET) | set(_open_gap_slots()))
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

# ---- ETF income sleeve ------------------------------------------------------
# ETFs carry lower IV than single growth stocks, so their weekly juice is
# thinner — but steadier, with no earnings-gap risk. They run as a lower-income,
# grow-the-position sleeve: same one book, but a LOWER juice-adequacy bar at
# entry than the growth-stock bar (which is the CFM 15-25%/4-8wk rule, ~1.9%/wk).
# PROPOSED_DEFAULT — a deliberate variant of the stated rule for the lower-vol
# ETF sleeve, not a change to the rule itself.
ETF_WEEKLY_JUICE_TARGET_PCT = 1.0

# Tickers treated as ETFs for the income profile above, in addition to the 11
# sector-ETF headers + SPY (which are always ETFs). Extend this as you add ETFs
# to the universe so they get the ETF juice bar.
KNOWN_ETFS = {"QQQ", "IWM", "DIA", "SMH", "ARKK", "XBI", "GDX", "XOP", "KRE", "XHB"}

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

# ---- Position reconciliation (state.json vs Schwab) ------------------------
# Nothing else verifies that state.json matches what the brokerage account
# actually holds. The reconciler detects divergence (assignment, expiry,
# partial fill, corporate action, or a bug), freezes the affected position, and
# alerts — the operator resolves. It never auto-corrects state.

# PROPOSED_DEFAULT — how many past reconciliation reports to retain in
# state.reconciliation.history (the last full report is kept separately).
RECONCILE_HISTORY_MAX = 30

# PROPOSED_DEFAULT — reconciliation is expected to run at least nightly + each
# pre-market slot; if the last SUCCESSFUL run is older than this while Schwab is
# connected and positions are open, reconcile_stale fires. Silence is itself a
# failure signal (the positions call failing, the scheduler wedged, etc.).
RECONCILE_STALE_HOURS = 36
