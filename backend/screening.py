"""The CFM scan: market regime, sector strength, stock filter, and the 4-level
entry gate. All read cached/fetched daily bars via data_handler and compute with
indicators — no provider calls beyond what data_handler caches.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import config
import data_handler
import indicators
import sector_data

# Short-TTL memoization so the expensive full-universe scans run once and are
# reused by repeated polls and by the entry gate, instead of recomputing on
# every concurrent request. Per-key locks collapse a thundering herd (many
# parallel callers on a cold cache) into a single computation.
_RESULT_TTL = int(__import__("os").environ.get("SCAN_CACHE_TTL", "300"))
_results: dict[str, tuple[float, object]] = {}
_result_locks: dict[str, threading.Lock] = {}
_results_guard = threading.Lock()


def clear_cache() -> None:
    """Drop memoized scan results — called on a demo/live mode switch so the next
    scan recomputes against the newly active data source."""
    _results.clear()


def warm_scan_cache() -> dict:
    """Pre-compute the full-universe scan so the operator's first Scan of the day
    is served warm instead of triggering a cold ~500-name provider fetch and
    indicator sweep on the request path.

    Left cold, the morning's first hit on Ready-to-Enter / Stock Filter re-fetches
    every symbol from Schwab (the overnight parquet has aged past its freshness
    window) and then runs the indicator sweep — tens of seconds on the one shared
    machine, which is exactly the "stocks won't load" the operator sees. Warming
    the parquet cache in one parallel batch, then priming the memoized sweeps,
    moves that cost off the request path. Called off the scheduler's market-day
    slots (notably the pre-open 08:30 slot) and once shortly after startup.

    Best-effort and self-contained: any failure is caught and returned, never
    raised, so a warm-up can't break the scheduler tick that triggered it."""
    try:
        # One parallel batch warms daily bars for SPY + every sector ETF + every
        # constituent; the sweeps below then read from the now-warm per-symbol
        # cache instead of fetching one name at a time.
        data_handler.prefetch(
            [config.BENCHMARK] + sector_data.sector_etfs() + sector_data.all_tickers()
        )
        regime()
        sectors()
        stock_filter(None)
        # The scorecard sweep is the heaviest Scan panel (Ready-to-Enter runs it);
        # memoize it here too so its first request is a cache hit.
        from metrics import scorecard as scorecard_metrics
        scorecard_metrics.scorecard(None)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — a warm-up must never break its caller
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Background scan runner — decouple the full-universe sweep from the request
# ---------------------------------------------------------------------------
# The heavy scan normally runs inside whichever Scan-tab request triggers it. On
# a phone / installed PWA, backgrounding the app throttles JS and can kill the
# in-flight fetch, abandoning a cold scan half-done. Running the sweep in a
# detached daemon thread — kicked by a request that returns in milliseconds —
# means the work survives the browser tab being backgrounded, navigated away, or
# closed. The client polls scan_status(); results land in the same memo the
# synchronous endpoints read, so a returning client is served warm.
_scan_thread: threading.Thread | None = None
_scan_guard = threading.Lock()
_scan_state: dict = {"status": "idle", "started_at": None, "finished_at": None,
                     "error": None}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _run_background_scan() -> None:
    result = warm_scan_cache()
    with _scan_guard:
        _scan_state.update(
            status="done" if result.get("ok") else "error",
            finished_at=_now_iso(),
            error=None if result.get("ok") else result.get("error"),
        )


def start_background_scan() -> dict:
    """Kick a full-universe scan in a detached daemon thread if one isn't already
    running, and return the status immediately. Idempotent: a concurrent call
    while a scan is in flight just returns the current status (one scan at a
    time, deduped). The work is not tied to the triggering request, so it keeps
    running even if the browser tab is backgrounded, switched, or closed."""
    global _scan_thread
    with _scan_guard:
        if _scan_thread is not None and _scan_thread.is_alive():
            return dict(_scan_state, running=True, fresh=_scan_fresh())
        _scan_state.update(status="running", started_at=_now_iso(),
                           finished_at=None, error=None)
        _scan_thread = threading.Thread(target=_run_background_scan,
                                        name="scan-runner", daemon=True)
        _scan_thread.start()
        return dict(_scan_state, running=True, fresh=_scan_fresh())


