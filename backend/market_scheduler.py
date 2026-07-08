"""Tiered market-data scheduler — the pure decision layer.

This module decides *who* gets fresh data and *when*. It contains no I/O: every
function is deterministic given its inputs and a clock, so it is fully unit-
testable with a mocked ``datetime`` and no provider calls. The transport layer
(actual Schwab / Alpha Vantage HTTP) lives in ``data_transport``; the two are
deliberately decoupled so a provider can be swapped per tier without touching
any logic here.

Three concerns, all pure:

  * ``assign_tiers`` — map every tracked symbol to a tier from position state +
    entry-queue rank. Promotion/demotion is automatic (a position opening lands
    a name in Tier 0; a slot opening + queue rank lands an on-deck name in Tier 1;
    passing the hard gates lands a watchlist name in Tier 2; everything else is
    Tier 3).
  * ``fetch_due`` — given a symbol's tier, the data kind, market state, and when
    it was last fetched, decide whether a fetch is due now. Quote cadence follows
    the tier (or the escalated cadence when a symbol is escalated); bars follow
    the once-daily EOD batch; chains are never fixed-schedule polled.
  * ``EscalationTracker`` — the escalation state machine. Defense escalations are
    per-Tier-0-symbol (price crossing a defense level); market escalations are
    global (SPY / a held sector ETF moving hard intraday). Both promote freshness
    to ``POLL_ESCALATED_SECONDS`` and emit an alert event, and both decay after
    ``ESCALATION_DECAY_MINUTES`` without a re-trigger.

Tier membership is driven by data, never hardcoded symbols. Where this codebase
has no ranked entry queue yet, Tier 1/2 are driven from the existing ready-to-
enter data behind the minimal ``QueueState`` interface (see ``queue_state.py``).
"""
from __future__ import annotations

import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config
import market_calendar

logger = logging.getLogger("cfm.scheduler")


def is_market_open(now: datetime) -> bool:
    """True during regular US equity trading hours on a trading day (holidays
    included, unlike the older weekday-only check). Pure — ``market_calendar`` is
    computation, not I/O."""
    if not market_calendar.is_trading_day(now.date()):
        return False
    return "09:30" <= now.strftime("%H:%M") < "16:00"


# ---- Tiers & data kinds ----------------------------------------------------

class Tier(enum.IntEnum):
    """Polling tiers, most-attention-first. IntEnum so tests can compare to 0..3
    and the values sort naturally (Tier 0 is the highest attention)."""
    T0 = 0  # open positions
    T1 = 1  # on-deck: top queue candidates when a slot is (or opens) available
    T2 = 2  # watchlist names passing the hard gates, no imminent slot
    T3 = 3  # remaining tracked universe + sector ETFs


# Data kinds a tier can request. Strings (not an enum) so callers/tests read
# naturally and JSON/logging is trivial.
QUOTE = "quote"
BARS = "bars"
CHAIN = "chain"


# ---- Plain-data interfaces (no I/O; built by adapters) ---------------------

@dataclass(frozen=True)
class QueueCandidate:
    """One ready-to-enter candidate. ``slot_opens_within_days`` is 0.0 when a book
    slot is free right now and ``float('inf')`` when none is in sight — the adapter
    fills it (this codebase has no close-forecast, so today it is 0/inf; the field
    is honoured as-is so real horizon data can drop in later without code changes)."""
    symbol: str
    rank: int
    gates_passed: bool
    slot_opens_within_days: float = float("inf")


@dataclass(frozen=True)
class QueueState:
    """The ranked entry queue, minimal interface. A flat, rank-ordered list of
    candidates — adapted from whatever ready-to-enter data exists."""
    candidates: tuple[QueueCandidate, ...] = ()


@dataclass(frozen=True)
class PortfolioState:
    """The plain inputs tier assignment needs — no state.json coupling, no I/O."""
    open_symbols: tuple[str, ...] = ()       # Tier 0
    tracked_universe: tuple[str, ...] = ()   # the screening universe (Tier 3 tail)
    sector_etfs: tuple[str, ...] = ()        # rotation ETFs (always at least Tier 3)


# ---- Escalation alert event (interface; sink is pluggable) -----------------

