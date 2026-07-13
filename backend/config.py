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


def market_settle_gate_enabled() -> bool:
    """Whether the market-settle execution gate ENFORCES (blocks / defers orders
    inside the settle window, close blackout, and off-hours). Off by default so the
    gate rolls out deliberately — it changes *live execution timing*, exactly the
    kind of behaviour change that wants an explicit ops opt-in (mirrors
    CFM_LIVE_TRADING). ``CFM_MARKET_SETTLE_GATE=1`` turns enforcement on.

    The gate's *verdict* is always computed and surfaced (so the PENDING_SETTLE
    staging / countdown UI works regardless); this flag governs only whether a
    blocked verdict actually refuses the order."""
    return os.environ.get("CFM_MARKET_SETTLE_GATE", "").strip().lower() in ("1", "true", "yes")


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
# NOTE: these feed the legacy breadth/VIX regime AND the SECONDARY breadth/VIX
# confirmation indicators of the Genius four-light regime below. Breadth and VIX
# do NOT determine the regime traffic light (only the four lights + the yellow
# dwell do) — they are shown alongside it for the operator's own read. See
# regime_genius.py + screening.regime().
REGIME_BREADTH_GREEN = 60      # % of universe above 50-DMA for a green tape
REGIME_BREADTH_RED = 40
VIX_CALM = 18                  # below = calm
VIX_ELEVATED = 24             # above = risk-off

# ---- Genius four-light market regime (CFM course canon) ---------------------
# The Cash Flow Machine "Genius System" votes four binary indicator "lights"
# computed on the market index (SPY daily bars) to a green/yellow/red condition,
# then holds a YELLOW condition for a minimum dwell to stop it flapping. The
# course specifies the indicator TYPES and the voting/dwell logic (HARD_CFM_RULE);
# it does NOT specify parameters (MA lengths, SAR settings, which oscillator), so
# those are PROPOSED_DEFAULT and calibration-tunable (see calibration.regime_series).
# Provenance tags are load-bearing (as elsewhere in this file):
#   HARD_CFM_RULE    — course canon; changing it changes the strategy.
#   PROPOSED_DEFAULT — a placeholder pending calibration; tune later.
#
# The four lights (each GREEN when bullish, RED when bearish):
#   1. close vs slow MA        — close above slow MA = GREEN
#   2. fast MA vs slow MA      — fast above slow = GREEN
#   3. Parabolic SAR vs close  — SAR dots under price = GREEN
#   4. momentum vs zero        — oscillator above zero = GREEN
GENIUS_VOTE_GREEN_MIN = 3      # HARD_CFM_RULE — >=3 of 4 GREEN lights -> GREEN; 2/2 -> YELLOW; >=3 RED -> RED
GENIUS_YELLOW_DWELL_DAYS = 3   # HARD_CFM_RULE — a YELLOW condition cannot change for at least this many TRADING days
GENIUS_INDEX_SYMBOL = "SPY"    # PROPOSED_DEFAULT — course says "the market"; SPY is the app benchmark
GENIUS_SLOW_MA = 50            # PROPOSED_DEFAULT — slow MA length (SMA), lights 1 & 2
GENIUS_FAST_MA = 21            # PROPOSED_DEFAULT — fast MA length (EMA), light 2
GENIUS_SAR_AF_STEP = 0.02      # PROPOSED_DEFAULT — Parabolic SAR acceleration step (standard Wilder)
GENIUS_SAR_AF_MAX = 0.20       # PROPOSED_DEFAULT — Parabolic SAR acceleration cap (standard Wilder)
GENIUS_MOMENTUM_ROC = 10       # PROPOSED_DEFAULT — ROC(n) sign is the zero-line oscillator (light 4).
                              # Simplest zero-line momentum; MACD-histogram-sign is the documented alternative.

# ---- Secondary regime indicators (breadth + VIX) ---------------------------
# Breadth and VIX are SECONDARY, informational confirmation indicators only —
# they are shown ALONGSIDE the regime for the operator's own read but do NOT
# determine the traffic light. The light is set purely by the four Genius lights
# and the yellow dwell (regime_genius.compute_trace). These reference levels only
# flag whether breadth/VIX are confirming or diverging from a green tape.
# PROPOSED_DEFAULT — breadth below this % of the universe above its 50-DMA is
# "diverging" (a non-confirming tape). Anchored to the green breadth floor.
BREADTH_CONFIRM_MIN_PCT = REGIME_BREADTH_GREEN
VIX_ELEVATED_THRESHOLD = 25    # PROPOSED_DEFAULT — VIX above this is flagged "elevated" (informational)
# Days of daily regime history retained in DATA_DIR/regime_history.json (derived
# telemetry, recomputable from cached bars — backfilled, never an execution).
REGIME_HISTORY_DAYS = 400      # PROPOSED_DEFAULT — ~1.5 trading years of daily regime records

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

