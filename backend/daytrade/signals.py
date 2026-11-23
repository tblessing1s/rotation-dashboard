"""Day-trade signal engine — strategy rules 3-8.

``run_day(day, account_id, ...)`` replays one trading day's ingested 5-min
bars (``daytrade/bars.py``, SHARED across every account) against today's
screener picks (``daytrade/universe.py``, also shared) through a small
per-symbol state machine, and journals every signal ONE ACCOUNT's strategy
run produces — taken or not, with outcome (rule 8) — to
``daytrade/store.py``'s signals log for that account. Two accounts with
day-trading enabled (``daytrade/settings.py``) replay the SAME bars
independently, each against its own budget/guardrails/trial, and can reach
different decisions on the same symbol (different budget -> different size
-> a guardrail trips for one and not the other).

STATE MACHINE (per symbol, independent of the other picks):

    watching --[setup, rule 3]--> armed --[break, rule 4]--> in_trade --> watching
       ^                            |                            |
       |                       [2 candles, no break]        [stop / target / cutoff]
       +----------------------------+----------------------------+

A symbol returns to ``watching`` after a trade resolves or a setup expires —
the trades/day cap (rule 7, ``config.DAYTRADE_MAX_TRADES_PER_DAY``) is an
ACCOUNT-WIDE cap (one account's own day) enforced by the shared ``_Day``
guardrail state, not a one-trade-per-symbol
limit.

THIS IS A REPLAY, NOT A LIVE STREAM: there is no persisted mid-day machine
state as its own object — instead, each call to ``run_day`` first
RECONSTRUCTS that state (which symbols are armed/in_trade, with what
entry/stop/target, plus the day's trades-taken/losses/cumulative-R) from
what's already journaled for this account+day (see ``_rehydrate``), then
only evaluates bars strictly after the last one already reflected there.
This matters because ``account_equity`` and ``entries_enabled`` are LIVE
inputs that can change between two calls on the SAME day (dry powder moves,
the trial completes mid-day) — without rehydration, a later call's
from-scratch replay of an EARLIER bar could reach a different decision than
the one already journaled for it (a budget that dried up would relabel an
already-"entry" bar "entry_skipped", silently orphaning that open trade
forever). Rehydration confines the current gate to genuinely NEW decisions
only, never a bar a past run already committed to. New events are still
diffed against what's already journaled by ``id`` before appending, so a
call that finds nothing new to do is a cheap no-op.

FILLS GO THROUGH ``daytrade/adapters.py`` (Phase 3): every entry/exit this
engine decides on is a REQUEST to an ``ExecutionAdapter`` (default
``PaperAdapter``, resolved per day via ``adapters.get_adapter``), not an
assumption. ``PaperAdapter`` still fills instantly and exactly at the
requested price — no slippage/partial-fill model yet, that's
``SchwabAdapter``'s problem, a later phase — but the seam is real: this
engine only ever uses the returned ``Fill``, never the request, for the
price/size it books. The adapter is also the day's trade log; see its
module docstring for how that differs from this module's signals journal.

REPLAY-SAFE ADAPTER CALLS: rehydration means a bar already reflected in the
journal is no longer re-processed at all in a later run, so its adapter call
isn't normally repeated either — but ``enter``/``exit`` are ALSO idempotent
on the adapter side regardless (keyed by trade_id and by trade_id+kind+at
respectively), as a second line of defense against a partial write (events
journaled but the adapter's own trade log didn't finish, or vice versa):
calling them again for an already-recorded fill is a no-op, not a
double-fill or a double-counted P&L. See ``PaperAdapter`` for the dedup.

RULE 3's "average 5-min volume" baseline is a Phase-2 interpretation call —
see ``_avg_prior_volume`` and the config.py comment above
``DAYTRADE_SETUP_VOLUME_MULT``: there is no historical intraday archive yet,
so it is the running average of the symbol's OWN bars ingested so far that
day. The first bar of the day therefore never qualifies as a setup (no
baseline to compare against) — a known, logged limitation, not a silent gap.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config

from daytrade import adapters, store

logger = logging.getLogger("cfm.daytrade")

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _parse_at(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _avg_prior_volume(seen_volumes: list[float]) -> float | None:
    """RUNNING average of a symbol's own bars so far today (rule 3's
    baseline — see the module docstring). None with no prior bars."""
    if not seen_volumes:
        return None
    return sum(seen_volumes) / len(seen_volumes)


def _setup_direction(bar: dict, prior_high: float, prior_low: float,
                      avg_volume: float | None) -> str | None:
    if avg_volume is None or avg_volume <= 0:
        return None
    if bar["volume"] < avg_volume * config.DAYTRADE_SETUP_VOLUME_MULT:
        return None
    if bar["close"] > prior_high:
        return "long"
    if bar["close"] < prior_low:
        return "short"
    return None


def _triggered(bar: dict, direction: str, setup_high: float, setup_low: float) -> bool:
    return bar["high"] >= setup_high if direction == "long" else bar["low"] <= setup_low


def _minutes_to_cutoff(at: str) -> float | None:
    """Minutes from a bar's timestamp to the time cutoff (DAYTRADE_WINDOW_END_ET
    on that bar's own date and UTC offset — when open positions are
    flattened). None if the timestamp can't be parsed."""
    try:
        when = _parse_at(at)
    except (TypeError, ValueError):
        return None
    h, m = (int(x) for x in config.DAYTRADE_WINDOW_END_ET.split(":"))
    cutoff = when.replace(hour=h, minute=m, second=0, microsecond=0)
    return round((cutoff - when).total_seconds() / 60, 1)


