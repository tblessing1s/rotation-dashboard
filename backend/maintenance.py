"""Nightly maintenance — earnings, dividends & cash balance as first-class
cached data.

Instead of ad-hoc lookups, the next earnings date and the next dividend event
(ex-date + amount) for every open-position ticker are refreshed once per night
into their day caches, and each position's stored ``dividend`` snapshot is
updated so ASSIGNMENT_RISK and the Positions tab read current data. The
operating-cash balance is also synced from the live Schwab account (a
read-only account call — doesn't require CFM_LIVE_TRADING) so the Capital
card / portfolio risk / daily checklist stay fresh even on a day the Execute
tab is never opened (account_gate.resolve_operating_cash refreshes it
opportunistically too, whenever the Level 5 gate runs). Runs from the alert
scheduler's nightly slot (config.MAINTENANCE_ET) or on demand via
POST /api/maintenance/refresh. Skipped in demo mode — the demo store is
synthetic and pinned by overrides.
"""
from __future__ import annotations

import logging

import backups
import config
import dividends
import earnings
import logging_handler as log

logger = logging.getLogger("cfm.alerts")


def open_tickers(state: dict | None = None) -> list[str]:
    state = state or log.load_state()
    return [p.get("ticker", "") for p in state.get("positions", [])
            if p.get("status") != "closed" and p.get("ticker")]


def snapshot_leap_deltas(today: str | None = None) -> list[dict]:
    """Append one {date, leap_delta} point per open LEAP to each position's
    delta_history, retaining the most recent DELTA_HISTORY_DAYS. Idempotent per
    day: a second run on the same date overwrites that day's point rather than
    duplicating it (a mid-day restart re-runs the nightly slot). Returns a small
    per-ticker report."""
    import indicators
    import leap_policy

    day = today or log.utcnow()[:10]
    state = log.load_state()
    report = []
    changed = False
    for p in state.get("positions", []):
        if p.get("status") == "closed" or not (p.get("leap") or {}):
            continue
        health = leap_policy.leap_health(p)
        delta = health.get("leap_delta")
        hist = p.setdefault("delta_history", [])
        if hist and hist[-1].get("date") == day:
            hist[-1]["leap_delta"] = delta       # same-day re-run: overwrite
        else:
            hist.append({"date": day, "leap_delta": delta})
        del hist[:-config.DELTA_HISTORY_DAYS]      # retain the newest N
        changed = True
        report.append({"ticker": p.get("ticker"), "leap_delta": delta})
    if changed:
        log.save_state(state)
    return report


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

    # Append today's per-position LEAP delta to the rolling delta_history that
    # powers the delta-velocity early warning (retained DELTA_HISTORY_DAYS).
    try:
        report["delta_snapshots"] = snapshot_leap_deltas()
    except Exception as e:  # noqa: BLE001 — a snapshot failure must not sink the sweep
        report["errors"].append(f"delta_snapshot: {e}")

    try:
        import account_gate
        cash_info = account_gate.resolve_operating_cash(log.load_state())
        report["operating_cash"] = cash_info
    except Exception as e:  # noqa: BLE001 — a cash-sync failure must not sink the sweep
        report["errors"].append(f"operating_cash: {e}")

    # Nightly position reconciliation (state.json vs Schwab), only when connected
    # — a read-only account call. A failed run records a failure (feeding
    # reconcile_stale); it never produces an empty broker view. (Demo mode never
    # reaches here — nightly_refresh returns early above.)
    try:
        import schwab_api
        if schwab_api.configured():
            import reconcile
            recon = reconcile.run_reconciliation()
            report["reconciliation"] = {"status": recon.get("status"),
                                        "diffs": len(recon.get("diffs", [])),
                                        "broker_ok": recon.get("broker_ok")}
    except Exception as e:  # noqa: BLE001 — a reconcile failure must not sink the sweep
        report["errors"].append(f"reconcile: {e}")

    # Nightly rotating backup + off-machine copy of state.json. Runs last so a
    # backup failure (which self-alerts through the Notifier) never blocks the
    # data refresh above.
    try:
        report["backup"] = backups.nightly_backup()
        off = (report["backup"].get("offmachine") or {})
        logger.info("nightly backup: local=%s off-machine=%s(%s)",
                    report["backup"].get("local"), off.get("method"),
                    "ok" if off.get("ok") else "FAILED")
    except Exception as e:  # noqa: BLE001 — belt-and-braces; nightly_backup never raises
        report["errors"].append(f"backup: {e}")

    logger.info("nightly maintenance refreshed %d ticker(s), %d error(s)",
                len(report["tickers"]), len(report["errors"]))
    return report