def _scan_fresh() -> bool:
    """True when the memoized full-universe sweeps are warm (results available
    without a recompute) — i.e. a returning client can render immediately."""
    return peek_cached("scorecard:full", max_age=_RESULT_TTL) is not None


def scan_status() -> dict:
    """Current background-scan state for the client to poll: idle / running /
    done / error, the start/finish stamps, and whether results are warm."""
    with _scan_guard:
        running = _scan_thread is not None and _scan_thread.is_alive()
        st = dict(_scan_state)
    st["running"] = running
    st["fresh"] = _scan_fresh()
    return st


def peek_cached(key: str, max_age: float | None = None):
    """Return a memoized scan result without ever computing one — a read-only peek
    (unlike ``_cached``, which computes on a miss). ``max_age`` (seconds) bounds
    how stale a hit may be; None returns any present value. Used by the refresh
    policy to read the last GO/earnings candidate pool cheaply on its tight
    cadence, so picking the hot set never triggers a fresh full-universe sweep."""
    hit = _results.get(key)
    if not hit:
        return None
    if max_age is not None and time.time() - hit[0] > max_age:
        return None
    return hit[1]


def _cached(key: str, fn, ttl: int = _RESULT_TTL, store_if=None):
    hit = _results.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _results_guard:
        lock = _result_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _results.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        # Don't pin a transient failure (e.g. a missing VIX right after a token
        # re-auth) for the full TTL — only cache results that pass store_if.
        if store_if is None or store_if(val):
            _results[key] = (time.time(), val)
        return val


# ---------------------------------------------------------------------------
# Level 1 — market regime
# ---------------------------------------------------------------------------
def regime() -> dict:
    # Don't cache a regime whose VIX failed to load — retry on the next poll so
    # it self-heals once the Schwab token is valid again.
    return _cached("regime", _compute_regime, store_if=lambda r: r.get("vix") is not None)


def _compute_regime() -> dict:
    # One parallel batch warms breadth universe + SPY, then compute.
    data_handler.prefetch(config.BREADTH_SYMBOLS + [config.BENCHMARK])
    frames = data_handler.get_many(config.BREADTH_SYMBOLS)
    breadth = indicators.breadth(frames)

    # VIX is an index ($VIX): Schwab's quotes endpoint serves it reliably, while
    # its pricehistory often returns nothing for indices. Take the live quote
    # first (we only need the latest level), then fall back to daily bars.
    vix, vix_source = None, None
    quote = data_handler.latest_quote(config.VIX_SYMBOL)
    if quote and quote.get("price"):
        vix, vix_source = quote["price"], quote.get("source")
    else:
        vix_df = data_handler.get_daily(config.VIX_SYMBOL)
        vix = indicators.last(vix_df)
        vix_source = "daily" if vix is not None else None
    vix_error = None if vix is not None else data_handler.last_error(config.VIX_SYMBOL)

    spy_df = data_handler.get_daily(config.BENCHMARK)
    spy_dist = indicators.pct_from_ma(spy_df) if spy_df is not None else None
    spy_trend = "up" if (spy_dist or 0) > 0 else "down" if spy_dist is not None else "unknown"

    status = "yellow"
    if breadth is not None and vix is not None:
        green = breadth >= config.REGIME_BREADTH_GREEN and vix < config.VIX_CALM and spy_trend == "up"
        red = breadth <= config.REGIME_BREADTH_RED or vix > config.VIX_ELEVATED
        status = "green" if green else "red" if red else "yellow"
    return {
        "status": status,
        "breadth": breadth,
        "vix": round(vix, 2) if vix is not None else None,
        "vix_source": vix_source,
        "vix_error": vix_error,
        "spy_trend": spy_trend,
        "spy_dist_ma21": spy_dist,
    }


# ---------------------------------------------------------------------------
# Level 2 — sector strength
# ---------------------------------------------------------------------------
def _sector_breadth(etf: str) -> float | None:
    frames = data_handler.get_many(sector_data.constituents(etf))
    return indicators.breadth(frames)


def sectors() -> dict:
    return _cached("sectors", _compute_sectors)