# ---- Tiered market-data scheduler ------------------------------------------
# Replaces the flat HOT_REFRESH_MINUTES cadence above (which re-fetched daily
# BARS for every hot name every 15 min) with a tier-aware polling budget that
# concentrates provider calls where data freshness changes a decision. Names are
# assigned a tier from position state + entry-queue rank (market_scheduler.
# assign_tiers), and each (tier, data_kind) has its own due-cadence (fetch_due).
# Intraday freshness now comes from ONE batched Schwab quote per interval overlaid
# on the frozen daily bars — not from re-fetching bars. See market_scheduler.py.
#
# Provenance tags (as elsewhere in this file):
#   HARD_CFM_RULE    — a stated strategy rule; changing it changes the strategy.
#   PROPOSED_DEFAULT — a placeholder pending calibration; tune later.
#
# NOTE ON PROVIDERS: this codebase has no yfinance (Schwab primary + Alpha
# Vantage fallback only). The prompt's "yfinance/cache" Tier 2/3 source maps to
# the existing parquet daily-bar cache, refreshed by the EOD batch via Schwab
# (AV fallback). Provider routing is per-tier and swappable (data_transport).

POLL_T0_SECONDS = 120            # PROPOSED_DEFAULT — Tier 0 (open positions) quote cadence
POLL_T1_SECONDS = 900            # PROPOSED_DEFAULT — Tier 1 (on-deck) quote cadence
POLL_ESCALATED_SECONDS = 30      # PROPOSED_DEFAULT — max freshness under escalation
QUEUE_ONDECK_COUNT = 5           # PROPOSED_DEFAULT — top-N queue candidates promoted to Tier 1
SLOT_HORIZON_DAYS = 14           # PROPOSED_DEFAULT — a slot opening within this window makes a name on-deck
REFRESH_KILLSWITCH_PER_DAY = 3   # PROPOSED_DEFAULT — intraday RS3M (vs SPY / vs Sector) recomputes/day
ESCALATION_INDEX_MOVE_PCT = 1.0  # PROPOSED_DEFAULT — SPY / held-sector intraday move that triggers a market escalation (%)
ESCALATION_DECAY_MINUTES = 60    # PROPOSED_DEFAULT — an escalation decays after this long without re-trigger
EOD_BATCH_TIME_ET = "16:30"      # PROPOSED_DEFAULT — the once-daily EOD bar batch fires after this ET time
BUDGET_SOFT_LIMIT_PCT = 80       # PROPOSED_DEFAULT — shed when a provider crosses this % of its configured daily limit
TIER0_NEVER_SHED = True          # HARD_CFM_RULE — open-position monitoring is never sacrificed
STALE_BLOCKS_GO = True           # HARD_CFM_RULE — no GO verdict on stale inputs

# Max-age per (tier, data_kind) is DERIVED from the poll cadence, not hardcoded
# independently: a datum is "stale" once it is older than this multiple of the
# interval that was supposed to refresh it. (PROPOSED_DEFAULT multiplier.)
MAX_AGE_POLL_MULT = 2.0          # PROPOSED_DEFAULT — stale = older than 2x its poll interval
# EOD data kinds (bars, and the once-daily chain snapshot) aren't intraday-polled,
# so their staleness ceiling reuses the existing daily-bar tolerance rather than a
# poll multiple: cached OHLCV older than this on a market day is a silent failure.
# Mirrors DATA_STALE_HOURS (defined below in the alerting section); kept as a
# literal here because that constant hasn't been assigned yet at this point in the
# module. A guard at the bottom of the file asserts the two stay in sync.
EOD_MAX_AGE_HOURS = 30.0  # == DATA_STALE_HOURS

# Known/configured provider daily call limits, for budget accounting + the soft-
# limit shed trigger. Schwab publishes ~120 req/min; there is no hard published
# daily cap, so the daily figure is a PROPOSED_DEFAULT ceiling for budgeting only.
# Alpha Vantage free tier is the real constraint (25/day historically; 500 on some
# keys) — set via env to match the operator's key. Override either via env.
SCHWAB_DAILY_CALL_LIMIT = int(os.environ.get("SCHWAB_DAILY_CALL_LIMIT") or 40000)      # PROPOSED_DEFAULT
ALPHA_VANTAGE_DAILY_CALL_LIMIT = int(os.environ.get("ALPHA_VANTAGE_DAILY_CALL_LIMIT") or 500)  # PROPOSED_DEFAULT

# Schwab HTTP 429 / Retry-After exponential backoff (the Schwab path has none
# today). Base delay doubles per attempt, capped, for at most N attempts before
# falling through to the per-tier fallback provider. PROPOSED_DEFAULT throughout.
SCHWAB_BACKOFF_BASE_SECONDS = 1.0
SCHWAB_BACKOFF_MAX_SECONDS = 16.0
SCHWAB_MAX_RETRIES = 4

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

