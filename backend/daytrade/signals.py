"""Day-trade signal engine — strategy rules 3-8.

Replays one trading day's ingested 5-min bars (``daytrade/bars.py``) against
today's screener picks (``daytrade/universe.py``, prior-day high/low + ATR14)
through a small per-symbol state machine, and journals every signal the
strategy produces — taken or not, with outcome (rule 8) — to
``daytrade/store.py``'s signals log.

STATE MACHINE (per symbol, independent of the other picks):

    watching --[setup, rule 3]--> armed --[break, rule 4]--> in_trade --> watching
       ^                            |                            |
       |                       [2 candles, no break]        [stop / target / cutoff]
       +----------------------------+----------------------------+

A symbol returns to ``watching`` after a trade resolves or a setup expires —
"Max 2 trades/day" (rule 7) is an ACCOUNT-WIDE cap enforced by the shared
``_Day`` guardrail state, not a one-trade-per-symbol limit.

THIS IS A REPLAY, NOT A LIVE STREAM: ``run_day`` recomputes the whole day's
events from the stored bars every time it's called (idempotent — new events
are diffed against what's already journaled by ``id`` before appending), the
same "re-run is cheap and correct" trade-off Phase 1's scheduler already
makes for the screener. There is no persisted mid-day machine state.

FILLS GO THROUGH ``daytrade/adapters.py`` (Phase 3): every entry/exit this
engine decides on is a REQUEST to an ``ExecutionAdapter`` (default
``PaperAdapter``, resolved per day via ``adapters.get_adapter``), not an
assumption. ``PaperAdapter`` still fills instantly and exactly at the
requested price — no slippage/partial-fill model yet, that's
``SchwabAdapter``'s problem, a later phase — but the seam is real: this
engine only ever uses the returned ``Fill``, never the request, for the
price/size it books. The adapter is also the day's trade log; see its
module docstring for how that differs from this module's signals journal.

REPLAY-SAFE ADAPTER CALLS: because a re-run replays bars this engine has
already seen, ``enter``/``exit`` are idempotent on the adapter side (keyed
by trade_id and by trade_id+kind+at respectively) — calling them again for
an already-recorded fill is a no-op, not a double-fill or a double-counted
P&L. See ``PaperAdapter`` for the dedup.

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
    """Account-wide guardrail state (rule 7) shared across every symbol."""

    def __init__(self, account_equity: float):
        self.account_equity = account_equity
        self.trades_taken = 0
        self.losses = 0
        self.cumulative_r = 0.0
        self.stopped_reason: str | None = None

    def block_reason(self) -> str | None:
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


def run_day(day: str, now: datetime | None = None, account_equity: float | None = None,
            adapter: adapters.ExecutionAdapter | None = None) -> dict:
    """Replay one trading day's bars through the signal engine and journal
    any new events. Idempotent: safe to call repeatedly as bars keep
    arriving (Phase 1's scheduler does, every DAYTRADE_BAR_INTERVAL_MINUTES).
    ``adapter`` defaults to ``adapters.get_adapter(day)`` (PaperAdapter) —
    tests inject their own to assert on fill/trade-log behaviour without a
    second config seam. Returns ``{"date", "events"}`` — every event
    journaled for the day so far, oldest first."""
    now = now or datetime.now(ET)
    account_equity = config.DAYTRADE_ACCOUNT_EQUITY if account_equity is None else account_equity
    adapter = adapter if adapter is not None else adapters.get_adapter(day)

    screen = store.load_screen(day)
    if not screen:
        return {"date": day, "events": []}

    symbols: dict[str, _Symbol] = {}
    for pick in screen.get("picks", []):
        symbols[pick["symbol"]] = _Symbol(pick["prior_day_high"], pick["prior_day_low"],
                                           pick.get("atr14"))
    if not symbols:
        return {"date": day, "events": []}

    bars = [b for b in store.load_bars(day) if b.get("symbol") in symbols]
    bars.sort(key=lambda b: (_parse_at(b["datetime"]), b["symbol"]))

    day_state = _Day(account_equity)
    new_events: list[dict] = []
    for bar in bars:
        sym = symbols[bar["symbol"]]
        new_events.extend(_process_bar(sym, day_state, bar, day, adapter))

    window_over = now.strftime("%H:%M") >= config.DAYTRADE_WINDOW_END_ET
    if window_over:
        new_events.extend(_finalize_open_trades(symbols, day_state, day, adapter))

    adapter.flush()

    existing = store.load_signals(day)
    existing_ids = {e.get("id") for e in existing}
    to_write = [e for e in new_events if e["id"] not in existing_ids]
    if to_write:
        store.append_signals(day, to_write)
        logger.info("daytrade signals: %d new event(s) journaled for %s", len(to_write), day)

    return {"date": day, "events": existing + to_write}
