"""The CFM scan: market regime, sector strength, stock filter, and the entry
evaluation. All read cached/fetched daily bars via data_handler and compute with
indicators — no provider calls beyond what data_handler caches.

``entry_gate`` is no longer a four-level filter. It evaluates the thin VETO SET
(``scan_verdict.VETOES`` — the exit mirrors plus hard account constraints) and
gathers the RANKING inputs everything else became. See ``scan_verdict`` for the
governing principle and ``scan_score`` for the rank.
"""
from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

# Short-TTL memoization so the expensive full-universe scans run once and are
# reused by repeated polls and by the entry gate, instead of recomputing on
# every concurrent request. Per-key locks collapse a thundering herd (many
# parallel callers on a cold cache) into a single computation.
_RESULT_TTL = int(__import__("os").environ.get("SCAN_CACHE_TTL", "300"))
_results: dict[str, tuple[float, object]] = {}
_result_locks: dict[str, threading.Lock] = {}
_results_guard = threading.Lock()


def clear_memo() -> None:
    """Drop ONLY the in-process memo, keeping the persisted day sweep.

    This is the UNIVERSE-CHANGE invalidation. The memo has to go, or the Scan tab
    would keep serving the old ticker list for minutes; the disk sweep must NOT,
    because its rows are still correct for every name that didn't change and
    ``scan_cache.reusable`` will serve them while only the added names are
    computed. Clearing the disk here is what used to turn a one-ticker edit into a
    full cold ~500-name sweep on the request path."""
    _results.clear()


def clear_cache() -> None:
    """Drop memoized scan results AND the persisted day sweep — the GLOBAL
    invalidation, for a demo/live mode switch: a sweep computed against the other
    data source must never be replayed for this one.

    For a universe change use ``clear_memo`` instead; there the stored rows are
    still valid for every unchanged name."""
    clear_memo()
    try:
        import scan_cache
        scan_cache.clear()
    except Exception:  # noqa: BLE001 — clearing a cache must never break its caller
        pass


def warm_scan_cache(force: bool = False) -> dict:
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
            config.scan_base_frames()
            + sector_data.sector_etfs() + sector_data.all_tickers()
        )
        regime()
        sectors()
        stock_filter(None)
        # The scorecard sweep is the heaviest Scan panel (Ready-to-Enter runs it);
        # memoize it here too so its first request is a cache hit.
        from metrics import scorecard as scorecard_metrics
        scorecard_metrics.scorecard(None, force=force)
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


def _run_background_scan(force: bool = False) -> None:
    result = warm_scan_cache(force=force)
    with _scan_guard:
        _scan_state.update(
            status="done" if result.get("ok") else "error",
            finished_at=_now_iso(),
            error=None if result.get("ok") else result.get("error"),
        )


def start_background_scan(force: bool = False) -> dict:
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
                                        kwargs={"force": force},
                                        name="scan-runner", daemon=True)
        _scan_thread.start()
        return dict(_scan_state, running=True, fresh=_scan_fresh())


def warm_symbols(tickers) -> dict:
    """Fetch daily bars + probe weeklies for these names, so they are warm before
    any sweep reaches them. Best-effort and never raises.

    Called when a ticker is ADDED to the universe. A brand-new name is cold in two
    independent caches, and the second one is the expensive half: the weeklies
    probe is a live option-chain call, and a name whose chain can't be read pays
    its retry/backoff too. Paying that here — off-request, right after the edit —
    is what keeps it off the operator's next Scan."""
    names = [str(t).strip().upper() for t in (tickers or []) if str(t).strip()]
    if not names:
        return {"warmed": [], "ok": True}
    try:
        import weeklies
        data_handler.prefetch(names)
        weeklies.prefetch(names)
        return {"warmed": names, "ok": True}
    except Exception as e:  # noqa: BLE001 — warming is an optimization, never a dependency
        logger.warning("could not warm %s (%s)", ", ".join(names), e)
        return {"warmed": [], "ok": False, "error": str(e)}