@dataclass(frozen=True)
class EscalationAlert:
    """The event emitted when a symbol's freshness is promoted. This is the
    stable interface to a future alerting engine — the default sink just logs it.

    ``kind`` is "defense" (per-symbol level cross) or "market" (global index move).
    """
    kind: str
    symbol: str | None
    level: str | None            # e.g. "short_strike" / "trailing_stop" / "consolidation_low" / SPY
    price: float
    level_value: float | None
    at: str                      # ISO ET timestamp
    detail: str


class LoggingAlertSink:
    """Default sink — logs the escalation. A real alerting engine implements the
    same one-method ``emit`` interface and is injected into the tracker."""

    def emit(self, alert: EscalationAlert) -> None:
        logger.warning("escalation alert: %s", alert.detail)


class ListAlertSink:
    """Captures alerts in a list — for the scheduler tick to drain and for tests."""

    def __init__(self) -> None:
        self.alerts: list[EscalationAlert] = []

    def emit(self, alert: EscalationAlert) -> None:
        self.alerts.append(alert)


# ---- Tier assignment (pure) ------------------------------------------------

def ondeck_symbols(queue: QueueState) -> list[str]:
    """The Tier-1 on-deck set: the top ``QUEUE_ONDECK_COUNT`` candidates that both
    pass the hard gates AND have a slot opening within ``SLOT_HORIZON_DAYS``,
    ordered by rank (lowest rank = strongest)."""
    eligible = [c for c in queue.candidates
                if c.gates_passed and c.slot_opens_within_days <= config.SLOT_HORIZON_DAYS]
    eligible.sort(key=lambda c: c.rank)
    seen: set[str] = set()
    out: list[str] = []
    for c in eligible:
        s = c.symbol.upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= config.QUEUE_ONDECK_COUNT:
            break
    return out


def assign_tiers(portfolio_state: PortfolioState, queue_state: QueueState,
                 clock: datetime | None = None) -> dict[str, Tier]:
    """Map every known symbol to a tier. Pure — consumes plain data only.

    Priority is highest-tier-wins: a name that is both an open position and a
    queue candidate stays Tier 0. ``clock`` is part of the interface (and reserved
    for future horizon hysteresis); the slot horizon is already encoded in each
    candidate's ``slot_opens_within_days`` so it is not needed here today.
    """
    _ = clock  # reserved; horizon is pre-encoded on the candidates
    tiers: dict[str, Tier] = {}

    def place(symbol: str | None, tier: Tier) -> None:
        s = (symbol or "").strip().upper()
        if s and s not in tiers:
            tiers[s] = tier

    # Tier 0 — open positions (highest attention; never overridden below).
    for s in portfolio_state.open_symbols:
        place(s, Tier.T0)

    # Tier 1 — on-deck queue candidates (a slot is/opens available).
    for s in ondeck_symbols(queue_state):
        place(s, Tier.T1)

    # Tier 2 — remaining candidates that pass the hard gates (no imminent slot).
    for c in queue_state.candidates:
        if c.gates_passed:
            place(c.symbol, Tier.T2)

    # Tier 3 — the remaining tracked universe + sector ETFs (cheap EOD data).
    for s in portfolio_state.tracked_universe:
        place(s, Tier.T3)
    for s in portfolio_state.sector_etfs:
        place(s, Tier.T3)

    return tiers


# ---- Cadence (pure) --------------------------------------------------------

def quote_poll_seconds(tier: Tier, escalated: bool) -> int | None:
    """The quote cadence for a tier, or None when the tier never quote-polls
    (Tier 2/3 ride EOD batch data). Escalation only lifts Tier 0/1 — the tiers
    that quote at all."""
    tier = Tier(tier)
    if escalated and tier in (Tier.T0, Tier.T1):
        return config.POLL_ESCALATED_SECONDS
    if tier == Tier.T0:
        return config.POLL_T0_SECONDS
    if tier == Tier.T1:
        return config.POLL_T1_SECONDS
    return None


def max_age_seconds(tier: Tier, data_kind: str) -> float:
    """Staleness ceiling for a (tier, data_kind), DERIVED from the poll cadence —
    a datum is stale once it is older than ``MAX_AGE_POLL_MULT`` × the interval
    that was meant to refresh it. EOD kinds (bars, chain) aren't intraday-polled,
    so they reuse the established daily-bar tolerance instead of a poll multiple."""
    tier = Tier(tier)
    if data_kind == QUOTE:
        base = quote_poll_seconds(tier, escalated=False)
        if base is None:  # T2/T3 have no live quote — treat like EOD data
            return config.EOD_MAX_AGE_HOURS * 3600.0
        return base * config.MAX_AGE_POLL_MULT
    # bars / chain / anything else: the EOD tolerance.
    return config.EOD_MAX_AGE_HOURS * 3600.0