# HARD_CFM_RULE — Travis's documented short-strike depth policy keyed to the
# published market regime: 1.5x ATR distance in a GREEN tape, 2.0x ATR in YELLOW,
# and RED blocks new entries (the Level 1 regime gate, unchanged). These are the
# canonical multiples the strategy is documented against.
#
# SCOPED FOLLOW-UP (not applied here): the live STRIKE_TABLE above encodes a
# DIFFERENT, internally-consistent scheme (shallower-when-safe -> deeper-when-
# dangerous: conservative green 0.5x, yellow 1.0x, red 1.5x) that predates this
# policy and drives the already-open-position defend/roll-down selector across
# BOTH postures. Reconciling the table to these multiples changes calibrated
# strategy numbers for aggressive + RED defend rows too, so it is deliberately
# left for a separate, reviewable change (see CHANGELOG "strike-policy follow-up").
# The regime plumbing is already correct: strike_policy.suggest_strike() consumes
# screening.regime()["status"], which is now the dwell-adjusted PUBLISHED regime.
STRIKE_ATR_MULT_GREEN = 1.5    # HARD_CFM_RULE — documented GREEN short-strike ATR distance
STRIKE_ATR_MULT_YELLOW = 2.0   # HARD_CFM_RULE — documented YELLOW short-strike ATR distance
LEAP_ROLL_DTE = 30           # (legacy, unused) superseded by LEAP_ROLL_DTE_FLOOR
                              # in the LEAP capital-preservation section below
ROLL_MAX_DTE = 45            # short-roll picker offers expirations out to this DTE

# Minimum DTE a weekly short must carry for its extrinsic to be a fair juice
# comparison. A coming-Friday with only a day or two left is a stub whose thin
# time value understates the weekly yield and falsely trips the Level-5 juice
# gate. When the current weekly is below this, the option chain compares against
# (and suggests) the next week's weekly — which has a full week of premium.
WEEKLY_MIN_COMPARISON_DTE = 5

# CFM sells a weekly short, so a monthly-only chain can't run the strategy. The
# scorecard flags names without weeklies; the status is near-static so it's cached
# for a week (override the TTL via WEEKLIES_TTL; disable via SCORECARD_CHECK_WEEKLIES=0).
WEEKLIES_CACHE_TTL = 7 * 24 * 3600

# ---- Earnings --------------------------------------------------------------
# Around earnings we either roll the short deep-ITM for protection or exit the
# position entirely, so the next report date is surfaced on every open position.
EARNINGS_WARN_DAYS = 7       # flag a position when earnings is within this many days

# The earnings guardrail is only as good as the calendar behind it. Free-tier
# Alpha Vantage dates are frequently wrong or late-updated, and a wrong date fails
# SILENTLY — the earnings alert simply doesn't fire and you roll into a report
# unprotected. Two mitigations (PROPOSED_DEFAULT thresholds):
#  * cross-check Schwab fundamentals when the endpoint exposes a next-earnings
#    field: fill when Alpha Vantage is blank, and flag a conflict when the two
#    disagree by more than EARNINGS_CONFLICT_DAYS;
#  * flag a held name whose earnings date hasn't been refreshed within
#    EARNINGS_STALE_DAYS. Nightly maintenance refreshes held names every day, so a
#    stale date means the refresh path itself is broken — exactly the state in
#    which you'd roll in blind.
EARNINGS_STALE_DAYS = 4
EARNINGS_CONFLICT_DAYS = 3
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

# PROPOSED_DEFAULT — a short's remaining extrinsic rising this many percent ABOVE
# its entry extrinsic is the "underwater on the leg because vol spiked" event. The
# payout-side capture meter clamps/floors that case to 0% (correct for income
# accounting — an IV spike must never book as negative income), which hides it
# from the defend view; this LOW-severity alert surfaces it. Risk-path only — it
# changes no threshold, trigger, or strike rule. [CAPTURE_CLAMP_SCOPE]
EXTRINSIC_ABOVE_ENTRY_ALERT_PCT = 25.0

# Assignment risk is fundamentally an EXTRINSIC problem, not a dividend problem: a
# deep-ITM short is assignable whenever its extrinsic collapses to ~0 (the
# counterparty forfeits no time value by exercising), no ex-date required — a
# dividend just makes early exercise rational on a specific date. So the trigger
# is "an ITM short's extrinsic below this floor", with the coming dividend as an
# ESCALATION of it (extrinsic below the dividend before ex-div; see the ASSIGNMENT
# section of the assignment/dividend rule). PROPOSED_DEFAULT floor — a few cents
# of remaining time value per share.
ASSIGNMENT_EXTRINSIC_FLOOR = 0.10

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