def _compute_sectors() -> dict:
    # Warm SPY + every sector ETF + every constituent in one parallel batch, so
    # the per-sector breadth loop below reads from cache instead of fetching
    # 500 symbols one at a time.
    data_handler.prefetch([config.BENCHMARK] + sector_data.sector_etfs() + sector_data.all_tickers())
    spy = data_handler.get_daily(config.BENCHMARK)
    out = {}
    for etf in sector_data.sector_etfs():
        df = data_handler.get_daily(etf)
        rs = indicators.rs3m(df, spy) if df is not None else None
        bdth = _sector_breadth(etf)
        expanding = indicators.atr_expanding(df) if df is not None else None
        strong = (rs is not None and rs >= config.SECTOR_RS3M_MIN
                  and bdth is not None and bdth >= config.SECTOR_BREADTH_MIN)
        status = "green" if strong else "red" if (rs is not None and rs < 0) else "yellow"
        out[etf] = {
            "name": sector_data.sectors()[etf].name,
            "rs3m": rs,
            "breadth": bdth,
            "atr_expanding": expanding,
            "status": status,
        }
    return out


# ---------------------------------------------------------------------------
# Levels 3 & 4 — stock filter
# ---------------------------------------------------------------------------
def _stock_row(ticker: str, spy, sector_rs_vs_spy: float | None, sector_etf: str,
               regime_green: bool = False, sector_strong: bool = False) -> dict:
    df = data_handler.get_daily(ticker)
    rs_vs_spy = indicators.rs3m(df, spy) if df is not None else None
    # A sector ETF entered as its own candidate has no distinct peer sector to
    # beat — comparing it to itself is tautologically zero every time, so that
    # leg is waived (not applicable) rather than scored as a fail.
    is_sector_etf = bool(sector_etf) and ticker.upper() == sector_etf.upper()
    rs_vs_sector = None
    if not is_sector_etf and rs_vs_spy is not None and sector_rs_vs_spy is not None:
        rs_vs_sector = round(rs_vs_spy - sector_rs_vs_spy, 2)
    atrp = indicators.atr_pct(df) if df is not None else None
    cons = indicators.consolidating(df) if df is not None else None

    # Stock-level legs (gate Levels 3 & 4). The "beats SPY" leg uses the lower
    # ETF bar for any ETF (income sleeve, not a growth leader); the "beats sector"
    # leg is waived for a sector ETF entered as its own candidate.
    spy_min = config.rs_vs_spy_min(sector_data.is_etf(ticker))
    beats = (rs_vs_spy is not None and rs_vs_spy > spy_min
             and (is_sector_etf
                  or (rs_vs_sector is not None and rs_vs_sector > config.STOCK_RS_VS_SECTOR_MIN)))

    # "ready" means the FULL gate would pass, so it matches the entry gate's
    # READY TO ENTER verdict — regime + sector must also be green, not just the
    # stock's own strength. blocked_by names what's missing so a strong stock
    # that isn't entry-ready explains why.
    blocked_by = []
    if not regime_green:
        blocked_by.append("regime")
    if not sector_strong:
        blocked_by.append("sector")
    if not beats:
        blocked_by.append("stock")
    if not cons:
        blocked_by.append("consolidation")

    if not blocked_by:
        status = "ready"
    elif rs_vs_sector is not None and rs_vs_sector < 0:
        status = "no"
    else:
        status = "wait"
    return {
        "ticker": ticker,
        "sector": sector_etf,
        "rs3m_vs_spy": rs_vs_spy,
        "rs3m_vs_sector": rs_vs_sector,
        "is_sector_etf": is_sector_etf,
        "atr_pct": atrp,
        "consolidating": cons,
        "stock_strong": beats,
        "blocked_by": blocked_by,
        "status": status,
    }


def stock_filter(sector: str | None = None) -> list[dict]:
    key = f"stock_filter:{(sector or 'ALL').upper()}"
    return _cached(key, lambda: _compute_stock_filter(sector))