def _entry_window_rejection(at: str) -> tuple[str, str] | None:
    """Round 2, Change 1: ``(reason_code, reason)`` if a setup or entry at
    ``at`` is too late in the window, else None. ``at or after
    DAYTRADE_LAST_ENTRY_ET`` -> entry_window_closed; fewer than
    DAYTRADE_MIN_MINUTES_TO_CUTOFF minutes before the cutoff ->
    insufficient_time."""
    try:
        hm = _parse_at(at).strftime("%H:%M")
    except (TypeError, ValueError):
        return None
    if hm >= config.DAYTRADE_LAST_ENTRY_ET:
        return ("entry_window_closed",
                f"at/after last entry time {config.DAYTRADE_LAST_ENTRY_ET} ET")
    left = _minutes_to_cutoff(at)
    if left is not None and left < config.DAYTRADE_MIN_MINUTES_TO_CUTOFF:
        return ("insufficient_time",
                f"{left:g} min to cutoff < {config.DAYTRADE_MIN_MINUTES_TO_CUTOFF} min")
    return None


# Machine-readable codes for the human-readable skip reasons, so the
# rejected-setups log (store.append_rejected) has a stable reason column.
_REASON_CODES = {
    "paper trial complete — no new entries": "trial_complete",
    "max trades/day reached": "max_trades_per_day",
    "two losing trades": "max_losses_per_day",
    "no ATR14 on file": "no_atr14",
    "no budget available": "no_budget",
    "daily capital limit reached": "daily_capital_limit",
}


def _reason_code(reason: str | None) -> str:
    if not reason:
        return "unknown"
    if reason in _REASON_CODES:
        return _REASON_CODES[reason]
    if reason.endswith("R reached"):
        return "daily_stop_r"
    return reason


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class _Symbol:
    __slots__ = ("prior_high", "prior_low", "atr14", "risk_per_share", "seen_volumes",
                 "status", "direction", "setup_high", "setup_low", "setup_at",
                 "candles_waited", "entry", "stop", "target1", "target2", "half_taken",
                 "size", "entry_at", "trade_id", "last_bar", "hwm", "tight", "half_at")

    def __init__(self, prior_high: float, prior_low: float, atr14: float | None):
        self.prior_high = prior_high
        self.prior_low = prior_low
        self.atr14 = atr14
        self.risk_per_share = (atr14 / config.DAYTRADE_STOP_ATR_DIVISOR) if atr14 else None
        self.seen_volumes: list[float] = []
        self.status = "watching"
        self.direction = None
        self.setup_high = self.setup_low = self.setup_at = None
        self.candles_waited = 0
        self.entry = self.stop = self.target1 = self.target2 = None
        self.half_taken = False
        self.hwm = None       # best price since the half target (trailing stop anchor)
        self.tight = False    # trail tightened after reaching DAYTRADE_FULL_TARGET_R
        self.half_at = None
        self.size = 0
        self.entry_at = self.trade_id = None
        self.last_bar: dict | None = None


class _Day:
    """Account-wide guardrail state (rule 7) shared across every symbol, plus
    the cross-day paper-trading trial gate (daytrade/trial.py)."""

    def __init__(self, account_equity: float, entries_enabled: bool = True):
        self.account_equity = account_equity
        self.entries_enabled = entries_enabled
        self.trades_taken = 0
        self.deployed = 0.0  # cumulative entry notional today (daily deploy cap)
        self.losses = 0
        self.cumulative_r = 0.0
        self.stopped_reason: str | None = None

    def block_reason(self) -> str | None:
        if not self.entries_enabled:
            # The trial (daytrade/trial.py) has already hit its target trade
            # count — a cross-day gate, not this day's own guardrails, so it
            # is checked first and never cleared by anything below.
            return "paper trial complete — no new entries"
        if self.stopped_reason:
            return self.stopped_reason
        if self.trades_taken >= config.DAYTRADE_MAX_TRADES_PER_DAY:
            return "max trades/day reached"
        return None

    def record_trade_result(self, net_r: float) -> None:
        self.cumulative_r += net_r
        if net_r < 0:
            self.losses += 1
        if self.losses >= config.DAYTRADE_MAX_LOSSES_PER_DAY:
            self.stopped_reason = "two losing trades"
        elif self.cumulative_r >= config.DAYTRADE_DAILY_STOP_R:
            self.stopped_reason = f"+{config.DAYTRADE_DAILY_STOP_R:g}R reached"


def _event(day: str, symbol: str, event: str, at: str, **extra) -> dict:
    row = {"schema_version": store.SCHEMA_VERSION, "date": day, "symbol": symbol,
           "event": event, "at": at, "id": f"{symbol}:{event}:{at}"}
    row.update(extra)
    return row


