"""Paper-trading trial tracker — per account.

Per the strategy brief's own definition of success — a tracked run in paper
mode before ever flipping to live — and an explicit operator decision to
size that run at ``config.DAYTRADE_TRIAL_TRADES`` completed trades (the
brief's own backtest precedent: "two rounds of 50 trades"), run
independently for each account with day-trading turned on
(``daytrade.settings``): each gets its own budget, so each gets its own
trade count and its own verdict.

``trial_status(account_id)`` aggregates every CLOSED trade across that
account's trade logs (``daytrade/store.py``'s
``YYYY-MM-DD.<account_id>.trades.json``, via ``store.iter_all_trades()``)
into one running trial, capped at the target. Pure/read-only, recomputed on
every call — the same recompute-from-source trade-off ``signals.run_day``
already makes for a day's events, so there is no separate running counter
that could drift from what the trade logs actually record.

Once an account's trial reaches its target, ``daytrade/scheduler.py`` stops
opening new positions FOR THAT ACCOUNT (``signals.run_day``'s
``entries_enabled``) — a trade already open that day still plays out
normally to its rule-6 exit, and every OTHER enabled account keeps running
on its own clock. The verdict (WIN/LOSS/FLAT) is net $ P&L across the
counted trades. Going live from there is an explicit operator decision
(``config.daytrade_mode()``), never automatic — this module only reports,
it never flips the switch.
"""
from __future__ import annotations

import config

from daytrade import store


def trial_status(account_id: str) -> dict:
    """``{target_trades, completed_trades, status, verdict, net_r,
    net_pnl, win_rate}`` for one account — ``status`` is "running" or
    "complete"; ``verdict`` ("win"/"loss"/"flat") is set only once
    complete."""
    trades: list[dict] = []
    for _day, day_trades in store.iter_all_trades(account_id):
        for t in day_trades.values():
            if t.get("status") == "closed":
                trades.append(t)
    trades.sort(key=lambda t: t.get("closed_at") or "")

    target = config.DAYTRADE_TRIAL_TRADES
    counted = trades[:target]
    completed = len(counted)
    net_r = sum(t.get("realized_r") or 0 for t in counted)
    net_pnl = sum(t.get("realized_pnl") or 0 for t in counted)
    wins = sum(1 for t in counted if (t.get("realized_r") or 0) > 0)

    status = "complete" if completed >= target else "running"
    verdict = None
    if status == "complete":
        verdict = "win" if net_pnl > 0 else "loss" if net_pnl < 0 else "flat"

    return {
        "target_trades": target,
        "completed_trades": completed,
        "status": status,
        "verdict": verdict,
        "net_r": round(net_r, 4),
        "net_pnl": round(net_pnl, 2),
        "win_rate": round(wins / completed * 100, 1) if completed else None,
    }