# ---- Market-settle execution gate (time-of-day order discipline) -----------
# The first ~30 min after the open and the last ~15 before the close are
# structurally hostile to this strategy's order types (widest spreads, unreliable
# IV marks, gap-distorted daily-bar signals, closing-auction imbalances). ALERTS
# still fire immediately; only ORDER EXECUTION is gated/deferred by action type,
# with one narrow gap-emergency exception for DEFENSE/EXIT_KILL. The gate is a pure
# function (backend/execution_gate.py) wired into the shared executor.execute path.
# Provenance tags below are load-bearing (see the atomic-roll section for the key).
MARKET_SETTLE_MINUTES = 30          # PROPOSED_DEFAULT — post-open blackout for entries/rolls; DEFENSE/EXIT only via gap-emergency
ENTRY_EARLIEST_MINUTES = 60         # PROPOSED_DEFAULT — entries additionally blocked until open+this (entries are never urgent)
CLOSE_BLACKOUT_MINUTES = 15         # PROPOSED_DEFAULT — pre-close blackout (keys off the ACTUAL close, early-close included)
GAP_EMERGENCY_ATR_MULT = 2.0        # PROPOSED_DEFAULT — overnight gap vs position >= this * ATR unlocks the pre-settle emergency path
OPENING_RANGE_MINUTES = 15          # PROPOSED_DEFAULT — opening-range window for the gap-continuation (break-of-range-low) confirmation
EMERGENCY_MIN_PRINT_MINUTES = 5     # PROPOSED_DEFAULT — underlying must print two-sided quotes >= this before an emergency execution
SPREAD_QUALITY_MULT = 2.0           # PROPOSED_DEFAULT — current spread > this * trailing average -> WIDE_SPREAD acknowledge (post-settle)
SPREAD_BASELINE_MIN_SAMPLES = 5     # PROPOSED_DEFAULT — trailing spread samples required before a baseline exists ("no baseline" until then)
NO_MARKET_ORDERS_AT_OPEN = True     # HARD_CFM_RULE — inside the settle window market orders are refused for EVERY action, emergency included
EMERGENCY_NEVER_FOR_ENTRY = True    # HARD_CFM_RULE — the gap-emergency path never applies to ENTRY or routine rolls
CANCEL_NEVER_GATED = True           # HARD_CFM_RULE — canceling a resting order is allowed any time the broker accepts cancels
# PROPOSED_DEFAULT — the operator's local timezone, shown alongside ET in window-aware
# push copy ("executable 10:00 ET (9:00 CT)"). Override with CFM_OPERATOR_TZ.
OPERATOR_TZ = (os.environ.get("CFM_OPERATOR_TZ") or "America/Chicago").strip() or "America/Chicago"

# ---- Paper-fill slippage (mid-fill assumption) -----------------------------
# Paper fills are booked at the quoted MIDPOINT, but deep-ITM options rarely fill
# at mid — so every paper cycle's juice is optimistic by ~half the spread, twice a
# week, compounding through the payback meter and the calibration harness's
# threshold tuning. Until enough live fills exist to measure it, paper results
# carry a mid-fill caveat and a default per-leg haircut; once realized slippage is
# measured (broker fill vs the reference mid captured at order time), that
# supersedes the assumption. See backend/slippage.py.
# PROPOSED_DEFAULT — assumed adverse slippage per leg as a fraction of the option
# mid, applied until live data replaces it (~half a 10%-of-mid deep-ITM spread).
ASSUMED_SLIPPAGE_PCT = 0.05
# PROPOSED_DEFAULT — live fills needed before measured slippage supersedes the
# assumed haircut (a handful, so one bad fill doesn't set the calibration).
SLIPPAGE_MIN_FILLS = 5

# ---- Atomic spread roll (short-call roll as ONE Schwab NET order) -----------
# The weekly short-call roll goes to Schwab as a single two-leg complex order
# (buy-to-close old short + sell-to-open new short) at one NET_CREDIT/NET_DEBIT
# limit, so the pair fills as a unit or not at all — no legging risk, one net
# crossing instead of two. See backend/executor.py (_roll_short) and
# backend/schwab_api.py (build_roll_order). Provenance tags are load-bearing:
# HARD_CFM_RULE constants encode strategy invariants; PROPOSED_DEFAULT ones are
# tunable; LIVE_VERIFY ones must be confirmed against a real Schwab account
# before production reliance.

# PROPOSED_DEFAULT — feature flag. When True the live roll transmits ONE atomic
# NET order; when False it falls back to the legacy legged path (two independent
# single-leg orders, which carry legging risk). Kept selectable during live
# verification of the complex-order path.
ATOMIC_ROLLS_ENABLED = True