def _enter_trade(sym: _Symbol, day_state: _Day, bar: dict,
                  adapter: adapters.ExecutionAdapter) -> list[dict]:
    """Commit a triggered setup as a live trade. Returns the events to
    journal: ``entry`` (plus ``size_capped`` if the notional safety cap
    bound), or a single ``entry_skipped`` — leaving the symbol able to
    re-arm — if a guardrail, the entry window (Round 2 Change 1) or sizing
    (Change 3) blocks the entry at trigger time. Guardrails are re-checked
    here for correctness, e.g. another symbol used the last daily slot in
    between."""
    def _skip(reason: str, code: str | None = None, **extra) -> list[dict]:
        sym.status = "watching"
        return [_event(bar["date"], bar["symbol"], "entry_skipped", bar["datetime"],
                       direction=sym.direction, reason=reason,
                       reason_code=code or _reason_code(reason), trade_id=sym.trade_id, **extra)]

    reason = day_state.block_reason()
    if reason:
        return _skip(reason)
    late = _entry_window_rejection(bar["datetime"])
    if late:
        return _skip(late[1], late[0])
    requested_entry = sym.setup_high if sym.direction == "long" else sym.setup_low
    risk_per_share = sym.risk_per_share
    sizing = _size_trade(risk_per_share, requested_entry, day_state)
    if sizing.get("skip"):
        code, why = sizing["skip"]
        return _skip(why, code, requested_size=sizing.get("size"))
    requested_size = sizing["size"]

    minutes_left = _minutes_to_cutoff(bar["datetime"])
    fill = adapter.enter(symbol=bar["symbol"], trade_id=sym.trade_id, direction=sym.direction,
                         price=requested_entry, size=requested_size, at=bar["datetime"])
    entry, size = fill.price, fill.size
    day_state.deployed += entry * size
    stop = entry - risk_per_share if sym.direction == "long" else entry + risk_per_share
    target1 = entry + risk_per_share if sym.direction == "long" else entry - risk_per_share
    target2 = (entry + 2 * risk_per_share if sym.direction == "long"
               else entry - 2 * risk_per_share)
    risk_usd = round(risk_per_share * size, 2)
    # Round 2, Change 4: the trade log carries the trade's own risk geometry
    # so 1R never has to be backed out of P&L / R again.
    adapter.annotate(trade_id=sym.trade_id, stop_price=round(stop, 4),
                     target_price=round(target1, 4), full_target_price=round(target2, 4),
                     risk_per_share=round(risk_per_share, 4), risk_usd=risk_usd,
                     minutes_to_cutoff_at_entry=minutes_left,
                     sizing_mode=config.DAYTRADE_SIZING_MODE,
                     round=config.DAYTRADE_RULES_ROUND)

    sym.status = "in_trade"
    sym.entry, sym.stop, sym.target1, sym.target2 = entry, stop, target1, target2
    sym.half_taken = False
    sym.hwm, sym.tight, sym.half_at = None, False, None
    sym.size = size
    sym.entry_at = fill.at
    sym.last_bar = bar  # so a trade entered on the day's last bar still has
                        # something to close out against if finalize() runs
    day_state.trades_taken += 1
    events = [_event(bar["date"], bar["symbol"], "entry", bar["datetime"], direction=sym.direction,
                     entry=round(entry, 4), stop=round(stop, 4), target1=round(target1, 4),
                     target2=round(target2, 4), size=size, trade_id=sym.trade_id,
                     risk_usd=risk_usd, minutes_to_cutoff=minutes_left)]
    if sizing.get("capped"):
        # Change 3: the notional safety cap bound — the trade still goes, but
        # at a smaller dollar risk than the rule asked for. Say so.
        events.append(_event(bar["date"], bar["symbol"], "size_capped", bar["datetime"],
                             direction=sym.direction, trade_id=sym.trade_id, size=size,
                             uncapped_size=sizing["uncapped_size"],
                             risk_usd=risk_usd, risk_usd_target=sizing["risk_usd_target"],
                             max_notional=config.DAYTRADE_MAX_NOTIONAL_PER_TRADE_USD))
    return events