def start_background_warm(tickers) -> dict:
    """Warm new names in a detached daemon thread and return immediately, so the
    add request answers at once and the fetching happens off the request path."""
    names = [str(t).strip().upper() for t in (tickers or []) if str(t).strip()]
    if not names:
        return {"warming": []}
    threading.Thread(target=warm_symbols, args=(names,),
                     name="universe-warm", daemon=True).start()
    return {"warming": names}


def _scan_fresh() -> bool:
    """True when a full-universe sweep is available WITHOUT recomputing — i.e. a
    returning client can render immediately.

    Checks the day cache as well as the in-process memo. That matters twice over:
    a restarted machine has an empty memo but a perfectly good sweep on disk, and
    the Scan tab auto-kicks a (forced) rescan whenever this reads false — so
    counting only the 5-minute memo would force a full sweep every time the tab
    was opened after a few idle minutes, which is exactly what the day cache is
    meant to stop."""
    if peek_cached("scorecard:full", max_age=_RESULT_TTL) is not None:
        return True
    return _day_cache_status()["warm"]


def _day_cache_status() -> dict:
    """This epoch's disk-cache state, or a cold answer if anything goes wrong."""
    try:
        import scan_cache
        from metrics import scorecard as scorecard_metrics
        return scan_cache.status(sector_data.all_tickers(),
                                 scorecard_metrics._current_regime_color())
    except Exception:  # noqa: BLE001 — a status read must never break the poll
        return {"warm": False, "scan_day": None, "scanned_at": None}


def scan_status() -> dict:
    """Current background-scan state for the client to poll: idle / running /
    done / error, the start/finish stamps, and whether results are warm."""
    with _scan_guard:
        running = _scan_thread is not None and _scan_thread.is_alive()
        st = dict(_scan_state)
    st["running"] = running
    day = _day_cache_status()
    st["fresh"] = running or _scan_fresh()
    # When the universe was actually last swept (vs. when this process last
    # returned a cached copy), so the UI can say so instead of implying the whole
    # universe is re-scanned on every visit.
    st["scanned_at"] = day["scanned_at"]
    st["scan_day"] = day["scan_day"]
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