# HARD_CFM_RULE — matches the single-leg invariant: an unfilled order is canceled
# and leaves no execution trace. The roll order is always a DAY order.
ROLL_ORDER_DURATION = "DAY"

# HARD_CFM_RULE — the net limit defaults to the reference net mid captured at
# ticket time (mid(new short) − mid(old short)); consistent with fill_verify's
# reference-mid design. The operator may adjust before submit.
ROLL_NET_PRICE_SOURCE = "reference_net_mid"

# PROPOSED_DEFAULT / LIVE_VERIFY — complexOrderStrategyType for a same-underlying,
# different-strike/expiry call pair. CUSTOM is the safe superset Schwab accepts
# for any strike/expiry combination and is what the atomic open/exit already use;
# Schwab also documents DIAGONAL (different expiry) / VERTICAL (same expiry).
# MUST be confirmed against a live Schwab account (spread-approval logic may
# prefer a specific enum) before production reliance — do NOT assume CUSTOM is
# universally accepted for approval purposes.
ROLL_COMPLEX_STRATEGY_TYPE = "CUSTOM"

# HARD_CFM_RULE — if Schwab ever reports a leg-imbalanced fill (one leg filled,
# the other not), freeze the position and alert; reconciliation NEVER
# auto-corrects. "freeze" is the only supported value.
ROLL_LEG_IMBALANCE_ACTION = "freeze"

# PROPOSED_DEFAULT — a paper roll simulates ONE net crossing (net mid ± a single
# haircut), not two per-leg crossings. Reported by slippage.py; the immutable
# ledger books at the net mid (paper fills are never haircut on the ledger).
PAPER_ROLL_HAIRCUT_CROSSINGS = 1

# ---- Entry (atomic open) order type ----------------------------------------
# The live entry is ONE two-leg NET_DEBIT diagonal (buy-to-open the deep-ITM LEAP
# + sell-to-open the weekly short) so it fills as a unit or not at all — the same
# atomic pattern as the roll. These mirror the roll's provenance-tagged knobs so
# the entry and roll can't silently disagree on strategy type / duration.

# HARD_CFM_RULE — an unfilled entry is canceled and leaves no execution trace,
# exactly like every other CFM order. The entry ticket is always a DAY order.
ENTRY_ORDER_DURATION = "DAY"

# PROPOSED_DEFAULT / LIVE_VERIFY — complexOrderStrategyType for the entry diagonal
# (deep-ITM LEAP long + near-dated short: different strike AND different expiry).
# CUSTOM is the safe superset Schwab accepts for any strike/expiry combination and
# is what the atomic open/exit have used to date; DIAGONAL is the documented enum
# for a different-expiry pair. MUST be confirmed against a live Schwab account
# (spread-approval logic may prefer a specific enum) before production reliance.
ENTRY_COMPLEX_STRATEGY_TYPE = "CUSTOM"

# ---- Order lifecycle: cancel-and-retry state machine -----------------------
# Provenance tags are load-bearing (see the roll block above): HARD_CFM_RULE
# encodes an invariant, PROPOSED_DEFAULT is tunable, LIVE_VERIFY must be confirmed
# against a real Schwab account. The lifecycle itself is order_lifecycle.py (pure
# state machine) + executor.py (broker I/O). None of these auto-submit an order.

# PROPOSED_DEFAULT — how long an unfilled live order may sit WORKING before the
# operator/monitor initiates a broker cancel. A DAY limit that hasn't filled this
# long has almost certainly missed the mid; chasing is a deliberate, gated choice
# (see REPRICE_ON_RETRY), never automatic.
ORDER_FILL_TIMEOUT_SEC = 45

# PROPOSED_DEFAULT — Schwab's DELETE only ACKNOWLEDGES a cancel; the order cancels
# asynchronously (WORKING -> PENDING_CANCEL -> CANCELED) and can still fill. After
# the DELETE we re-poll the order this many times, waiting this long between polls,
# to CONFIRM it reached a terminal state before claiming it canceled. Bounded so a
# stuck PENDING_CANCEL surfaces as such rather than hanging. Set the interval to 0
# in tests for an effectively mocked clock.
CANCEL_POLL_INTERVAL_SEC = 0.4
CANCEL_POLL_MAX_ATTEMPTS = 6

# PROPOSED_DEFAULT — how many times a NEW order may be submitted for the SAME
# position intent within one app session (a place, cancel, and re-place counts as
# two attempts). After this many, the app alerts and STOPS auto-offering a retry —
# repeated no-fills mean the price or the thesis is wrong, not that we should keep
# crossing the spread. Enforced in order_lifecycle.check_resubmit.
MAX_RESUBMIT_ATTEMPTS = 3

