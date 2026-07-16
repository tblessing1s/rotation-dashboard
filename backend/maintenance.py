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


def snapshot_burn_marks(today: str | None = None) -> list[dict]:
    """Record one weekly theta-burn mark per open LEAP: the model extrinsic at the
    live DTE plus the forward burn projection re-run against current spot & IV, so
    the realized-vs-projected divergence series stays current. IV is the ticker's
    trailing realized vol (indicators.hist_vol) — the same offline BS basis the
    juice estimate and roll-cost estimator use; the divergence tracking is exactly
    what verifies whether that basis (and the put-IV path) holds up. Best-effort
    per ticker: a pricing gap skips that name, never the sweep. Returns a report."""
    import burn
    import burn_marks
    import data_handler
    import dividends
    import indicators
    import leap_policy

    day = today or log.utcnow()[:10]
    state = log.load_state()
    out = []
    for p in state.get("positions", []):
        if p.get("status") == "closed" or not (p.get("leap") or {}):
            continue
        ticker = p.get("ticker", "")
        try:
            df = data_handler.get_daily(ticker)
            spot = indicators.last(df)
            hv = indicators.hist_vol(df)  # annualized %
            leap = p.get("leap") or {}
            dte = leap_policy._leap_dte(p)
            planned_exit = p.get("planned_exit_dte", config.PLANNED_EXIT_DTE)
            q = dividends.yield_for(ticker) or 0.0
            proj = burn.burn_projection(
                {"strike": leap.get("strike"), "contracts": leap.get("contracts"),
                 "expiration": leap.get("expiration")},
                spot, hv, dte, planned_exit, q=q)
            mark = burn_marks.record_mark(ticker, proj, spot=spot, iv=hv,
                                          current_dte=dte, day=day)
            out.append({"ticker": ticker,
                        "recorded": mark is not None,
                        "burn_per_week": (mark or {}).get("projected_burn_per_week"),
                        "realized_burn_week": (mark or {}).get("realized_burn_week")})
        except Exception as e:  # noqa: BLE001 — one ticker must not sink the sweep
            logger.info("burn mark skipped for %s: %s", ticker, e)
            out.append({"ticker": ticker, "recorded": False, "error": str(e)})
    return out


def snapshot_iv(tickers: list[str]) -> list[dict]:
    """Compute + record today's weekly IV for each ticker via the option-chain
    view (its capture hook writes the point). Best-effort per ticker — a blocked
    view (RED tape) or a provider hiccup skips that name, never the sweep."""
    import iv_history
    import option_chain
    out = []
    for t in tickers:
        try:
            option_chain.option_chain(t)  # records weekly IV as a side effect
            out.append({"ticker": t, "iv_rank": iv_history.iv_rank(t).get("iv_rank")})
        except Exception as e:  # noqa: BLE001 — one ticker must not sink the sweep
            logger.info("iv snapshot skipped for %s: %s", t, e)
    return out


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

    # Record today's weekly IV for each held name so IV rank has a daily point
    # even on days the operator never opens a chain (the option-chain view
    # records it opportunistically the rest of the time).
    try:
        report["iv_snapshots"] = snapshot_iv(tickers)
    except Exception as e:  # noqa: BLE001 — an IV snapshot failure must not sink the sweep
        report["errors"].append(f"iv_snapshot: {e}")

    # Persist today's market-regime decision trace (the Genius four-light vote,
    # yellow-dwell state, and the secondary breadth/VIX indicators) so the dwell
    # has a trading-day sequence to count against and calibration has full regime
    # provenance. This
    # is DERIVED telemetry (DATA_DIR/regime_history.json), recomputable from cached
    # SPY bars — never touches the execution record. Runs after the post-close
    # refresh has cached the official close.
    try:
        import regime_history
        rec = regime_history.record_today()
        report["regime"] = {"published": (rec or {}).get("published_regime"),
                            "raw": (rec or {}).get("raw_condition")} if rec else None
    except Exception as e:  # noqa: BLE001 — a regime snapshot failure must not sink the sweep
        report["errors"].append(f"regime_snapshot: {e}")

    # Shadow-log today's Symbol Genius color for the held names + sector ETFs, so
    # SYM flip frequency can be measured before deciding whether a per-symbol
    # yellow dwell is worth building (the audit's explicit prerequisite). Pure
    # telemetry (DATA_DIR/symbol_genius_history.json) — records what SYM already
    # shows, changes nothing. Sector ETFs are always cached post-close, so the log
    # has a stable set even with no open positions.
    try:
        import sector_data
        import symbol_genius_history
        names = list(dict.fromkeys(tickers + sector_data.sector_etfs()))
        report["symbol_genius_log"] = symbol_genius_history.record_today(names)
    except Exception as e:  # noqa: BLE001 — a SYM snapshot failure must not sink the sweep
        report["errors"].append(f"symbol_genius_log: {e}")

    # Weekly theta-burn mark (end-of-week cadence, once per ISO week): snapshots
    # each LEAP's model extrinsic + forward burn projection so the
    # realized-vs-projected divergence harness stays current. Telemetry only
    # (DATA_DIR/burn_marks.json) — never touches the execution record.
    try:
        import burn_marks
        if burn_marks.weekly_due():
            report["burn_marks"] = snapshot_burn_marks()
    except Exception as e:  # noqa: BLE001 — a burn-mark failure must not sink the sweep
        report["errors"].append(f"burn_marks: {e}")

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