def prime_cache(key: str, value) -> None:
    """Seed the memo with an already-computed result. Used after a FORCED sweep so
    the short-TTL memo can't immediately serve back the stale value the force was
    meant to replace."""
    _results[key] = (time.time(), value)


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
    data_handler.prefetch(config.scan_base_frames()
                          + sector_data.sector_etfs() + sector_data.all_tickers())
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
def _stock_row(ticker: str, spy, sector_etf: str,
               regime_green: bool = False, sector_strong: bool = False,
               profile: str | None = None) -> dict:
    """One scan/gate row for a name.

    ``profile`` (schema v21, TRAVIS_EXTENSION) swaps ONLY the vs-peer comparison
    benchmark: a DIVIDEND_COMPOUNDER is measured against the dividend-peer
    benchmark instead of the growth-tilted sector ETF, because a dividend payer
    compared against a growth sector is rejected for being the wrong KIND of stock
    rather than for being a laggard within its own peer group. Everything else on
    this row — the four lights, the vetoes' rules, the right-spot gate, the RS
    vs SPY leg — is identical for both profiles [HARD_CFM_RULE].
    """
    import income_profile
    df = data_handler.get_daily(ticker)
    is_etf = sector_data.is_etf(ticker)
    # The income-profile resolution. Its vs-peer FRAME is no longer fetched: the
    # only consumer was the rs3m/rs1m-vs-sector pair, removed 2026-08-21
    # (docs/decision-2026-08-21-remove-sector-rs.md). The profile and its
    # benchmark NAME stay — they still drive the dividend sleeve's income floors.
    peer = income_profile.resolve(ticker, profile, sector_etf)
    profile, benchmark = peer["profile"], peer["benchmark"]
    is_sector_etf, is_own_benchmark = peer["is_sector_etf"], peer["is_own_benchmark"]

    # RS3M (3-month) is DISPLAY / kill-switch only now — kept on the row so the UI
    # and snapshot still show it, but it no longer gates entry. A sector ETF has
    # no distinct peer sector to beat (tautologically itself), so its vs-sector RS
    # is N/A.
    rs3m_vs_spy = indicators.rs3m(df, spy) if df is not None else None
    # RS1M (1-month) vs SPY is the RANKING key within GREENs, for every name.
    # It used to be rs1m_vs_sector for stocks and rs1m_vs_spy only for ETFs;
    # the vs-sector leg was removed 2026-08-21 with the rest of the
    # sector-relative logic (docs/decision-2026-08-21-remove-sector-rs.md), so
    # stocks and ETFs now rank on one comparable key.
    rs1m_vs_spy = indicators.rs1m(df, spy) if df is not None else None
    atrp = indicators.atr_pct(df) if df is not None else None

    # The per-name Genius lights + vetoes + right-spot gate. The vs-sector veto is
    # waived for ETFs inside stock_lights (an ETF has no growth-leader peer). IVR
    # for the volatility veto is read from the local IV history file.
    try:
        import iv_history
        ivr_percentile = (iv_history.iv_rank(ticker) or {}).get("iv_percentile")
    except Exception:  # noqa: BLE001
        ivr_percentile = None
    sl = stock_lights.compute(df, ivr_percentile=ivr_percentile, is_etf=is_etf)
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
        "rs1m_vs_spy": rs1m_vs_spy,
        "is_sector_etf": is_sector_etf,
        "is_etf": is_etf,
        "atr_pct": atrp,
        # Income profile + its peer benchmark NAME (schema v21). The vs-peer RS
        # legs this benchmark used to measure are gone; the profile still selects
        # the dividend sleeve's income floors, so both stay as provenance.
        "income_profile": profile,
        "peer_benchmark": benchmark,
        "is_own_benchmark": is_own_benchmark,
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
        # SHADOW record (gate recalibration): the mandatory-core light state, the
        "core_green": sl["core_green"],
        "stock_green": stock_green,
        # Back-compat: `consolidating` now means "in the right spot" (the gate that
        # replaced the old single consolidating flag).
        "consolidating": spot["pass"],
        "blocked_by": blocked_by,
        # Ranking key within GREENs — RS1M vs SPY for every name (see above).
        "rank_key": rs1m_vs_spy,
        "status": status,
    }


def stock_filter(sector: str | None = None) -> list[dict]:
    key = f"stock_filter:{(sector or 'ALL').upper()}"
    return _cached(key, lambda: _compute_stock_filter(sector))


def _compute_stock_filter(sector: str | None = None) -> list[dict]:
    etfs = [sector.upper()] if sector else sector_data.sector_etfs()
    # Parallel-warm SPY + the sector ETF(s) + their constituents first.
    # The dividend-peer benchmark is warmed alongside SPY so a DIVIDEND_COMPOUNDER
    # row's comparison frame is a cache hit, not a cold per-request fetch inside
    # the memoized sweep (schema v21).
    universe = config.scan_base_frames() + etfs
    for etf in etfs:
        universe += sector_data.constituents(etf)
    data_handler.prefetch(universe)
    spy = data_handler.get_daily(config.BENCHMARK)
    # Regime + sector strength gate "ready" the same way the entry gate does, so
    # the filter's status agrees with the gate verdict.
    regime_green = regime().get("status") == "green"
    sector_status = sectors()
    # One state read for the whole sweep (schema v21) — resolving the profile per
    # ticker would mean a state.json load per name.
    try:
        import logging_handler as log
        sweep_state = log.load_state()
    except Exception:  # noqa: BLE001 — no state just means no explicit assignments
        sweep_state = None
    rows = []
    for etf in etfs:
        sector_df = data_handler.get_daily(etf)
        sector_strong = sector_status.get(etf, {}).get("status") == "green"
        # The ETF itself is a valid CFM candidate alongside its constituents —
        # liquid, weekly-optionable, and a real entry choice in its own right.
        rows.append(_stock_row(etf, spy, etf,
                               regime_green=regime_green, sector_strong=sector_strong,
                               profile=resolve_profile(etf, state=sweep_state)))
        for ticker in sector_data.constituents(etf):
            rows.append(_stock_row(ticker, spy, etf,
                                   regime_green=regime_green, sector_strong=sector_strong,
                                   profile=resolve_profile(ticker, state=sweep_state)))
    # Ranking (item F): GREENs first, then by the RS1M-vs-SPY rank key
    # descending; None last within a group.
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