def _size_trade(risk_per_share: float, entry: float, day_state: _Day) -> dict:
    """Share count for a new entry (rule 7 / Round 2 Change 3).

    ``fixed_usd`` (default): ``floor(DAYTRADE_RISK_PER_TRADE_USD / risk_per_
    share)``, capped only by DAYTRADE_MAX_NOTIONAL_PER_TRADE_USD — and a cap
    that binds is reported (``capped``), never silent. ``pct_equity``: the
    pre-Round-2 sizing — DAYTRADE_RISK_PCT of the live budget, capped by
    DAYTRADE_MAX_POSITION_PCT per trade and DAYTRADE_MAX_DAILY_DEPLOY_PCT
    per day. Either way, fewer than DAYTRADE_MIN_SHARES shares is a skip.

    Returns ``{"size", "capped", "uncapped_size", "risk_usd_target"}`` or
    ``{"skip": (reason_code, reason), "size"}``."""
    if risk_per_share is None or risk_per_share <= 0 or entry <= 0:
        return {"skip": ("no_budget", "no budget available"), "size": 0}
    if config.DAYTRADE_SIZING_MODE == "pct_equity":
        risk_amount = day_state.account_equity * (config.DAYTRADE_RISK_PCT / 100.0)
        risk_based = int(risk_amount // risk_per_share)
        notional_cap = day_state.account_equity * (config.DAYTRADE_MAX_POSITION_PCT / 100.0)
        notional_based = int(notional_cap // entry)
        daily_room = (day_state.account_equity * (config.DAYTRADE_MAX_DAILY_DEPLOY_PCT / 100.0)
                      - day_state.deployed)
        daily_based = int(daily_room // entry) if daily_room > 0 else 0
        size = min(risk_based, notional_based, daily_based)
        if size <= 0:
            # No room: the budget is ~0, or 1.5% of it doesn't buy one share
            # at this price, or the day's deploy cap is used up. A size-0
            # "trade" would still burn one of the day's slots.
            why = ("daily capital limit reached" if daily_based <= 0 and notional_based > 0
                   and risk_based > 0 else "no budget available")
            return {"skip": (_reason_code(why), why), "size": 0}
        out = {"size": size, "capped": False, "uncapped_size": risk_based,
               "risk_usd_target": round(risk_amount, 2)}
    else:
        target = config.DAYTRADE_RISK_PER_TRADE_USD
        uncapped = int(target // risk_per_share)
        cap = int(config.DAYTRADE_MAX_NOTIONAL_PER_TRADE_USD // entry)
        size = min(uncapped, cap)
        out = {"size": size, "capped": cap < uncapped, "uncapped_size": uncapped,
               "risk_usd_target": round(target, 2)}
    if size < config.DAYTRADE_MIN_SHARES:
        return {"skip": ("size_too_small",
                         f"{size} share(s) < min {config.DAYTRADE_MIN_SHARES}"), "size": size}
    return out


def _exit_fill(adapter: adapters.ExecutionAdapter, sym: _Symbol, bar: dict, kind: str,
                price: float, size: int, r: float) -> float:
    """Request an exit fill and return the price actually booked."""
    fill = adapter.exit(symbol=bar["symbol"], trade_id=sym.trade_id, kind=kind,
                        price=price, size=size, at=bar["datetime"], r=r)
    return fill.price


def _advance_trail(sym: _Symbol, bar: dict) -> bool:
    """Fold one bar into the remainder's trailing stop: track the best price
    since the half target, tighten the trail once price has reached
    DAYTRADE_FULL_TARGET_R, and ratchet the stop (never loosen it, never below
    breakeven). Returns True on the bar that FIRST tightens the trail."""
    long = sym.direction == "long"
    extreme = bar["high"] if long else bar["low"]
    if sym.hwm is None:
        sym.hwm = extreme
    else:
        sym.hwm = max(sym.hwm, extreme) if long else min(sym.hwm, extreme)
    risk = sym.risk_per_share
    gain_r = ((sym.hwm - sym.entry) if long else (sym.entry - sym.hwm)) / risk
    newly = not sym.tight and gain_r >= config.DAYTRADE_FULL_TARGET_R
    if newly:
        sym.tight = True
    dist = (config.DAYTRADE_TRAIL_TIGHT_R if sym.tight else config.DAYTRADE_TRAIL_LOOSE_R) * risk
    cand = sym.hwm - dist if long else sym.hwm + dist
    sym.stop = max(sym.stop, cand, sym.entry) if long else min(sym.stop, cand, sym.entry)
    return newly


def _resolve_trade(sym: _Symbol, day_state: _Day, bar: dict,
                    adapter: adapters.ExecutionAdapter) -> list[dict]:
    events: list[dict] = []
    long = sym.direction == "long"
    half_size = sym.size // 2
    remainder_size = sym.size - half_size

    if not sym.half_taken:
        stop_hit = bar["low"] <= sym.stop if long else bar["high"] >= sym.stop
        if stop_hit:
            net_r = -1.0
            price = _exit_fill(adapter, sym, bar, "stop_out", sym.stop, sym.size, net_r)
            day_state.record_trade_result(net_r)
            events.append(_event(bar["date"], bar["symbol"], "stop_out", bar["datetime"],
                                  direction=sym.direction, price=round(price, 4),
                                  r=net_r, trade_id=sym.trade_id))
            sym.status = "watching"
            return events
        target1_hit = bar["high"] >= sym.target1 if long else bar["low"] <= sym.target1
        if target1_hit:
            price = _exit_fill(adapter, sym, bar, "half_target", sym.target1, half_size,
                               config.DAYTRADE_HALF_TARGET_R)
            sym.half_taken = True
            sym.half_at = bar["datetime"]
            sym.stop = sym.entry  # breakeven floor, rule 6 — the trail only ratchets up from here
            events.append(_event(bar["date"], bar["symbol"], "half_target", bar["datetime"],
                                  direction=sym.direction, price=round(price, 4),
                                  r=config.DAYTRADE_HALF_TARGET_R, stop=round(sym.stop, 4),
                                  trade_id=sym.trade_id))
            # fall through: the same bar can also resolve the remainder below
        else:
            sym.last_bar = bar
            return events

    # Remainder (half banked at DAYTRADE_HALF_TARGET_R): a trailing stop, no
    # fixed target. The stop tested is the one set by PRIOR bars (a bar's own
    # new high can't both raise the stop and be stopped out by it).
    stop_hit = bar["low"] <= sym.stop if long else bar["high"] >= sym.stop
    if stop_hit:
        # A gap through the stop fills at the open, not the stop.
        fill_at = min(sym.stop, bar["open"]) if long else max(sym.stop, bar["open"])
        remainder_r = ((fill_at - sym.entry) if long else (sym.entry - fill_at)) / sym.risk_per_share
        net_r = round(0.5 * config.DAYTRADE_HALF_TARGET_R + 0.5 * remainder_r, 4)
        kind = "trail_stop" if sym.stop != sym.entry else "breakeven_exit"
        price = _exit_fill(adapter, sym, bar, kind, fill_at, remainder_size, net_r)
        day_state.record_trade_result(net_r)
        events.append(_event(bar["date"], bar["symbol"], kind, bar["datetime"],
                              direction=sym.direction, price=round(price, 4),
                              r=net_r, trade_id=sym.trade_id))
        sym.status = "watching"
        return events
    if _advance_trail(sym, bar):
        events.append(_event(bar["date"], bar["symbol"], "trail_tighten", bar["datetime"],
                              direction=sym.direction, stop=round(sym.stop, 4),
                              trail_r=config.DAYTRADE_TRAIL_TIGHT_R, trade_id=sym.trade_id))
    sym.last_bar = bar
    return events


def _process_bar(sym: _Symbol, day_state: _Day, bar: dict, day: str,
                  adapter: adapters.ExecutionAdapter) -> list[dict]:
    events: list[dict] = []
    avg_volume = _avg_prior_volume(sym.seen_volumes)

    if sym.status == "watching":
        direction = _setup_direction(bar, sym.prior_high, sym.prior_low, avg_volume)
        if direction:
            setup_id = f"{bar['symbol']}:setup:{bar['datetime']}"
            reason = day_state.block_reason()
            late = _entry_window_rejection(bar["datetime"])
            if reason:
                events.append(_event(day, bar["symbol"], "setup_skipped", bar["datetime"],
                                      direction=direction, reason=reason,
                                      reason_code=_reason_code(reason)))
            elif late:
                # Round 2, Change 1: an entry from this setup could only
                # trigger later still, so don't arm it at all.
                events.append(_event(day, bar["symbol"], "setup_skipped", bar["datetime"],
                                      direction=direction, reason=late[1], reason_code=late[0]))
            elif sym.risk_per_share is None or sym.risk_per_share <= 0:
                events.append(_event(day, bar["symbol"], "setup_skipped", bar["datetime"],
                                      direction=direction, reason="no ATR14 on file",
                                      reason_code="no_atr14"))
            else:
                sym.status = "armed"
                sym.direction = direction
                sym.setup_high, sym.setup_low = bar["high"], bar["low"]
                sym.setup_at = bar["datetime"]
                sym.trade_id = setup_id
                sym.candles_waited = 0
                events.append(_event(day, bar["symbol"], "setup", bar["datetime"],
                                      direction=direction, high=bar["high"], low=bar["low"],
                                      volume=bar["volume"], avg_volume=round(avg_volume, 1)))
    elif sym.status == "armed":
        sym.candles_waited += 1
        if _triggered(bar, sym.direction, sym.setup_high, sym.setup_low):
            events.extend(_enter_trade(sym, day_state, bar, adapter))
        elif sym.candles_waited >= config.DAYTRADE_ENTRY_EXPIRY_CANDLES:
            events.append(_event(day, bar["symbol"], "expired", bar["datetime"],
                                  direction=sym.direction, trade_id=sym.trade_id))
            sym.status = "watching"
    elif sym.status == "in_trade":
        events.extend(_resolve_trade(sym, day_state, bar, adapter))

    sym.seen_volumes.append(bar["volume"])
    return events


def _finalize_open_trades(symbols: dict[str, _Symbol], day_state: _Day, day: str,
                           adapter: adapters.ExecutionAdapter,
                           latest: dict[str, dict] | None = None) -> list[dict]:
    """Force-exit EVERY still-open trade at the window cutoff (rule 6), using
    the last bar seen for that symbol as the cutoff price. Only called once
    the caller has established the window has actually ended — a trade still
    open mid-window is left alone (unresolved) for the next replay to pick
    up once a later bar lands.

    Round 2, Change 2: a trade rehydrated from the journal (e.g. one entered
    on the cutoff bar itself by an earlier in-window run) has no ``last_bar``
    of its own in THIS run, because every bar up to its entry is skipped as
    already reflected. It used to be silently passed over here and left open
    forever (EWZ/GDX/NKE in Round 1). ``latest`` — the day's latest bar per
    symbol — is now the fallback, so no open trade is skipped."""
    events: list[dict] = []
    latest = latest or {}
    for symbol, sym in symbols.items():
        if sym.status != "in_trade":
            continue
        last_bar = sym.last_bar or latest.get(symbol)
        if last_bar is None or not sym.risk_per_share:
            continue  # nothing to price it against — _sweep_open_trades catches it
        long = sym.direction == "long"
        close = last_bar["close"]
        size = sym.size - sym.size // 2 if sym.half_taken else sym.size
        if sym.half_taken:
            remainder_r = ((close - sym.entry) if long else (sym.entry - close)) / sym.risk_per_share
            net_r = 0.5 * config.DAYTRADE_HALF_TARGET_R + 0.5 * remainder_r
        else:
            net_r = ((close - sym.entry) if long else (sym.entry - close)) / sym.risk_per_share
        price = _exit_fill(adapter, sym, last_bar, "time_cutoff", close, size, round(net_r, 4))
        day_state.record_trade_result(net_r)
        events.append(_event(day, symbol, "time_cutoff", last_bar["datetime"],
                              direction=sym.direction, price=round(price, 4),
                              half_taken=sym.half_taken, r=round(net_r, 4), trade_id=sym.trade_id))
        sym.status = "watching"
    return events


def _sweep_open_trades(day: str, adapter: adapters.ExecutionAdapter,
                       latest: dict[str, dict], journal: list[dict], now: datetime) -> list[dict]:
    """End-of-session safety net (Round 2, Change 2): force-close any trade
    the TRADE LOG still has open after the cutoff — one the engine's own
    state lost track of — with exit kind ``eod_sweep``, priced at that
    symbol's last bar close (else its entry price, i.e. flat), and log an
    ERROR: reaching this means a cutoff exit was missed. R uses the trade's
    stored risk_per_share, else the journaled entry's stop distance, else 0."""
    events: list[dict] = []
    entries = {e.get("trade_id"): e for e in journal if e.get("event") == "entry"}
    for trade in adapter.open_trades():
        trade_id, symbol = trade["trade_id"], trade["symbol"]
        long = trade["direction"] == "long"
        entry_price = trade["entry"]["price"]
        exits = trade.get("exits") or []
        remaining = trade["entry"]["size"] - sum(x.get("size") or 0 for x in exits)
        bar = latest.get(symbol)
        price = bar["close"] if bar else entry_price
        at = bar["datetime"] if bar else now.isoformat(timespec="seconds")
        rps = trade.get("risk_per_share")
        if not rps:
            ent = entries.get(trade_id)
            if ent and ent.get("entry") is not None and ent.get("stop") is not None:
                rps = abs(ent["entry"] - ent["stop"])
        move = (price - entry_price) if long else (entry_price - price)
        rem_r = move / rps if rps else 0.0
        half = any(x.get("kind") == "half_target" for x in exits)
        net_r = round(0.5 * config.DAYTRADE_HALF_TARGET_R + 0.5 * rem_r if half else rem_r, 4)
        logger.error("daytrade EOD sweep: %s %s (%s) still open after the %s ET cutoff on %s "
                     "— force-closing %d share(s) @ %.4f (%+.2fR)", symbol, trade_id,
                     trade["direction"], config.DAYTRADE_WINDOW_END_ET, day, remaining, price, net_r)
        fill = adapter.exit(symbol=symbol, trade_id=trade_id, kind="eod_sweep", price=price,
                            size=max(remaining, 0), at=at, r=net_r)
        events.append(_event(day, symbol, "eod_sweep", at, direction=trade["direction"],
                             price=round(fill.price, 4), r=net_r, trade_id=trade_id))
    return events


def _window_over(day: str, now: datetime) -> bool:
    """The day's signal window has ended: a later calendar day, or at/after
    DAYTRADE_WINDOW_END_ET on the day itself."""
    today = now.strftime("%Y-%m-%d")
    return today > day or (today == day and now.strftime("%H:%M") >= config.DAYTRADE_WINDOW_END_ET)


def _journal_new(day: str, account_id: str, existing: list[dict], new_events: list[dict]) -> list[dict]:
    """Append the not-yet-journaled events (dedup by ``id``), plus a row in
    the rejected-setups log (Round 2, Change 4) for each new skip."""
    existing_ids = {e.get("id") for e in existing}
    to_write = [e for e in new_events if e["id"] not in existing_ids]
    if to_write:
        store.append_signals(day, account_id, to_write)
        rejected = [{"at": e["at"], "symbol": e["symbol"], "direction": e.get("direction"),
                     "reason": e.get("reason_code") or _reason_code(e.get("reason")),
                     "detail": e.get("reason"), "stage": e["event"].replace("_skipped", ""),
                     "id": e["id"]}
                    for e in to_write if e["event"] in ("setup_skipped", "entry_skipped")]
        store.append_rejected(day, account_id, rejected)
        logger.info("daytrade signals: %d new event(s) journaled for %s (account %s)",
                    len(to_write), day, account_id)
    return to_write


def _rehydrate(existing: list[dict], symbols: dict[str, _Symbol], day_state: _Day) -> dict[str, str]:
    """Reconstruct in-memory state from what's ALREADY journaled for this
    account+day, so THIS run's current account_equity/entries_enabled only
    ever governs a genuinely NEW decision — never a bar a past run already
    decided on. Returns {symbol: last_event_at}; the caller skips any bar at
    or before that timestamp for that symbol, since it's already reflected.

    Why this is needed: run_day re-derives the whole day from raw bars every
    call, with no other persisted mid-day state. account_equity is a LIVE
    read of the account's real dry powder and entries_enabled flips once the
    paper trial hits its target — both can legitimately change between two
    runs on the SAME day. Without rehydration, a later run's from-scratch
    replay can relabel an already-"entry" bar "entry_skipped" (budget went
    to 0) or an already-"setup" bar "setup_skipped" (trial completed) —
    contradicting what was already journaled. Worse than a relabeling: the
    symbol then never reaches "in_trade" in THAT run's model, so it never
    gets evaluated for a stop/target/cutoff exit either — an already-open
    trade sits in the journal (and trades.json) as open forever, never
    counts toward the trial, and directly contradicts this function's own
    "still resolves any trade already open" promise (see run_day's
    docstring) — the code did not actually keep that promise before this.

    Why it's safe to do unconditionally: _resolve_trade and the armed
    trigger/expiry check (_triggered, candles_waited) are pure price/candle-
    count logic with NO gate dependency, so replaying them from rehydrated
    state for every bar since is deterministic no matter how many times
    this reruns. The ONE gate-dependent moment — an armed setup actually
    becoming a trade, in _enter_trade — still re-checks the CURRENT gate
    when its bar is reached in the replay below, which is correct, not a
    bug: a genuinely new commitment should see the current world, not a
    stale snapshot of it from whenever it first armed."""
    resume_at: dict[str, str] = {}
    for e in existing:
        symbol = e.get("symbol")
        sym = symbols.get(symbol)
        if sym is None:
            continue
        kind, at = e["event"], e.get("at")
        if kind == "setup":
            sym.status = "armed"
            sym.direction = e.get("direction")
            sym.setup_high, sym.setup_low = e.get("high"), e.get("low")
            sym.setup_at = at
            sym.trade_id = f"{symbol}:setup:{at}"  # matches _process_bar's own construction
            sym.candles_waited = 0
        elif kind in ("expired", "entry_skipped"):
            sym.status = "watching"
        elif kind == "entry":
            sym.status = "in_trade"
            sym.direction = e.get("direction")
            sym.entry, sym.stop = e.get("entry"), e.get("stop")
            sym.target1, sym.target2 = e.get("target1"), e.get("target2")
            sym.size = e.get("size")
            sym.trade_id = e.get("trade_id")
            sym.entry_at = at
            sym.half_taken = False
            day_state.trades_taken += 1
        elif kind == "half_target":
            sym.half_taken = True
            sym.half_at = at
            sym.stop = sym.entry  # the trail's own state is rebuilt from bars (_restore_trail)
        elif kind in ("breakeven_exit", "trail_stop", "final_target", "stop_out", "time_cutoff",
                      "eod_sweep"):
            sym.status = "watching"
            r = e.get("r")
            if r is not None:
                day_state.record_trade_result(r)
        if at:
            resume_at[symbol] = at
    return resume_at


def _restore_trail(symbols: dict[str, _Symbol], bars: list[dict], resume_at: dict[str, str]) -> None:
    """Rebuild each still-open, half-taken trade's trailing-stop state (best
    price, tightened flag, current stop) by folding the bars from the half
    target up to the last journaled event back through ``_advance_trail`` —
    the trail's high-water mark isn't journaled per bar, but it is a pure
    function of the bars, so a replay from a later run resumes it exactly."""
    for bar in bars:
        sym = symbols.get(bar["symbol"])
        if sym is None or sym.status != "in_trade" or not sym.half_taken or not sym.half_at:
            continue
        if sym.half_at <= bar["datetime"] <= resume_at.get(bar["symbol"], ""):
            _advance_trail(sym, bar)
            sym.last_bar = bar


def run_day(day: str, account_id: str, now: datetime | None = None,
            account_equity: float | None = None,
            adapter: adapters.ExecutionAdapter | None = None,
            entries_enabled: bool = True) -> dict:
    """Replay one trading day's bars through ONE ACCOUNT's signal engine and
    journal any new events for it. Idempotent: safe to call repeatedly as
    bars keep arriving (Phase 1's scheduler does, every
    DAYTRADE_BAR_INTERVAL_MINUTES). The screener/bars this reads are shared
    across every account (daytrade/store.py); the signals/trades this
    writes are that account's alone. ``adapter`` defaults to
    ``adapters.get_adapter(day, account_id)`` (PaperAdapter) — tests inject
    their own to assert on fill/trade-log behaviour without a second config
    seam. ``entries_enabled=False`` (the scheduler passes this once
    daytrade.trial.trial_status(account_id) says THIS account's paper trial
    has hit its target) blocks every NEW setup/entry for the day but still
    resolves any trade already open — see _Day.block_reason and, for how
    "already open" survives a gate flip between calls, ``_rehydrate``.
    Returns ``{"date", "events"}`` — every event journaled for this account
    today, oldest first."""
    now = now or datetime.now(ET)
    account_equity = config.DAYTRADE_ACCOUNT_EQUITY if account_equity is None else account_equity
    adapter = adapter if adapter is not None else adapters.get_adapter(day, account_id)

    screen = store.load_screen(day)
    if not screen:
        return {"date": day, "events": []}

    symbols: dict[str, _Symbol] = {}
    for pick in screen.get("picks", []):
        symbols[pick["symbol"]] = _Symbol(pick["prior_day_high"], pick["prior_day_low"],
                                           pick.get("atr14"))
    if not symbols:
        return {"date": day, "events": []}

    existing = store.load_signals(day, account_id)
    day_state = _Day(account_equity, entries_enabled=entries_enabled)
    resume_at = _rehydrate(existing, symbols, day_state)

    bars = [b for b in store.load_bars(day) if b.get("symbol") in symbols]
    bars.sort(key=lambda b: (_parse_at(b["datetime"]), b["symbol"]))

    _restore_trail(symbols, bars, resume_at)

    new_events: list[dict] = []
    for bar in bars:
        symbol = bar["symbol"]
        cutoff = resume_at.get(symbol)
        if cutoff and bar["datetime"] <= cutoff:
            continue  # already reflected in the rehydrated state above
        sym = symbols[symbol]
        new_events.extend(_process_bar(sym, day_state, bar, day, adapter))

    if _window_over(day, now):
        latest = store.latest_bars(day)
        new_events.extend(_finalize_open_trades(symbols, day_state, day, adapter, latest))
        new_events.extend(_sweep_open_trades(day, adapter, latest, existing + new_events, now))

    adapter.flush()

    to_write = _journal_new(day, account_id, existing, new_events)
    return {"date": day, "events": existing + to_write}


def sweep_day(day: str, account_id: str, now: datetime | None = None,
              adapter: adapters.ExecutionAdapter | None = None) -> list[dict]:
    """Close (``eod_sweep``) any trade a PAST day's trade log still has open,
    without replaying that day's bars — no new setups or entries can come of
    it. The scheduler runs this once a day for every earlier day with an
    open trade, which also clears positions left open by the pre-Round-2
    cutoff bug. Returns the events journaled."""
    now = now or datetime.now(ET)
    if not _window_over(day, now):
        return []
    adapter = adapter if adapter is not None else adapters.get_adapter(day, account_id)
    existing = store.load_signals(day, account_id)
    events = _sweep_open_trades(day, adapter, store.latest_bars(day), existing, now)
    if not events:
        return []
    adapter.flush()
    return _journal_new(day, account_id, existing, events)


def open_trade_days(account_id: str, before_day: str) -> list[str]:
    """Days before ``before_day`` whose trade log still holds an open trade."""
    return [day for day, trades in store.iter_all_trades(account_id)
            if day < before_day and any(t.get("status") == "open" for t in trades.values())]


# ---------------------------------------------------------------------------
# Live status — for display only, e.g. a "how close is this to filling /
# closing" meter. NOT part of the rule engine: this re-derives each symbol's
# CURRENT status (armed / in_trade) by walking that account's own PERSISTED
# signals log oldest-first, mirroring the same status transitions
# _process_bar/_enter_trade/_resolve_trade already make. It reads the
# engine's own output rather than replaying bars, so it can surface stale or
# wrong information but can never contradict or influence an actual trading
# decision — nothing here is authoritative.
# ---------------------------------------------------------------------------
_CLOSED_EVENTS = frozenset({"expired", "entry_skipped", "breakeven_exit", "trail_stop",
                            "final_target", "stop_out", "time_cutoff", "eod_sweep"})


def current_status(day: str, account_id: str) -> list[dict]:
    """That account's symbols currently ``armed`` or ``in_trade`` today, each
    with the fields a progress meter needs (setup_low/setup_high for armed;
    entry/stop/target1/target2/half_taken for in_trade) plus the latest
    ingested price (``store.latest_bars``) to mark where price is right now.
    Symbols back to ``watching`` (no live setup/trade) are omitted."""
    state: dict[str, dict] = {}
    for e in store.load_signals(day, account_id):
        row = state.setdefault(e["symbol"], {"symbol": e["symbol"], "status": "watching"})
        kind = e["event"]
        if kind == "setup":
            row.clear()
            row.update(symbol=e["symbol"], status="armed", direction=e.get("direction"),
                      setup_low=e.get("low"), setup_high=e.get("high"))
        elif kind == "entry":
            row.clear()
            row.update(symbol=e["symbol"], status="in_trade", direction=e.get("direction"),
                      entry=e.get("entry"), stop=e.get("stop"), target1=e.get("target1"),
                      target2=e.get("target2"), half_taken=False)
        elif kind == "half_target":
            row["half_taken"] = True
        elif kind in _CLOSED_EVENTS:
            row["status"] = "watching"
        # "setup_skipped" — no state change; the engine never armed in the
        # first place (see _process_bar's "watching" branch).

    bars = store.latest_bars(day)
    out = []
    for row in state.values():
        if row["status"] not in ("armed", "in_trade"):
            continue
        bar = bars.get(row["symbol"])
        row["current_price"] = bar["close"] if bar else None
        row["current_price_at"] = bar["datetime"] if bar else None
        out.append(row)
    return out