def _compute_stock_filter(sector: str | None = None) -> list[dict]:
    etfs = [sector.upper()] if sector else sector_data.sector_etfs()
    # Parallel-warm SPY + the sector ETF(s) + their constituents first.
    universe = [config.BENCHMARK] + etfs
    for etf in etfs:
        universe += sector_data.constituents(etf)
    data_handler.prefetch(universe)
    spy = data_handler.get_daily(config.BENCHMARK)
    # Regime + sector strength gate "ready" the same way the entry gate does, so
    # the filter's status agrees with the gate verdict.
    regime_green = regime().get("status") == "green"
    sector_status = sectors()
    rows = []
    for etf in etfs:
        sector_df = data_handler.get_daily(etf)
        sector_rs = indicators.rs3m(sector_df, spy) if sector_df is not None else None
        sector_strong = sector_status.get(etf, {}).get("status") == "green"
        # The ETF itself is a valid CFM candidate alongside its constituents —
        # liquid, weekly-optionable, and a real entry choice in its own right.
        rows.append(_stock_row(etf, spy, sector_rs, etf,
                               regime_green=regime_green, sector_strong=sector_strong))
        for ticker in sector_data.constituents(etf):
            rows.append(_stock_row(ticker, spy, sector_rs, etf,
                                   regime_green=regime_green, sector_strong=sector_strong))
    # Sort by RS3M vs Sector descending (best fit first); None last.
    rows.sort(key=lambda r: (r["rs3m_vs_sector"] is None, -(r["rs3m_vs_sector"] or 0)))
    return rows


# ---------------------------------------------------------------------------
# The 4-level entry gate (stop on first fail)
# ---------------------------------------------------------------------------
def _check(label: str, value, passed) -> dict:
    """One named sub-condition with its value and pass flag (native bool)."""
    return {"label": label, "value": value, "pass": bool(passed)}


def _all(checks: list[dict]) -> bool:
    return all(c["pass"] for c in checks)


def entry_gate(ticker: str) -> dict:
    ticker = ticker.upper()
    sector_etf = sector_data.sector_for(ticker)
    levels = []

    # Level 1 — market regime. Each sub-condition is shown independently so a
    # fail is never ambiguous about *which* leg missed.
    reg = regime()
    l1_checks = [
        _check(f"Breadth ≥ {config.REGIME_BREADTH_GREEN:g}%", reg.get("breadth"),
               reg.get("breadth") is not None and reg["breadth"] >= config.REGIME_BREADTH_GREEN),
        _check(f"VIX < {config.VIX_CALM:g}", reg.get("vix"),
               reg.get("vix") is not None and reg["vix"] < config.VIX_CALM),
        _check("SPY trend up", reg.get("spy_trend"), reg.get("spy_trend") == "up"),
    ]
    levels.append({"level": 1, "name": "Market regime green", "pass": _all(l1_checks),
                   "checks": l1_checks, "detail": reg})

    # Level 2 — sector strong
    sec = sectors().get(sector_etf, {}) if sector_etf else {}
    l2_checks = [
        _check(f"Sector RS3M ≥ +{config.SECTOR_RS3M_MIN:g}%", sec.get("rs3m"),
               sec.get("rs3m") is not None and sec.get("rs3m") >= config.SECTOR_RS3M_MIN),
        _check(f"Sector breadth ≥ {config.SECTOR_BREADTH_MIN:g}%", sec.get("breadth"),
               sec.get("breadth") is not None and sec.get("breadth") >= config.SECTOR_BREADTH_MIN),
    ]
    levels.append({"level": 2, "name": "Sector strong", "pass": _all(l2_checks),
                   "checks": l2_checks, "detail": {"sector": sector_etf, **sec}})

    # Levels 3 & 4 — stock beating peers + consolidating
    spy = data_handler.get_daily(config.BENCHMARK)
    sector_df = data_handler.get_daily(sector_etf) if sector_etf else None
    sector_rs = indicators.rs3m(sector_df, spy) if sector_df is not None else None
    # Pass the regime/sector verdicts so the row's status matches this gate's.
    row = _stock_row(ticker, spy, sector_rs, sector_etf or "",
                     regime_green=_all(l1_checks), sector_strong=_all(l2_checks))

    # The two legs are checked separately: "beats SPY" and "beats its sector"
    # are distinct conditions, so the UI can show exactly which one failed. A
    # sector ETF entered as its own candidate has no peer sector to beat — the
    # comparison is tautologically itself, so that leg is waived (N/A, not a
    # fail) rather than blocking a real ETF entry on a meaningless self-check.
    rs_spy, rs_sec = row["rs3m_vs_spy"], row["rs3m_vs_sector"]
    is_etf = row.get("is_sector_etf", False)
    # Any ETF (sector or added) beats SPY on the lower income-sleeve bar; only a
    # sector ETF waives the beats-sector leg (it IS the sector).
    spy_min = config.rs_vs_spy_min(sector_data.is_etf(ticker))
    l3_checks = [
        _check(f"RS3M vs SPY > +{spy_min:g}%", rs_spy,
               rs_spy is not None and rs_spy > spy_min),
        _check(f"RS3M vs Sector > {config.STOCK_RS_VS_SECTOR_MIN:g}%"
               + (" (N/A — is the sector)" if is_etf else ""),
               rs_sec, is_etf or (rs_sec is not None and rs_sec > config.STOCK_RS_VS_SECTOR_MIN)),
    ]
    levels.append({"level": 3, "name": "Stock beating peers", "pass": _all(l3_checks),
                   "checks": l3_checks, "detail": row})

    l4_checks = [
        _check(f"ATR% ≤ {config.CONSOLIDATION_ATR_PCT_MAX:g}", row["atr_pct"],
               row["atr_pct"] is not None and row["atr_pct"] <= config.CONSOLIDATION_ATR_PCT_MAX),
        _check("Near MA21 (consolidating)", row["consolidating"], bool(row["consolidating"])),
    ]
    levels.append({"level": 4, "name": "Consolidating, not breaking", "pass": _all(l4_checks),
                   "checks": l4_checks, "detail": {"atr_pct": row["atr_pct"], "consolidating": row["consolidating"]}})

    # Stop-on-fail: the cleared level is the highest contiguous pass from 1.
    cleared = 0
    for lv in levels:
        if lv["pass"]:
            cleared = lv["level"]
        else:
            break
    verdict = "READY TO ENTER" if cleared == 4 else "WAIT"
    return {"ticker": ticker, "sector": sector_etf, "levels": levels,
            "cleared_level": cleared, "verdict": verdict}