def resolve_profile_detail(ticker: str, state: dict | None = None,
                           overrides: dict | None = None) -> tuple[str, float | None]:
    """``(income_profile, trailing_annual_dividend_yield_pct)`` for a scan candidate.

    Resolution: explicit operator assignment -> the trailing-yield heuristic ->
    JUICE_ENGINE (schema v21). Cache-only on the dividend side — a bulk sweep must
    never trigger a fundamentals fetch storm — and an unresolved yield falls back to
    JUICE_ENGINE rather than auto-enrolling a name into the extension.

    Returns the yield alongside the profile because every caller needs both: the
    scan row displays it, the gate feeds it into the combined metric, and resolving
    it twice was costing a second cache read per ticker.

    A BULK caller passes ``state`` (or just ``overrides``) so neither the assignment
    lookup nor the yield's override check re-reads ``state.json`` per ticker."""
    import dividends
    import income_profile
    if state is None and overrides is None:
        try:
            import logging_handler as log
            state = log.load_state()
        except Exception:  # noqa: BLE001 — no state just means no explicit assignments
            state = None
    annual_pct = dividends.cached_annual_yield_pct(ticker, state)
    profile = income_profile.profile_for(ticker, state=state, overrides=overrides,
                                         annual_dividend_yield_pct=annual_pct)
    return profile, annual_pct


def resolve_profile(ticker: str, state: dict | None = None) -> str:
    """The income profile alone — see ``resolve_profile_detail``."""
    return resolve_profile_detail(ticker, state=state)[0]




