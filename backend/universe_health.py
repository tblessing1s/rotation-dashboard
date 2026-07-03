"""Universe health — which tickers in the CFM universe actually work.

Sweeps every name in tickers_by_sector.txt (plus the sector ETFs) and reports
the two ways a ticker can be a dead weight in the list:

  * no_data      — no provider returned OHLCV for it (a renamed / delisted /
                   typo'd ticker; it silently shows "—" everywhere and never
                   scans). This is what turns "am I missing stocks" into a live,
                   self-updating answer instead of a manual audit.
  * no_weeklies  — data is fine but the name has no weekly options, so it can't
                   run CFM (the weekly short can't be sold). Optional + heavier
                   (one option-chain probe per ticker), so it's off by default.

Meant to be run on demand from the live app (it needs provider keys). In demo
mode the store is synthetic, so a data sweep is meaningless and is skipped.
"""
from __future__ import annotations

import config
import data_handler
import sector_data


def vet_candidates(symbols: list[str]) -> dict:
    """Run a list of candidate symbols through the CFM criteria so any list (from
    QQQ, a screener, a tip) becomes an add-ready shortlist. Per symbol reports
    whether it's already in the universe, returns data, has weekly options (the
    hard CFM gate), and its live Scorecard verdict + juice — all from the same
    machinery a normal scan uses. ``fit`` = has data + has weeklies + not already
    present (the addable set). Verdict/juice are shown as current-tradeability
    context, not a membership requirement."""
    if config.demo_enabled():
        return {"skipped": "demo mode — run in live mode to vet against real data",
                "candidates": [], "fit_count": 0}
    from metrics import scorecard as scorecard_metrics

    syms = list(dict.fromkeys(str(s).strip().upper() for s in (symbols or []) if str(s).strip()))
    if not syms:
        return {"candidates": [], "fit_count": 0, "sectors": sector_data.sector_etfs()}

    rows = {r["ticker"]: r for r in scorecard_metrics.scorecard(syms).get("results", [])}
    out = []
    for t in syms:
        r = rows.get(t, {})
        in_sector = sector_data.sector_for(t)
        has_data = r.get("price") is not None
        hw = r.get("has_weeklies")
        already = in_sector is not None
        fit = bool(has_data and hw is True and not already)
        if already:
            reason = f"already in {in_sector}"
        elif not has_data:
            reason = "no data (dead / typo)"
        elif hw is False:
            reason = "no weekly options — can't run CFM"
        elif hw is None:
            reason = "weeklies unknown"
        else:
            reason = ""
        out.append({
            "ticker": t, "in_universe": in_sector, "has_data": has_data,
            "has_weeklies": hw, "verdict": r.get("verdict"),
            "juice_weekly_pct": r.get("juice_weekly_pct"), "fit": fit, "reason": reason,
        })
    return {"candidates": out, "fit_count": sum(1 for c in out if c["fit"]),
            "sectors": sector_data.sector_etfs()}


def check(check_weeklies: bool = False, tickers: list[str] | None = None) -> dict:
    """Sweep the universe (or a supplied subset) and report dead / CFM-unusable
    tickers. OHLCV is fetched in parallel over the shared pool; the weeklies
    probe (when enabled) is likewise prefetched in parallel."""
    if config.demo_enabled():
        return {"skipped": "demo mode — the demo store is synthetic; run this in live mode",
                "total": 0, "no_data": [], "checked_weeklies": False}

    tickers = tickers or sector_data.all_tickers()
    frames = data_handler.get_many(tickers)  # parallel, degrades per-symbol

    with_data, no_data = [], []
    for t in tickers:
        df = frames.get(t)
        if df is None or getattr(df, "empty", True):
            no_data.append({"ticker": t, "sector": sector_data.sector_for(t),
                            "error": data_handler.last_error(t)})
        else:
            with_data.append(t)

    report = {
        "total": len(tickers),
        "with_data": len(with_data),
        "no_data": sorted(no_data, key=lambda r: (r["sector"] or "", r["ticker"])),
        "checked_weeklies": False,
    }

    if check_weeklies:
        import weeklies
        weeklies.prefetch(with_data)  # warm the weeklies cache in parallel
        no_weeklies = []
        for t in with_data:
            if weeklies.has_weeklies(t) is False:   # None = unknown, don't flag
                no_weeklies.append({"ticker": t, "sector": sector_data.sector_for(t)})
        report["checked_weeklies"] = True
        report["no_weeklies"] = sorted(no_weeklies, key=lambda r: (r["sector"] or "", r["ticker"]))
        report["cfm_ready"] = len(with_data) - len(no_weeklies)

    return report