# PROPOSED_DEFAULT — how a resubmitted order adjusts its limit toward the ask.
# "none" (the default) re-sends at the SAME mid-seeded limit: honest, never chases
# price, but may miss again if the market moved. Any price-chasing variant must be
# an explicit, logged, config-gated behavior — silently walking the limit toward
# the ask is how a "just get it filled" retry loop quietly pays up. "none" is the
# only value wired today; a future "one_tick"/"toward_ask_frac" stays opt-in.
REPRICE_ON_RETRY = "none"

# HARD_CFM_RULE — the named invariant behind the resubmission gate: a new order for
# a position intent may ONLY be sent once the prior order is confirmed TERMINAL at
# the broker AND its fill is reconciled. This flag exists so the rule is a checked,
# greppable constant, not a convention. Turning it off is a strategy change and is
# not a supported configuration; it is asserted, not consulted-and-skipped.
NO_RESUBMIT_BEFORE_TERMINAL = True

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
# env: BACKUP_S3_BUCKET (falls back to BUCKET_NAME, which `fly storage create`
# sets), BACKUP_S3_ENDPOINT (falls back to boto3's AWS_ENDPOINT_URL_S3, also
# set by Tigris), BACKUP_S3_KEY_PREFIX, plus AWS_* credentials. On Fly the
# whole setup is: `fly storage create` + `fly secrets set CFM_BACKUP_S3=1`.
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


# ---- Weekly theta burn & net juice -----------------------------------------
# The LEAP is held ~8 weeks and exited/rolled around 130-140 DTE — the FLATTEST
# part of the theta curve. Only the extrinsic consumed during that hold window is
# a true cost (the rest is recovered when the LEAP is sold, minus slippage). So
# the burn a position must clear is the extrinsic DIFFERENCE between two model
# prices — at the current DTE and at the planned exit DTE — NOT the whole entry
# extrinsic and NOT a straight-line proration of it. Provenance per constant:
#   HARD_CFM_RULE    — a stated strategy rule; changing it changes the strategy.
#   PROPOSED_DEFAULT — a placeholder pending calibration once realized-burn data
#                      accumulates; tune later.

# HARD_CFM_RULE — burn is the difference of two Black-Scholes model prices
# (extrinsic at current DTE minus extrinsic at planned-exit DTE, same spot & IV),
# never total_extrinsic x (held_days / total_days). Straight-line proration
# averages in the steep never-held tail and overstates front-end burn ~3x.
BURN_IS_MODEL_DIFF = True

# HARD_CFM_RULE — gross juice is never the ranking or portfolio-rollup metric.
# The headline per-position figure and the entry-queue ranking key are
# net juice/week = juice collected/week - burn/week (with slippage).
NET_JUICE_IS_HEADLINE = True

# PROPOSED_DEFAULT — mid of the observed 130-140 DTE exit band. The default
# per-position planned_exit_dte; all burn math keys off this, not off LEAP
# expiration. Seeded onto existing positions by the v14 migration.
PLANNED_EXIT_DTE = 135

# PROPOSED_DEFAULT — mid of the 185-195 DTE entry band. The hypothetical LEAP
# entry DTE used when ranking entry candidates on net juice (no live position yet).
LEAP_ENTRY_DTE_DEFAULT = 190

# PROPOSED_DEFAULT — LEAP round-trip exit slippage as a % of the LEAP price, used
# when no fresh option chain is cached to read a live bid-ask spread from. Half
# the spread x 2 (round trip) is preferred when a chain is available.
LEAP_SLIPPAGE_PCT_FALLBACK = 0.5

# PROPOSED_DEFAULT — coverage ratio = juice/week / burn/week (with slippage).
# healthy >= COVERAGE_HEALTHY; marginal in [COVERAGE_MARGINAL, COVERAGE_HEALTHY);
# flagged below COVERAGE_MARGINAL.
COVERAGE_HEALTHY = 3.0
COVERAGE_MARGINAL = 2.0

# PROPOSED_DEFAULT — when burn is floored near zero (deep-ITM drift after a
# run-up), a near-zero denominator would make coverage explode; cap the DISPLAYED
# ratio here and surface the low_extrinsic_flag instead of an absurd number.
COVERAGE_DISPLAY_CAP = 10.0

# PROPOSED_DEFAULT — model extrinsic per share below which burn is floored at
# zero and low_extrinsic_flag is set (a deep-ITM LEAP with ~no time value left).
# Mirrors ASSIGNMENT_EXTRINSIC_FLOOR — a few cents of time value per share.
BURN_LOW_EXTRINSIC_FLOOR = 0.10

# PROPOSED_DEFAULT — trailing realized-vs-projected burn divergence (%) beyond
# which a warning badge surfaces. This doubles as a live verification harness for
# the BS engine + put-IV substitution; persistent divergence is a soft warning,
# never a hard failure.
BURN_DIVERGENCE_WARN_PCT = 25