def entry_gate(ticker: str, profile: str | None = None) -> dict:
    """Evaluate one candidate against the VETO SET, and gather its ranking inputs.

    This is no longer a filter. It used to be a four-level, stop-on-first-fail gate
    whose pass rate collapsed multiplicatively and whose middle levels screened for
    momentum LEADERSHIP — a screen for appreciation, which is the wrong thing to
    want when the upside is capped by a short call. It now answers two separate
    questions and keeps them separate:

      * **May I enter?**  ``scan_verdict.evaluate`` over the thin veto set. Only the
        exit mirrors and hard account constraints block.
      * **How good is it?**  the ``ranking`` block below, consumed by
        ``scan_score.compute_score``. Nothing in it can block.

    The Level-5 account overlay is NOT evaluated here — it needs a state load and a
    live cash resolution per name, which a ~500-name sweep cannot afford. The caller
    layers it (``/api/scan/ready`` via ``account_gate.evaluate_many``), and the
    executor enforces it independently at the ticket. This function's output is
    ADVISORY; ``executor.execute`` remains the sole enforcement point.

    ``profile`` (schema v21) swaps only the vs-peer comparison benchmark.
    """
    ticker = ticker.upper()
    sector_etf = sector_data.sector_for(ticker)
    if profile is None:
        profile = resolve_profile(ticker)

    # ---- Inputs (the only I/O in this function) ------------------------------
    reg = regime()
    regime_color = reg.get("published_regime") or reg.get("status")
    sec = sectors().get(sector_etf, {}) if sector_etf else {}
    spy = data_handler.get_daily(config.BENCHMARK)
    df = data_handler.get_daily(ticker)
    row = _stock_row(ticker, spy, sector_etf or "",
                     regime_green=(regime_color == "green"),
                     sector_strong=True,   # no longer a gate leg; sector now RANKS
                     profile=profile)

    import structure_classifier
    import scan_verdict
    base_stage, inst_flow = structure_classifier.classify_symbol(df)
    entrability = structure_classifier.structure_entrability(base_stage, inst_flow)

    # None-safe throughout: a name with no cached frame (a fresh universe add, a
    # provider outage) must produce an UNKNOWN read, never an exception. The three
    # chart vetoes fail OPEN on None, so an unmeasurable name is eligible-by-
    # default rather than blocked-by-accident — see scan_verdict's missing-data
    # policy. `indicators.sma` is the one helper that is not None-tolerant.
    price = indicators.last(df)
    ma21 = indicators.sma(df, config.MA_WINDOW) if df is not None else None
    extension_atr = indicators.atr_extension(df)
    below_ma50 = below_ma200 = None
    if price is not None and df is not None:
        ma50 = indicators.sma(df, config.GENIUS_SLOW_MA)
        ma200 = indicators.sma(df, stock_lights.MA200_WINDOW)
        below_ma50 = None if ma50 is None else bool(price < ma50)
        below_ma200 = None if ma200 is None else bool(price < ma200)

    # ---- The veto set. This list is the ONLY thing that blocks. ---------------
    blocks = scan_verdict.evaluate(
        regime_color=regime_color,
        rs3m_vs_spy=row.get("rs3m_vs_spy"),
        below_ma50=below_ma50,
        below_ma200=below_ma200,
        price=price,
        line_in_the_sand=None,     # a scan candidate has no stored line; the
                                   # caller supplies one for a name that does
        has_weeklies=row.get("has_weeklies"),
        spread_pct=None,           # resolved at the ticket, not in a bulk sweep
        stale=None,                # layered by the caller from data_cache
        account_gate=None,         # layered by the caller (see docstring)
    )
    composed = scan_verdict.compose(blocks)

    # ---- Ranking inputs. NONE of these may veto. ------------------------------
    # Everything the old Levels 2, 3, 3.5 and 4 blocked on lives here now, as
    # values rather than verdicts. `scan_score.compute_score` consumes this dict
    # directly; keeping the shapes aligned is what stops a ranking input from being
    # re-derived differently in two places.
    spot = row.get("right_spot") or {}
    ranking = {
        "inst_flow": inst_flow,
        "base_stage": base_stage,
        "entrability": entrability,
        "extension_atr": extension_atr,
        "stock_greens": row.get("greens"),
        "rs3m_vs_spy": row.get("rs3m_vs_spy"),
        "sector_rs1m": sec.get("rs1m"),
        "sector_breadth": sec.get("breadth"),
        "sector_atr_expanding": sec.get("atr_expanding"),
        "sector_inst_flow": sec.get("inst_flow"),
        "atr_momentum": row.get("atr_momentum"),
        "atr_pct": row.get("atr_pct"),
        "right_spot": spot,
    }

    return {
        "ticker": ticker,
        "sector": sector_etf,
        "verdict": composed["verdict"],
        "blocks": composed["blocks"],
        "blocked_by": composed["blocked_by"],
        "ranking": ranking,
        # The entry ROUTE — advisory output only. No put order is ever constructed
        # from this; it tells the operator whether today's entry is shares or a
        # weekly put struck at the MA21 zone.
        "route": scan_verdict.route(extension_atr=extension_atr,
                                    regime_color=regime_color, ma21=ma21,
                                    entrability=entrability),
        "regime_color": regime_color,
        # The three provenance blocks `entry_context` freezes onto the immutable
        # execution. A READ of what was already computed above — the snapshot has
        # to capture the values that produced a verdict, and they must come from
        # the same evaluation rather than a second one that could disagree.
        "regime": reg,
        "sector_detail": {"sector": sector_etf, **sec},
        "stock_detail": row,
        "income_profile": row.get("income_profile"),
        "peer_benchmark": row.get("peer_benchmark"),
        # Per-name Genius detail, retained for the drawer + entry_context snapshot.
        # A READ of what `_stock_row` already computed — never a re-evaluation.
        "lights": row.get("lights"),
        "stock_verdict": row.get("verdict"),
        "stock_vetoes": row.get("vetoes"),
    }
