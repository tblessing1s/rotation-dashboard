"""The CFM scan: market regime, sector strength, stock filter, and the 4-level
entry gate. All read cached/fetched daily bars via data_handler and compute with
indicators — no provider calls beyond what data_handler caches.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import config
import data_handler
import indicators
import regime_genius
import regime_history
import sector_data
import stock_lights

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

    spy_df = data_handler.get_daily(config.GENIUS_INDEX_SYMBOL)

    # Genius four-light regime + yellow dwell. compute_trace is pure; the dwell
    # reads the chronological prior PUBLISHED regimes from the daily history,
    # excluding any record already stored for today (so today's own nightly-
    # persisted record can't double-count in its own dwell). The regime light is
    # decided by the four lights + the dwell ONLY; breadth + VIX ride along as
    # secondary informational indicators, and SPY's MA21 trend is not a regime
    # input at all (not computed or surfaced).
    vix_disp = round(vix, 2) if vix is not None else None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prior_published = regime_history.prior_published(before=today)
    trace = regime_genius.compute_trace(spy_df, breadth, vix_disp, prior_published)

    # Merge the legacy VIX provenance fields the existing UI / snapshot read
    # (status is the published regime; breadth/vix are the secondary indicators).
    # The four-light trace is otherwise additive.
    trace.update({
        "vix": vix_disp,
        "vix_source": vix_source,
        "vix_error": vix_error,
    })
    return trace


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
        # The sector gate now bars on RS1M vs SPY (a fresher 1-month read) plus
        # breadth. RS3M vs SPY is a laggy 3-month figure that keeps a rolled-over
        # sector "strong" for weeks after it turns down, so it is kept for DISPLAY
        # only — the gate keys off rs1m > SECTOR_RS1M_MIN.
        rs1m = indicators.rs1m(df, spy) if df is not None else None
        rs3m = indicators.rs3m(df, spy) if df is not None else None
        bdth = _sector_breadth(etf)
        expanding = indicators.atr_expanding(df) if df is not None else None
        strong = (rs1m is not None and rs1m > config.SECTOR_RS1M_MIN
                  and bdth is not None and bdth >= config.SECTOR_BREADTH_MIN)
        # Level-2 reframe: sector as a VETO, not a selector. The gate blocks only on
        # positive evidence the sector is DETERIORATING — lagging SPY (RS1M < 0),
        # breadth collapsing (below the collapse floor, well under the participation
        # bar), or the sector ETF itself under distribution (the classifier's InstFlow
        # on price/volume). Missing data never vetoes (fail-open). Otherwise the
        # sector passes through and lets SYM + BASE + INST carry selection. `strong`
        # (the old bar) is kept for display / sizing only.
        import structure_classifier
        inst_flow = structure_classifier.classify_symbol(df)[1] if df is not None else None
        det_reasons = []
        if rs1m is not None and rs1m < 0:
            det_reasons.append("rs1m_negative")
        if bdth is not None and bdth < config.SECTOR_BREADTH_COLLAPSE:
            det_reasons.append("breadth_collapsing")
        if inst_flow == structure_classifier.InstFlow.DISTRIBUTING:
            det_reasons.append("under_distribution")
        deteriorating = bool(det_reasons)
        status = "green" if strong else "red" if deteriorating else "yellow"
        out[etf] = {
            "name": sector_data.sectors()[etf].name,
            "rs1m": rs1m,        # display + the sizing "strong" bar (vs SPY, 1-month)
            "rs3m": rs3m,        # display only (vs SPY, 3-month)
            "breadth": bdth,
            "atr_expanding": expanding,
            "inst_flow": inst_flow,           # sector ETF's own accumulation/distribution read
            "strong": strong,                 # the old "sector strong" bar — display / sizing only
            "deteriorating": deteriorating,   # the Level-2 VETO: True blocks entry
            "deteriorating_reasons": det_reasons,
            "status": status,
        }
    return out


# ---------------------------------------------------------------------------
# Levels 3 & 4 — stock filter
# ---------------------------------------------------------------------------
def _stock_row(ticker: str, spy, sector_df, sector_etf: str,
               regime_green: bool = False, sector_strong: bool = False) -> dict:
    df = data_handler.get_daily(ticker)
    is_sector_etf = bool(sector_etf) and ticker.upper() == sector_etf.upper()
    is_etf = sector_data.is_etf(ticker)

    # RS3M (3-month) is DISPLAY / kill-switch only now — kept on the row so the UI
    # and snapshot still show it, but it no longer gates entry. A sector ETF has
    # no distinct peer sector to beat (tautologically itself), so its vs-sector RS
    # is N/A.
    rs3m_vs_spy = indicators.rs3m(df, spy) if df is not None else None
    rs3m_vs_sector = (indicators.rs3m(df, sector_df)
                      if (not is_sector_etf and df is not None and sector_df is not None) else None)
    # RS1M (1-month) is the RANKING key within GREENs: rs1m_vs_sector desc for
    # stocks, rs1m_vs_spy desc for ETFs (item F).
    rs1m_vs_spy = indicators.rs1m(df, spy) if df is not None else None
    rs1m_vs_sector = (indicators.rs1m(df, sector_df)
                      if (not is_sector_etf and df is not None and sector_df is not None) else None)
    atrp = indicators.atr_pct(df) if df is not None else None

    # The per-name Genius lights + vetoes + right-spot gate. The vs-sector veto is
    # waived for ETFs inside stock_lights (an ETF has no growth-leader peer). IVR
    # for the volatility veto is read from the local IV history file.
    try:
        import iv_history
        ivr_percentile = (iv_history.iv_rank(ticker) or {}).get("iv_percentile")
    except Exception:  # noqa: BLE001
        ivr_percentile = None
    sl = stock_lights.compute(df, sector_df=(None if is_etf else sector_df),
                              ivr_percentile=ivr_percentile, is_etf=is_etf)
    stock_green = sl["verdict"] == stock_lights.GREEN
    spot = sl["right_spot"]

    # "ready" means the FULL pipeline would pass (worst-signal-wins): regime green
    # -> sector strong -> stock lights GREEN -> right-spot -> (Level 5 checked
    # separately). blocked_by names every failing stage so a strong name that is
    # not entry-ready explains why. The right-spot gate contributes its own
    # spot:<check> reasons.
    blocked_by = []
    if not regime_green:
        blocked_by.append("regime")
    if not sector_strong:
        blocked_by.append("sector")
    if not stock_green:
        blocked_by.append("lights")
    blocked_by.extend(spot["blocked_by"])
    # Vetoes already force the verdict to RED (folded into "lights"); surface them
    # explicitly too so the reason is legible.
    blocked_by.extend(sl["veto_reasons"])

    if not blocked_by:
        status = "ready"
    elif sl["verdict"] == stock_lights.RED and sl["vetoed"]:
        status = "no"
    else:
        status = "wait"
    return {
        "ticker": ticker,
        "sector": sector_etf,
        "rs3m_vs_spy": rs3m_vs_spy,
        "rs3m_vs_sector": rs3m_vs_sector,
        "rs1m_vs_spy": rs1m_vs_spy,
        "rs1m_vs_sector": rs1m_vs_sector,
        "is_sector_etf": is_sector_etf,
        "is_etf": is_etf,
        "atr_pct": atrp,
        # Per-name Genius light block (mirrors the market regime's four lights).
        "lights": sl["lights"],
        "greens": sl["greens"],
        "verdict": sl["verdict"],
        "insufficient": sl["insufficient"],
        "vetoes": sl["vetoes"],
        "vetoed": sl["vetoed"],
        "veto_reasons": sl["veto_reasons"],
        "right_spot": spot,
        "enterable": sl["enterable"],
        "stock_green": stock_green,
        # Back-compat: `consolidating` now means "in the right spot" (the gate that
        # replaced the old single consolidating flag).
        "consolidating": spot["pass"],
        "blocked_by": blocked_by,
        # Ranking key within GREENs (rs1m_vs_sector for stocks, rs1m_vs_spy for ETFs).
        "rank_key": (rs1m_vs_spy if is_etf else rs1m_vs_sector),
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
        sector_strong = sector_status.get(etf, {}).get("status") == "green"
        # The ETF itself is a valid CFM candidate alongside its constituents —
        # liquid, weekly-optionable, and a real entry choice in its own right.
        rows.append(_stock_row(etf, spy, sector_df, etf,
                               regime_green=regime_green, sector_strong=sector_strong))
        for ticker in sector_data.constituents(etf):
            rows.append(_stock_row(ticker, spy, sector_df, etf,
                                   regime_green=regime_green, sector_strong=sector_strong))
    # Ranking (item F): GREENs first, then by the RS1M rank key descending
    # (rs1m_vs_sector for stocks, rs1m_vs_spy for ETFs); None last within a group.
    rows.sort(key=lambda r: (r.get("verdict") != stock_lights.GREEN,
                             r.get("rank_key") is None, -(r.get("rank_key") or 0)))
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

    # Level 1 — market regime. The Genius four-light regime: Level 1 passes iff the
    # dwell-adjusted PUBLISHED regime is green. The traffic light is decided by the
    # four lights + the yellow dwell ONLY; breadth/VIX are secondary informational
    # indicators (carried in `detail.secondary`), NOT gate conditions. The four
    # lights are shown as sub-checks — the level does NOT require all four green (a
    # green vote is >=3 of 4), so the level's pass is the published regime itself,
    # not _all(checks). `detail` carries the full trace for the snapshot.
    reg = regime()
    lights = reg.get("lights") or {}

    def _light_check(label: str, key: str) -> dict:
        sig = (lights.get(key) or {}).get("signal")
        return _check(label, sig, sig == "green")

    l1_checks = [
        _light_check(f"Close > {config.GENIUS_SLOW_MA} SMA", "close_vs_ma"),
        _light_check(f"EMA{config.GENIUS_FAST_MA} > SMA{config.GENIUS_SLOW_MA}", "fast_vs_slow"),
        _light_check("Parabolic SAR below price", "sar"),
        _light_check(f"ROC({config.GENIUS_MOMENTUM_ROC}) > 0", "momentum"),
    ]

    # `published_regime` is the app-facing regime; fall back to the legacy `status`
    # so callers/tests that stub regime() with just {"status": ...} still gate.
    regime_green = (reg.get("published_regime") or reg.get("status")) == "green"
    levels.append({"level": 1, "name": "Market regime green", "pass": regime_green,
                   "checks": l1_checks, "detail": reg})

    # Level 2 — sector NOT deteriorating (a VETO, not a selector). The old bar
    # required the sector to be strong (RS1M vs SPY > 0 AND breadth ≥ 60); the
    # reframe blocks only on positive evidence of deterioration — RS1M negative,
    # breadth collapsing, or the sector ETF under distribution — and otherwise
    # passes through, letting SYM + BASE + INST carry selection. Missing data never
    # vetoes (fail-open). One-position-per-sector at Level 5 still caps the
    # concentration this bar used to manage implicitly.
    sec = sectors().get(sector_etf, {}) if sector_etf else {}
    l2_checks = [
        _check(f"Sector RS1M vs SPY not negative", sec.get("rs1m"),
               not (sec.get("rs1m") is not None and sec.get("rs1m") < 0)),
        _check(f"Sector breadth not collapsing (≥ {config.SECTOR_BREADTH_COLLAPSE:g}%)", sec.get("breadth"),
               not (sec.get("breadth") is not None and sec.get("breadth") < config.SECTOR_BREADTH_COLLAPSE)),
        _check("Sector not under distribution", sec.get("inst_flow"),
               sec.get("inst_flow") != "DISTRIBUTING"),
    ]
    levels.append({"level": 2, "name": "Sector not deteriorating", "pass": _all(l2_checks),
                   "checks": l2_checks, "detail": {"sector": sector_etf, **sec}})

    # Level 3 — stock lights GREEN. The SAME four Genius lights as the market
    # regime, applied per name (stock_lights). Level 3 passes iff the stock verdict
    # is GREEN (4/4 lights green AND no veto). The four lights are shown as
    # sub-checks; a veto (folded into the verdict) or an insufficient light both
    # drop the verdict below GREEN. YELLOW (exactly 3 green) is a watchlist state,
    # never enterable, so it does NOT pass this level.
    spy = data_handler.get_daily(config.BENCHMARK)
    sector_df = data_handler.get_daily(sector_etf) if sector_etf else None
    row = _stock_row(ticker, spy, sector_df, sector_etf or "",
                     regime_green=regime_green, sector_strong=_all(l2_checks))
    row_lights = row.get("lights") or {}

    def _row_light_check(label: str, key: str) -> dict:
        sig = (row_lights.get(key) or {}).get("signal")
        return _check(label, sig, sig == "green")

    l3_checks = [
        _row_light_check(f"Close > {config.GENIUS_SLOW_MA} SMA", "close_vs_ma"),
        _row_light_check(f"EMA{config.GENIUS_FAST_MA} > SMA{config.GENIUS_SLOW_MA}", "fast_vs_slow"),
        _row_light_check("Parabolic SAR below price", "sar"),
        _row_light_check(f"ROC({config.GENIUS_MOMENTUM_ROC}) > 0", "momentum"),
    ]
    stock_green = row.get("verdict") == stock_lights.GREEN
    levels.append({"level": 3, "name": "Stock lights green", "pass": stock_green,
                   "checks": l3_checks, "detail": row})

    # Level 3.5 — structure. The pure classifier's (BaseStage, InstFlow) mapped
    # through the entrability grid, inserted between "beats peers" (L3) and "right
    # spot" (L4): a topping / declining / distributing / insufficient structure
    # (BLOCKED) or a watchlist-only cell (WATCH) is not entrable and stops the gate.
    # Reads only price/volume structure, so it runs IDENTICALLY for stocks and ETFs
    # (no is_etf branch, no vs-sector path). All classifier thresholds are
    # PROPOSED_DEFAULT. Stop-on-first-fail is preserved: with the level ordered 3.5
    # here, a structure miss holds cleared at 3 (WAIT), and a full clear still
    # reaches cleared == 4 only when 3.5 AND 4 both pass.
    import structure_classifier
    df_struct = data_handler.get_daily(ticker)
    base_stage, inst_flow = structure_classifier.classify_symbol(df_struct)
    entrability = structure_classifier.structure_entrability(base_stage, inst_flow)
    l35_pass = entrability in (structure_classifier.Entrability.READY,
                               structure_classifier.Entrability.CAUTION)
    l35_checks = [_check(f"Structure entrable ({base_stage} × {inst_flow})",
                         entrability, l35_pass)]
    levels.append({"level": 3.5, "name": "Structure entrable", "pass": bool(l35_pass),
                   "checks": l35_checks,
                   "detail": {"base_stage": base_stage, "inst_flow": inst_flow,
                              "entrability": entrability}})

    # Level 4 — right spot. A SEPARATE, blocking gate applied AFTER the lights (the
    # consolidation "right spot" checks are NOT lights). Identical for stocks/ETFs.
    spot = row.get("right_spot") or {"checks": [], "pass": False}
    spot_by_id = {c["id"]: c for c in spot.get("checks") or []}
    l4_checks = [
        _check(f"ATR% ≤ {config.CONSOLIDATION_ATR_PCT_MAX:g}",
               (spot_by_id.get("atr_pct") or {}).get("value"),
               (spot_by_id.get("atr_pct") or {}).get("pass")),
        _check(f"ATR contracting/flat (≤ {config.SPOT_ATR_MOMENTUM_MAX:g})",
               (spot_by_id.get("atr_5d_ema") or {}).get("value"),
               (spot_by_id.get("atr_5d_ema") or {}).get("pass")),
        _check(f"Extension ≤ {config.SPOT_ATR_EXTENSION_MAX:g} ATR above MA21",
               (spot_by_id.get("extension") or {}).get("value"),
               (spot_by_id.get("extension") or {}).get("pass")),
    ]
    levels.append({"level": 4, "name": "Right spot (not extended)", "pass": bool(spot.get("pass")),
                   "checks": l4_checks, "detail": {"atr_pct": row["atr_pct"],
                                                   "right_spot": spot, "consolidating": row["consolidating"]}})

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