# PROPOSED_DEFAULT — when a position is held past its planned exit without a LEAP
# roll, extend the projection window in increments of this many weeks and
# recompute (burn/week rises as the window slides down the curve — the point).
EXTENSION_STEP_WEEKS = 1


# ---- Whipsaw circuit breaker (cumulative defend guard) ---------------------
# The defend engine's individual roll-downs are each correct, but the WHIPSAW —
# roll-down after roll-down in a slow grind, each locking a lower strike — is the
# strategy's real killer, and no single check owns it: the RS kill switch and the
# price circuit breaker can both stay untripped while defend bleeds the position
# weekly. This is the cumulative guard, computed from the roll ledger the app
# already derives: too many defensive rolls in a short window, OR cumulative roll
# drag past a fraction of the position's capital -> recommend EXIT, not another
# defend. HARD_CFM_RULE concept (whipsaw is the killer); the specific counts /
# percent are PROPOSED_DEFAULT pending the roll-ledger data that validates them.
WHIPSAW_DEFEND_ROLLS = 3       # defensive (reason="defend") rolls...
WHIPSAW_WINDOW_WEEKS = 4       # ...within this trailing window, OR
WHIPSAW_DRAG_PCT = 0.05        # cumulative roll drag > this fraction of position capital


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

# ---- Entry-context snapshots + coded exit reasons --------------------------
# Every closed cycle needs (a) the entry-time feature values that produced the
# GO verdict and (b) a machine-readable exit reason, or the calibration harness
# can never validate a threshold against it. Both are frozen onto the immutable
# executions at trade time (entry_context on the buy_leap, exit_reason on the
# close_leap) — they cannot be reconstructed after the fact. See
# backend/entry_context.py and backend/exit_reasons.py.

# HARD_CFM_RULE — the snapshot schema is versioned from day one, INDEPENDENT of
# the state.json schema_version, so the snapshot shape can evolve on its own
# cadence and old snapshots stay readable by their own version tag.
# v2: the regime section carries the full Genius four-light decision trace
# (lights, raw vote, dwell state, secondary breadth/VIX indicators, published
# regime) in ADDITION to the legacy status/breadth/vix fields. Older v1 snapshots
# stay valid — the new fields are purely additive.
# v3: the stock section records rs3m_vs_sector_method ("direct" — the RS3M-vs-
# sector figure that gated the entry is now the true rs3m(stock, sector_etf)
# ratio, not the vs-SPY difference approximation). Additive; v1/v2 stay valid.
SNAPSHOT_SCHEMA_VERSION = 3

# HARD_CFM_RULE — a trade must NEVER be blocked or delayed because telemetry
# capture failed. Snapshot capture is best-effort and wrapped so any failure
# degrades to null-with-reason; it never raises into the execution path.
SNAPSHOT_NEVER_BLOCKS_EXECUTION = True

# PROPOSED_DEFAULT — if more than this fraction of the snapshot's tracked
# scalar fields come back null (stale/unavailable/provider_error), fire a
# LOW-severity data-quality alert. This is worth-knowing-about, not a trade
# blocker (see SNAPSHOT_NEVER_BLOCKS_EXECUTION).
SNAPSHOT_NULL_FIELD_ALERT_FRACTION = 0.25

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

# ---- Position circuit breaker (the exit rule) ------------------------------
# HARD_CFM_RULE — a position is a hard EXIT on WHICHEVER of these trips first.
# backend/circuit_breaker.py is the single source of truth that evaluates them;
# the thresholds here are tunable, that the rule exists is not.
#   1. Drawdown  — the underlying has fallen CIRCUIT_BREAKER_DROP_PCT from the
#      price it was entered at.
#   2. Fast-MA break — CIRCUIT_BREAKER_MA_FAST_CLOSES consecutive daily closes
#      below the CIRCUIT_BREAKER_MA_FAST-day moving average.
#   3. Slow-MA break — a single close below the CIRCUIT_BREAKER_MA_SLOW-day MA.
CIRCUIT_BREAKER_DROP_PCT = 0.15        # 15% drop from the entry price
CIRCUIT_BREAKER_MA_FAST = 50           # fast MA window (days)
CIRCUIT_BREAKER_MA_FAST_CLOSES = 3     # consecutive closes below the fast MA (the "2-3" rule)
CIRCUIT_BREAKER_MA_SLOW = 200          # slow MA window; a single close below is a breach

# HARD_CFM_RULE (candidate — OFF by default, pending confirmation): block
# pullback share-accumulation on any ticker whose kill switch reads non-green
# (red = exit in progress, yellow = RS3M thinning toward the kill line). The
# accumulation play buys weakness, the kill switch sells it — without this
# guard the two rules can add to a name the strategy is 1-2 days from exiting.
# Flip to True to enforce; the Positions tab surfaces the block either way.
BLOCK_ACCUMULATION_ON_RS_DETERIORATION = False