# ---------------------------------------------------------------------------
# Daily checklist (15-minute routine)
# ---------------------------------------------------------------------------
def daily_checklist(state: dict) -> list[dict]:
    import datetime as _dt
    import kill_switch

    items = []
    reg = regime()
    items.append({"id": "regime", "label": f"Market regime: {reg['status'].upper()}",
                  "ok": reg["status"] == "green", "detail": reg})

    meta = state.get("metadata", {})
    reserve = float(meta.get("reserve_required") or 0)
    operating = float(meta.get("operating_cash") or 0)
    items.append({"id": "reserve", "label": "Reserve funded",
                  "ok": operating >= reserve or reserve == 0,
                  "detail": {"operating_cash": operating, "reserve_required": reserve}})

    # Kill-switch sweep across open positions.
    for ev in kill_switch.evaluate_all(state):
        items.append({"id": f"ks_{ev['ticker']}",
                      "label": f"{ev['ticker']} RS3M intact (vs SPY {ev['rs3m_vs_spy']}, vs Sector {ev['rs3m_vs_sector']})",
                      "ok": ev["status"] != "red", "detail": ev})

    # Short calls expiring + LEAPs nearing roll DTE + earnings approaching.
    import earnings
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        try:
            earn = earnings.next_earnings(p.get("ticker", ""))
        except Exception:  # noqa: BLE001
            earn = {"warning": False}
        if earn.get("warning"):
            items.append({"id": f"earnings_{p['ticker']}",
                          "label": (f"{p['ticker']} earnings in {earn['days_until']}d "
                                    f"({earn['date']}) — roll deep-ITM or exit"),
                          "ok": False, "detail": earn})
        for sc in p.get("short_calls", []):
            dte = sc.get("dte")
            if dte is not None and dte <= 2:
                items.append({"id": f"short_{p['ticker']}_{sc.get('strike')}",
                              "label": f"Roll {p['ticker']} short (DTE {dte})", "ok": False,
                              "detail": sc})
        leap = p.get("leap") or {}
        if leap.get("dte") is not None and leap["dte"] <= config.LEAP_ROLL_DTE:
            items.append({"id": f"leap_{p['ticker']}",
                          "label": f"{p['ticker']} LEAP nearing {config.LEAP_ROLL_DTE} DTE (roll out)",
                          "ok": False, "detail": leap})

    is_friday = _dt.date.today().weekday() == 4
    if is_friday:
        items.append({"id": "friday", "label": "Friday: roll all shorts, log juice, check cap status",
                      "ok": False, "detail": {"weekday": "Friday"}})
    return items
