"""Execution adapters — Phase 3.

Strategy code (``daytrade/signals.py``) talks to ONE interface
(``ExecutionAdapter``) to turn a rule-engine DECISION (enter here, exit
there) into a FILL, rather than assuming its requested price is what
happened. ``PaperAdapter`` is the only implementation in this change —
``SchwabAdapter`` (live money) is Phase 6 of the build order and does not
exist yet; ``get_adapter()`` picks between them off ``config.daytrade_mode()``
and raises rather than silently substituting paper for an unimplemented
live mode.

``PaperAdapter`` fills instantly, exactly at the requested price — still no
slippage/partial-fill model (see signals.py's module docstring for why
that's an explicit stand-in) — and is also the day's TRADE LOG: every
``enter``/``exit`` call updates that trade's row in
``daytrade/store.py``'s ``YYYY-MM-DD.trades.json`` (trade_id -> row), the
aggregated, fill-oriented counterpart to the signal engine's raw event
journal. One adapter instance is constructed per ``signals.run_day()`` call
(loading that day's existing trades, if any, so a later replay resumes the
same rows rather than starting over) and flushed back to disk once at the
end of the replay — the same "recompute in memory, persist once" shape
``run_day`` already uses for the signals journal.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

import config

from daytrade import store

# Exit kinds signals.py can report that CLOSE a trade (the position's
# remaining size goes to zero). "half_target" is deliberately excluded —
# the trade stays open with its remainder after a half-target exit.
TERMINAL_EXIT_KINDS = frozenset({"stop_out", "breakeven_exit", "final_target", "time_cutoff"})


@dataclass(frozen=True)
class Fill:
    """What actually happened, as opposed to what the strategy asked for.
    PaperAdapter always returns request == fill; a future SchwabAdapter is
    exactly the seam that would make them differ (slippage, partial size)."""
    price: float
    size: int
    at: str


class ExecutionAdapter(abc.ABC):
    """What the signal engine needs from wherever a trade actually happens."""

    @abc.abstractmethod
    def enter(self, *, symbol: str, trade_id: str, direction: str, price: float,
              size: int, at: str) -> Fill:
        """Request an entry fill for a newly-triggered setup (rule 4)."""

    @abc.abstractmethod
    def exit(self, *, symbol: str, trade_id: str, kind: str, price: float,
              size: int, at: str, r: float) -> Fill:
        """Request an exit fill for part or all of an open trade's size
        (rule 6's half-target, or a terminal stop/target/cutoff exit). ``r``
        is the R-multiple the signal engine already computed for this exit
        (rule-defined, not fill-dependent) — the adapter records it, it
        doesn't recompute it."""

    def flush(self) -> None:
        """Called once at the end of a run_day() replay. A no-op by default
        (e.g. a live adapter that places each order immediately has nothing
        to batch); PaperAdapter overrides this to persist the trade log."""


class PaperAdapter(ExecutionAdapter):
    """REPLAY-SAFE: ``run_day`` reprocesses a day's bars from scratch on
    every call, so ``enter``/``exit`` WILL be asked to fill the same
    trade_id (and the same exit) more than once across separate
    ``run_day()`` calls (and, for ``enter``, even within one call if the
    signal engine ever re-armed the same id — it can't, ids are unique per
    setup bar, but the guard costs nothing). Both are therefore idempotent:
    ``enter`` returns the EXISTING row's fill instead of overwriting it (an
    overwrite would silently wipe out exits a prior replay already
    recorded), and ``exit`` skips appending if that exact (trade_id, kind,
    at) is already in ``exits`` rather than double-booking size and P&L."""

    def __init__(self, day: str):
        self.day = day
        self.trades: dict[str, dict] = store.load_trades(day)

    def enter(self, *, symbol: str, trade_id: str, direction: str, price: float,
              size: int, at: str) -> Fill:
        existing = self.trades.get(trade_id)
        if existing is not None:
            e = existing["entry"]
            return Fill(price=e["price"], size=e["size"], at=e["at"])
        self.trades[trade_id] = {
            "schema_version": store.SCHEMA_VERSION, "date": self.day, "symbol": symbol,
            "trade_id": trade_id, "direction": direction,
            "entry": {"price": round(price, 4), "size": size, "at": at},
            "exits": [], "status": "open", "realized_r": None, "realized_pnl": 0.0,
            "closed_at": None,
        }
        return Fill(price=price, size=size, at=at)

    def exit(self, *, symbol: str, trade_id: str, kind: str, price: float,
              size: int, at: str, r: float) -> Fill:
        trade = self.trades[trade_id]
        already = next((e for e in trade["exits"] if e["kind"] == kind and e["at"] == at), None)
        if already is not None:
            return Fill(price=already["price"], size=already["size"], at=at)
        sign = 1 if trade["direction"] == "long" else -1
        pnl = round(size * (price - trade["entry"]["price"]) * sign, 2)
        trade["exits"].append({"kind": kind, "price": round(price, 4), "size": size,
                                "at": at, "r": r, "pnl": pnl})
        trade["realized_pnl"] = round(trade["realized_pnl"] + pnl, 2)
        if kind in TERMINAL_EXIT_KINDS:
            trade["status"] = "closed"
            trade["realized_r"] = r
            trade["closed_at"] = at
        return Fill(price=price, size=size, at=at)

    def flush(self) -> None:
        """Persist this replay's trades (new + updated) for the day. Called
        once at the end of signals.run_day, mirroring how it flushes the
        signals journal once rather than per bar."""
        store.save_trades(self.day, self.trades)


def get_adapter(day: str) -> ExecutionAdapter:
    mode = config.daytrade_mode()
    if mode == "paper":
        return PaperAdapter(day)
    raise NotImplementedError(
        f"daytrade execution mode {mode!r} has no adapter yet — SchwabAdapter "
        "(live money) is a later phase of the build order, not built in this change")