# ---- Book concentration / correlation --------------------------------------
# MAX_POSITIONS_PER_SECTOR stops two names in the SAME sector, but two mega-caps
# in DIFFERENT sectors (e.g. a name in XLK and one in XLC) can still be ~0.9
# correlated — the 1/sector rule is satisfied while the book is really one bet.
# The portfolio-risk card already computes per-name beta and beta-adjusted delta;
# these bars turn that into a warning (portfolio_risk.concentration).
# PROPOSED_DEFAULT — trailing daily-return correlation above which two open
# underlyings are "too correlated to count as diversified".
CORRELATION_WARN_THRESHOLD = 0.80
CORRELATION_LOOKBACK = 60         # trailing sessions for the correlation estimate
# PROPOSED_DEFAULT — net SPY-beta-adjusted book delta as a multiple of deployed
# capital, above which the book is effectively one directional (index-beta) bet
# even if it's spread across sectors.
BETA_ADJ_LEVERAGE_WARN = 1.5

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

# ---- Recommendation trust layer (v2.6, state schema v17) -------------------
# The engine (recommendation_engine.py) commits to specific recommendations
# BEFORE the operator acts; recompute_derived() then measures agreement
# (coverage/precision/timeliness) and order-lifecycle fidelity. Automation
# eligibility ("graduation") is a derived, display-only readout — NO automated
# order submission exists anywhere in this version.

# PROPOSED_DEFAULT — how long an emitted recommendation stays matchable, by
# action type (hours from emission). A stale recommendation must never match a
# later action, so these are deliberately short: EXIT mirrors the kill switch's
# "within 1-2 days" (72h covers a weekend gap); rolls are same/next-day acts.
REC_VALID_HOURS = {
    "ENTER": 24,
    "ROLL_OUT": 72,
    "ROLL_DOWN": 48,
    "DEFEND": 48,
    "EXIT": 72,
    "NO_ACTION": 26,   # an all-clear covers roughly one trading day
}

# PROPOSED_DEFAULT — max adverse slippage vs the reference mid a proposed
# ticket tolerates, stamped on every proposed_ticket and graded by the
# SLIPPAGE_IN_BOUND fidelity check. Anchored to ASSUMED_SLIPPAGE_PCT so the
# fidelity bound and the net-juice haircut assumption can't silently diverge.
REC_MAX_SLIPPAGE_PCT_OF_MID = ASSUMED_SLIPPAGE_PCT

# PROPOSED_DEFAULT — a cancel that has not been confirmed terminal at the
# broker within this many minutes fails CANCEL_CONFIRMED_DEAD (the
# pending_cancel escape path relies on later polls; this is the deadline that
# turns "still waiting" into a graded defect).
FIDELITY_CANCEL_CONFIRM_STALE_MIN = 30

# PROPOSED_DEFAULT — coverage misses / fidelity failures older than this many
# days stop re-paging through the alert engine (they remain on the scoreboard
# forever; only the alert noise is windowed).
TRUST_ALERT_WINDOW_DAYS = 14

# ---- Graduation criteria (ALL PROPOSED_DEFAULT unless marked HARD) ---------
# Per action type, automation-eligible only when EVERY criterion holds over the
# trailing window. Display-only in this iteration: nothing is automated.
GRAD_MIN_LIVE_CYCLES = 10          # PROPOSED_DEFAULT — consecutive live instances
GRAD_MIN_WEEKS = {                 # PROPOSED_DEFAULT — trailing window length
    "ROLL_OUT": 8,
    "ROLL_DOWN": 16,
    "DEFEND": 16,
    "EXIT": 26,
    "ENTER": None,                 # ENTER is never auto-eligible in this iteration
    "NO_ACTION": None,             # not a gradable action
}
GRAD_MAX_OVERRIDE_RATE = 0.10      # PROPOSED_DEFAULT — plus zero unresolved DISAGREE_ACTION
# HARD requirements (not tunable, enforced in trust_derive.graduation):
#   - coverage misses in window == 0
#   - fidelity pass rate == 100% for the ticket type in window
#   - reconciliation green throughout the window; while post-fill
#     reconciliation is NOT_YET_IMPLEMENTED, no action type may graduate.

# ---- Consistency guards ----------------------------------------------------
# EOD_MAX_AGE_HOURS is written as a literal up in the tiered-scheduler section
# (DATA_STALE_HOURS isn't assigned yet at that point); assert they stay in sync so
# a future edit to one can't silently diverge from the other.
assert EOD_MAX_AGE_HOURS == DATA_STALE_HOURS, (
    "EOD_MAX_AGE_HOURS must mirror DATA_STALE_HOURS")
