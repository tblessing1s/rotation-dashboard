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
"Max 2 trades/day" (rule 7) is an ACCOUNT-WIDE cap (one account's own day)
enforced by the shared ``_Day`` guardrail state, not a one-trade-per-symbol
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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class _Symbol:
    __slots__ = ("prior_high", "prior_low", "atr14", "risk_per_share", "seen_volumes",
                 "status", "direction", "setup_high", "setup_low", "setup_at",
                 "candles_waited", "entry", "stop", "target1", "target2", "half_taken",
                 "size", "entry_at", "trade_id", "last_bar")

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
                  adapter: adapters.ExecutionAdapter) -> dict | None:
    """Commit a triggered setup as a live trade. Returns None (and leaves the
    symbol able to re-arm) if a guardrail blocks the entry at trigger time —
    rare (the same guardrail was clear when the setup armed) but re-checked
    for correctness, e.g. another symbol used the last daily slot in between."""
    reason = day_state.block_reason()
    if reason:
        sym.status = "watching"
        return _event(bar["date"], bar["symbol"], "entry_skipped", bar["datetime"],
                      direction=sym.direction, reason=reason, trade_id=sym.trade_id)
    requested_entry = sym.setup_high if sym.direction == "long" else sym.setup_low
    risk_per_share = sym.risk_per_share
    risk_amount = day_state.account_equity * (config.DAYTRADE_RISK_PCT / 100.0)
    requested_size = int(risk_amount // risk_per_share) if risk_per_share > 0 else 0
    if requested_size <= 0:
        # 1% of the current budget doesn't buy even one share at this risk
        # distance — e.g. the funding book's dry powder is at or near zero
        # (daytrade/budget.py). A real trade here would be a size-0 no-op
        # that still consumed one of the day's two slots; skip it outright
        # instead, the same as any other guardrail block.
        sym.status = "watching"
        return _event(bar["date"], bar["symbol"], "entry_skipped", bar["datetime"],
                      direction=sym.direction, reason="no budget available", trade_id=sym.trade_id)

    fill = adapter.enter(symbol=bar["symbol"], trade_id=sym.trade_id, direction=sym.direction,
                         price=requested_entry, size=requested_size, at=bar["datetime"])
    entry, size = fill.price, fill.size
    stop = entry - risk_per_share if sym.direction == "long" else entry + risk_per_share
    target1 = entry + risk_per_share if sym.direction == "long" else entry - risk_per_share
    target2 = (entry + 2 * risk_per_share if sym.direction == "long"
               else entry - 2 * risk_per_share)

    sym.status = "in_trade"
    sym.entry, sym.stop, sym.target1, sym.target2 = entry, stop, target1, target2
    sym.half_taken = False
    sym.size = size
    sym.entry_at = fill.at
    sym.last_bar = bar  # so a trade entered on the day's last bar still has
                        # something to close out against if finalize() runs
    day_state.trades_taken += 1
    return _event(bar["date"], bar["symbol"], "entry", bar["datetime"], direction=sym.direction,
                  entry=round(entry, 4), stop=round(stop, 4), target1=round(target1, 4),
                  target2=round(target2, 4), size=size, trade_id=sym.trade_id)


def _exit_fill(adapter: adapters.ExecutionAdapter, sym: _Symbol, bar: dict, kind: str,
                price: float, size: int, r: float) -> float:
    """Request an exit fill and return the price actually booked."""
    fill = adapter.exit(symbol=bar["symbol"], trade_id=sym.trade_id, kind=kind,
                        price=price, size=size, at=bar["datetime"], r=r)
    return fill.price


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
            sym.stop = sym.entry  # move to breakeven, rule 6
            events.append(_event(bar["date"], bar["symbol"], "half_target", bar["datetime"],
                                  direction=sym.direction, price=round(price, 4),
                                  r=config.DAYTRADE_HALF_TARGET_R, trade_id=sym.trade_id))
            # fall through: the same bar can also resolve the remainder below
        else:
            sym.last_bar = bar
            return events

    # Remainder (half already banked at DAYTRADE_HALF_TARGET_R, stop at breakeven).
    breakeven_hit = bar["low"] <= sym.stop if long else bar["high"] >= sym.stop
    target2_hit = bar["high"] >= sym.target2 if long else bar["low"] <= sym.target2
    if breakeven_hit:
        net_r = 0.5 * config.DAYTRADE_HALF_TARGET_R
        price = _exit_fill(adapter, sym, bar, "breakeven_exit", sym.stop, remainder_size, net_r)
        day_state.record_trade_result(net_r)
        events.append(_event(bar["date"], bar["symbol"], "breakeven_exit", bar["datetime"],
                              direction=sym.direction, price=round(price, 4),
                              r=net_r, trade_id=sym.trade_id))
        sym.status = "watching"
    elif target2_hit:
        net_r = 0.5 * config.DAYTRADE_HALF_TARGET_R + 0.5 * config.DAYTRADE_FULL_TARGET_R
        price = _exit_fill(adapter, sym, bar, "final_target", sym.target2, remainder_size, net_r)
        day_state.record_trade_result(net_r)
        events.append(_event(bar["date"], bar["symbol"], "final_target", bar["datetime"],
                              direction=sym.direction, price=round(price, 4),
                              r=net_r, trade_id=sym.trade_id))
        sym.status = "watching"
    else:
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
            if reason:
                events.append(_event(day, bar["symbol"], "setup_skipped", bar["datetime"],
                                      direction=direction, reason=reason))
            elif sym.risk_per_share is None or sym.risk_per_share <= 0:
                events.append(_event(day, bar["symbol"], "setup_skipped", bar["datetime"],
                                      direction=direction, reason="no ATR14 on file"))
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
            ev = _enter_trade(sym, day_state, bar, adapter)
            if ev:
                events.append(ev)
        elif sym.candles_waited >= config.DAYTRADE_ENTRY_EXPIRY_CANDLES:
            events.append(_event(day, bar["symbol"], "expired", bar["datetime"],
                                  direction=sym.direction, trade_id=sym.trade_id))
            sym.status = "watching"
    elif sym.status == "in_trade":
        events.extend(_resolve_trade(sym, day_state, bar, adapter))

    sym.seen_volumes.append(bar["volume"])
    return events


def _finalize_open_trades(symbols: dict[str, _Symbol], day_state: _Day, day: str,
                           adapter: adapters.ExecutionAdapter) -> list[dict]:
    """Force-exit any still-open trade at the window cutoff (rule 6), using
    the last bar seen for that symbol as the cutoff price. Only called once
    the caller has established the window has actually ended — a trade still
    open mid-window is left alone (unresolved) for the next replay to pick
    up once a later bar lands."""
    events: list[dict] = []
    for symbol, sym in symbols.items():
        if sym.status != "in_trade" or sym.last_bar is None:
            continue
        long = sym.direction == "long"
        close = sym.last_bar["close"]
        size = sym.size - sym.size // 2 if sym.half_taken else sym.size
        if sym.half_taken:
            remainder_r = ((close - sym.entry) if long else (sym.entry - close)) / sym.risk_per_share
            net_r = 0.5 * config.DAYTRADE_HALF_TARGET_R + 0.5 * remainder_r
        else:
            net_r = ((close - sym.entry) if long else (sym.entry - close)) / sym.risk_per_share
        price = _exit_fill(adapter, sym, sym.last_bar, "time_cutoff", close, size, round(net_r, 4))
        day_state.record_trade_result(net_r)
        events.append(_event(day, symbol, "time_cutoff", sym.last_bar["datetime"],
                              direction=sym.direction, price=round(price, 4),
                              half_taken=sym.half_taken, r=round(net_r, 4), trade_id=sym.trade_id))
        sym.status = "watching"
    return events


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
        elif kind in ("breakeven_exit", "final_target", "stop_out", "time_cutoff"):
            sym.status = "watching"
            r = e.get("r")
            if r is not None:
                day_state.record_trade_result(r)
        if at:
            resume_at[symbol] = at
    return resume_at


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

    new_events: list[dict] = []
    for bar in bars:
        symbol = bar["symbol"]
        cutoff = resume_at.get(symbol)
        if cutoff and bar["datetime"] <= cutoff:
            continue  # already reflected in the rehydrated state above
        sym = symbols[symbol]
        new_events.extend(_process_bar(sym, day_state, bar, day, adapter))

    window_over = now.strftime("%H:%M") >= config.DAYTRADE_WINDOW_END_ET
    if window_over:
        new_events.extend(_finalize_open_trades(symbols, day_state, day, adapter))

    adapter.flush()

    existing_ids = {e.get("id") for e in existing}
    to_write = [e for e in new_events if e["id"] not in existing_ids]
    if to_write:
        store.append_signals(day, account_id, to_write)
        logger.info("daytrade signals: %d new event(s) journaled for %s (account %s)",
                    len(to_write), day, account_id)

    return {"date": day, "events": existing + to_write}


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
_CLOSED_EVENTS = frozenset({"expired", "entry_skipped", "breakeven_exit",
                            "final_target", "stop_out", "time_cutoff"})


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
