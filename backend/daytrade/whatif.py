"""What-if replay: run a saved day's screener picks + bars through the signal
engine with some parameters overridden, and compare against the live values.

Writes nothing (``run_day(persist=False)`` + an in-memory adapter) and the
overrides are context-local (``params.overriding``), so a replay can't touch
the account's real signal/trade logs or the scheduler's concurrent run.
"""
from __future__ import annotations

from datetime import datetime

import config

from daytrade import adapters, params, signals, store


class _MemoryAdapter(adapters.PaperAdapter):
    """PaperAdapter fills, no disk: starts empty and never flushes."""

    def __init__(self, day: str):
        self.day = day
        self.account_id = "whatif"
        self.trades: dict[str, dict] = {}

    def flush(self) -> None:
        pass


def _replay(day: str, overrides: dict) -> dict:
    adapter = _MemoryAdapter(day)
    equity = overrides.get("DAYTRADE_ACCOUNT_EQUITY", config.DAYTRADE_ACCOUNT_EQUITY)
    # `now` after the window so open trades are closed at the cutoff, as live
    # does once the window is over.
    end = datetime.strptime(f"{day} 23:59", "%Y-%m-%d %H:%M").replace(tzinfo=signals.ET)
    with params.overriding(overrides):
        out = signals.run_day(day, "whatif", now=end, account_equity=equity,
                              adapter=adapter, persist=False)
    return {"events": out["events"], "trades": list(adapter.trades.values())}


def _summary(result: dict) -> dict:
    trades = result["trades"]
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if (t["realized_r"] or 0) > 0]
    return {
        "trades": len(trades),
        "closed": len(closed),
        "wins": len(wins),
        "losses": sum(1 for t in closed if (t["realized_r"] or 0) < 0),
        "net_r": round(sum(t["realized_r"] or 0 for t in closed), 2),
        "net_pnl": round(sum(t["realized_pnl"] or 0 for t in trades), 2),
        "skipped": sum(1 for e in result["events"] if e["event"] in ("entry_skipped", "setup_skipped")),
    }


def compare(day: str, raw_overrides: dict) -> dict:
    overrides = params.coerce_overrides(raw_overrides)
    if store.load_screen(day) is None:
        return {"date": day, "ran": False}
    base = _replay(day, {})
    mod = _replay(day, overrides)
    return {"date": day, "ran": True, "overrides": overrides,
            "baseline": {"summary": _summary(base), "trades": base["trades"]},
            "modified": {"summary": _summary(mod), "trades": mod["trades"]}}