def _elapsed_seconds(last_fetch_at: datetime | None, now: datetime) -> float:
    if last_fetch_at is None:
        return float("inf")
    return (now - last_fetch_at).total_seconds()


def _eod_batch_due(last_fetch_at: datetime | None, now: datetime) -> bool:
    """The once-daily EOD bar batch: due after ``EOD_BATCH_TIME_ET`` if it hasn't
    run since today's boundary. Fires exactly once per (trading) day — the caller
    restricts this to trading days; here it is purely time-of-day + last-fetch."""
    if now.strftime("%H:%M") < config.EOD_BATCH_TIME_ET:
        return False
    hh, mm = (int(x) for x in config.EOD_BATCH_TIME_ET.split(":"))
    boundary = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if last_fetch_at is None:
        return True
    return last_fetch_at < boundary


def fetch_due(symbol: str, tier: Tier, data_kind: str, market_open: bool,
              last_fetch_at: datetime | None, escalation_flags, clock: datetime) -> bool:
    """Is a fetch of ``data_kind`` for ``symbol`` (in ``tier``) due at ``clock``?

    Fully deterministic. ``escalation_flags`` may be a bool, a set/list of
    escalated symbols, a mapping ``{symbol: bool}``, or an object exposing
    ``is_escalated(symbol)`` (e.g. an ``EscalationTracker`` snapshot).

    Rules:
      * quotes — Tier 0/1 only, on the tier (or escalated) cadence, and only while
        the market is open (off-hours quote polling drops to zero);
      * bars   — the once-daily EOD batch (``_eod_batch_due``), independent of
        market_open (it runs after the close);
      * chains — never fixed-schedule polled (on-demand + escalation events only).
    """
    tier = Tier(tier)
    if data_kind == QUOTE:
        if not market_open:
            return False
        interval = quote_poll_seconds(tier, _is_escalated(escalation_flags, symbol))
        if interval is None:
            return False
        return _elapsed_seconds(last_fetch_at, clock) >= interval
    if data_kind == BARS:
        return _eod_batch_due(last_fetch_at, clock)
    # CHAIN and any other kind: never scheduled here.
    return False


def _is_escalated(escalation_flags, symbol: str) -> bool:
    if not escalation_flags:
        return False
    if escalation_flags is True:
        return True
    sym = (symbol or "").upper()
    if isinstance(escalation_flags, Mapping):
        return bool(escalation_flags.get(sym))
    if isinstance(escalation_flags, (set, frozenset, list, tuple)):
        return sym in {str(s).upper() for s in escalation_flags}
    is_esc = getattr(escalation_flags, "is_escalated", None)
    if callable(is_esc):
        return bool(is_esc(sym))
    return False


# ---- Escalation math (pure) ------------------------------------------------

def breached_defense_levels(levels: Mapping[str, float | None], price: float) -> list[str]:
    """The downside defense levels the price is at or below (breached), sorted by
    name. All defense levels are downside protection (short-call strike, trailing
    stop, consolidation low, circuit-breaker line) — price falling to/through one
    is the danger. Levels of ``None`` (not derivable) are skipped."""
    out = []
    for name, lvl in levels.items():
        if lvl is not None and price <= float(lvl):
            out.append(name)
    return sorted(out)


def index_move_pct(ref_price: float | None, cur_price: float | None) -> float:
    """Intraday % move of an index/ETF vs its reference (prior close). 0.0 when
    either input is missing so a missing quote never fabricates an escalation."""
    if not ref_price or cur_price is None:
        return 0.0
    return (float(cur_price) - float(ref_price)) / float(ref_price) * 100.0


def market_escalation_triggered(moves: Mapping[str, float]) -> bool:
    """True when any tracked index/sector move (percent) meets the market-
    escalation threshold in absolute terms."""
    return any(abs(p) >= config.ESCALATION_INDEX_MOVE_PCT for p in moves.values())


# ---- Escalation state machine ----------------------------------------------

