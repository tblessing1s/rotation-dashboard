"""Nightly maintenance — earnings & dividends as first-class cached data.

Instead of ad-hoc lookups, the next earnings date and the next dividend event
(ex-date + amount) for every open-position ticker are refreshed once per night
into their day caches, and each position's stored ``dividend`` snapshot is
updated so ASSIGNMENT_RISK and the Positions tab read current data. Runs from
the alert scheduler's nightly slot (config.MAINTENANCE_ET) or on demand via
POST /api/maintenance/refresh. Skipped in demo mode — the demo store is
synthetic and pinned by overrides.
"""
from __future__ import annotations

import logging

import config
import dividends
import earnings
import logging_handler as log

logger = logging.getLogger("cfm.alerts")


def open_tickers(state: dict | None = None) -> list[str]:
    state = state or log.load_state()
    return [p.get("ticker", "") for p in state.get("positions", [])
            if p.get("status") != "closed" and p.get("ticker")]


def nightly_refresh() -> dict:
    """Refresh earnings + dividend caches for every held name and sync each
    position's dividend snapshot. Returns a per-ticker report."""
    if config.demo_enabled():
        return {"skipped": "demo mode", "tickers": []}
    report = {"tickers": [], "errors": []}
    tickers = open_tickers()
    fresh_div: dict[str, dict] = {}
    for t in tickers:
        entry = {"ticker": t}
        try:
            entry["earnings"] = earnings.next_earnings(t, refresh=True).get("date")
        except Exception as e:  # noqa: BLE001 — one ticker must not sink the sweep
            report["errors"].append(f"{t} earnings: {e}")
        try:
            div = dividends.next_dividend(t, refresh=True)
            fresh_div[t] = div
            entry["ex_div"] = div.get("ex_date")
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{t} dividend: {e}")
        report["tickers"].append(entry)

    if fresh_div:
        state = log.load_state()
        changed = False
        for p in state.get("positions", []):
            div = fresh_div.get(p.get("ticker", ""))
            if div and p.get("status") != "closed":
                p["dividend"] = div
                changed = True
        if changed:
            log.save_state(state)
    logger.info("nightly maintenance refreshed %d ticker(s), %d error(s)",
                len(report["tickers"]), len(report["errors"]))
    return report