@dataclass
class EscalationTracker:
    """Tracks defense (per-symbol) and market (global) escalations over time.

    Deterministic given the clock passed to each call — no wall-clock reads, so it
    is testable with a mocked ``now``. Alerts are emitted on the *rising edge* of a
    breach (so a level held below doesn't re-alert every tick), while the cadence
    promotion decays ``ESCALATION_DECAY_MINUTES`` after the last re-trigger.
    """
    sink: object = field(default_factory=LoggingAlertSink)
    _defense_expiry: dict[str, datetime] = field(default_factory=dict)
    _active_breach: dict[str, set[str]] = field(default_factory=dict)
    _market_expiry: datetime | None = None
    _market_reason: str | None = None

    def _decay(self) -> timedelta:
        return timedelta(minutes=config.ESCALATION_DECAY_MINUTES)

    # -- defense (per Tier-0 symbol) --
    def observe_defense(self, symbol: str, levels: Mapping[str, float | None],
                        price: float, now: datetime) -> list[EscalationAlert]:
        """Check a symbol's price against its defense levels. (Re)arms the cadence
        escalation on any active breach and emits one alert per NEWLY-breached
        level. Returns the alerts emitted this call."""
        sym = (symbol or "").upper()
        breached = set(breached_defense_levels(levels, price))
        previously = self._active_breach.get(sym, set())
        newly = breached - previously
        if breached:
            self._active_breach[sym] = breached
            self._defense_expiry[sym] = now + self._decay()  # re-arm on every active breach
        else:
            self._active_breach.pop(sym, None)
        alerts: list[EscalationAlert] = []
        for name in sorted(newly):
            lvl = levels.get(name)
            alert = EscalationAlert(
                kind="defense", symbol=sym, level=name, price=float(price),
                level_value=(float(lvl) if lvl is not None else None), at=_iso(now),
                detail=f"{sym} {price:.2f} breached {name}"
                       f"{f' {float(lvl):.2f}' if lvl is not None else ''}")
            self.sink.emit(alert)
            alerts.append(alert)
        return alerts

    # -- market (global) --
    def observe_market(self, moves: Mapping[str, float], now: datetime) -> EscalationAlert | None:
        """Check SPY / held-sector intraday moves. On a fresh trigger, arms the
        global escalation and emits ONE alert; while it stays triggered it re-arms
        (extends decay) silently."""
        if not market_escalation_triggered(moves):
            return None
        was_active = self.market_active(now)
        worst_sym, worst_pct = max(moves.items(), key=lambda kv: abs(kv[1]))
        self._market_expiry = now + self._decay()
        self._market_reason = worst_sym
        if was_active:
            return None  # already escalated — extend decay, don't re-alert
        alert = EscalationAlert(
            kind="market", symbol=None, level=worst_sym, price=float(worst_pct),
            level_value=config.ESCALATION_INDEX_MOVE_PCT, at=_iso(now),
            detail=f"market escalation: {worst_sym} moved {worst_pct:+.2f}% intraday")
        self.sink.emit(alert)
        return alert

    # -- queries --
    def market_active(self, now: datetime) -> bool:
        return self._market_expiry is not None and now < self._market_expiry

    def is_escalated(self, symbol: str, now: datetime | None = None) -> bool:
        """True when ``symbol``'s freshness is promoted right now — either its own
        defense escalation or a global market escalation is active. ``now`` may be
        omitted when the tracker is used as a plain flags snapshot for ``fetch_due``
        (in which case any recorded escalation counts)."""
        sym = (symbol or "").upper()
        if now is None:  # snapshot semantics
            return self._market_expiry is not None or sym in self._defense_expiry
        if self.market_active(now):
            return True
        exp = self._defense_expiry.get(sym)
        return exp is not None and exp > now

    def escalated_symbols(self, now: datetime) -> frozenset[str]:
        """The set of symbols with an ACTIVE defense escalation at ``now`` (market
        escalation is global and applies to whatever is being polled, so it is not
        enumerated here)."""
        return frozenset(s for s, exp in self._defense_expiry.items() if exp > now)

    def prune(self, now: datetime) -> None:
        """Drop expired escalations. Optional housekeeping — queries already treat
        an expired escalation as inactive; pruning just keeps the dicts small."""
        for s in [s for s, exp in self._defense_expiry.items() if exp <= now]:
            self._defense_expiry.pop(s, None)
            self._active_breach.pop(s, None)
        if self._market_expiry is not None and now >= self._market_expiry:
            self._market_expiry = None
            self._market_reason = None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")
