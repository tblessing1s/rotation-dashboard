"""Trailing juice CAPACITY — the structural-vs-transient discriminator (SHADOW).

The scan's weekly juice (``account_gate.juice_estimate`` -> the row's
``juice_weekly_pct``) is a SPOT reading, so it cannot distinguish two very
different kinds of weak juice:

  * TRANSIENT — a normally-juicy name in IV compression (post-earnings crush, a
    quiet regime). The premium comes back.
  * STRUCTURAL — the instrument is BUILT low-vol (midstream MLPs, diversified
    sector ETFs). It never comes back.

The discriminator is not the current reading but the historical CAPACITY: the
trailing median of the achievable combined weekly yield. A compression leaves the
median near the name's long-run level; a structurally-thin name's median was never
above the floor to begin with. Two names both reading 0.40%/wk today are a BUY-
LATER and a NEVER, and only the median tells them apart.

ZERO AUTHORITY
--------------
Everything in this module is SHADOW, on the same contract
``scan_triggers.shadow_floor`` and ``chart_structure`` carry: computed, persisted,
displayed, and never consulted by a decision. Nothing here is appended to the
``blocks`` list that feeds ``scan_triggers.compose_row_verdict`` — that list is
what gives a finding verdict authority, and keeping capacity OUT of it is the
load-bearing invariant. Capacity gates nothing, hides nothing, benches nothing,
ranks nothing and reorders nothing. There is deliberately NO config switch that
would grant it authority; graduating it is a reviewed code change.

WHAT IS MEASURED
----------------
``combined_wk_pct`` = juice%/wk + dividend%/wk, via ``scan_triggers``'
``combined_weekly_yield`` — the SAME function the shadow floor is measured
against, so capacity and the floor can never disagree about what a name yields.
The juice leg is the scan's own GROSS/WK (a Black-Scholes weekly short priced at
trailing realized vol, at a flat ``config.CAPACITY_STRIKE_ATR_MULT`` strike); the
dividend leg is the trailing/declared annual yield / 52. The dividend basis is
recorded per observation (``dividend_basis``) rather than assumed, and an
unresolved yield is persisted as ``dividend_known: False`` with a
``DIVIDEND_STUBBED`` marker — never as a confident zero.

STORAGE
-------
DERIVED market telemetry, recomputable from cached bars — so, exactly like
``iv_history`` / ``regime_history`` / ``symbol_genius_history`` /
``scan_rejection_log``, it lives in a standalone append-only store under
``DATA_DIR`` and NOT in state.json. ``recompute_derived`` keys off the executions
ledger and never rebuilds this.

ONE OBSERVATION PER SYMBOL PER CALENDAR DAY, last write of the day wins
(``iv_history``'s rule, not ``scan_rejection_log``'s append-per-run). The median
is over DATES: under ``config.SCAN_WARM_INTERVAL_MINUTES`` a busy session can
sweep the universe dozens of times, and appending each would let one heavily-
rescanned day outvote twenty quiet ones and silently bias the median. The one
exception to last-write-wins is provenance: a BACKFILLED observation never
overwrites a LIVE one (see ``record_observations``).

The median is a PURE function over the persisted observations — recomputed from
the raw series on every call, never stored as a running value, and the stored
records are never mutated after their day closes. Below
``config.CAPACITY_MIN_OBS`` it returns INSUFFICIENT_HISTORY, never a number.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
from datetime import datetime, timezone

import config

CAPACITY_PATH = os.path.join(config.DATA_DIR, "juice_capacity.json")
_lock = threading.RLock()

# Record schema version — bumped when the persisted observation gains or changes
# fields, so a later pass can tell which records carry which columns rather than
# inferring from absence (the discipline scan_rejection_log.SCHEMA_VERSION sets).
#   1 — juice + dividend legs, combined, strike/spot/atr_mult/regime provenance.
SCHEMA_VERSION = 1

# Capacity is a STATUS, not a nullable float: "we have not measured this yet" and
# "this name yields nothing" are opposite facts and must never share a
# representation. Prompt 2 treats INSUFFICIENT_HISTORY as UNSUPPRESSIBLE.
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
OK = "OK"

# Observation provenance. Live observations are byproducts of a scan the operator
# actually ran; backfilled ones were replayed offline from cached bars and are
# distinguishable from live ones FOREVER (regime_history's `backfilled` marker,
# generalized to a source string).
SOURCE_LIVE = "live"
SOURCE_BACKFILL_BAR_REPLAY = "backfill_bar_replay"

# Marker on an observation whose dividend leg could not be resolved. The leg is
# then contributed as 0.0 — but the marker keeps that distinguishable from a
# genuine non-payer, so a fundamentals outage is never read as a confident zero.
DIVIDEND_STUBBED = "DIVIDEND_STUBBED"

# What the dividend leg is derived from, recorded rather than assumed. The
# quoted trailing annual yield / 52 is what `scan_triggers.combined_weekly_yield`
# already measures the shadow floor against; a realized-distribution series does
# not exist in this tree (dividends.cached_dividend holds the NEXT event, not a
# trailing series), and building one would be a dividend fetcher.
DIVIDEND_BASIS_QUOTED = "quoted_annual_yield"
# A backfilled observation necessarily carries TODAY's yield attached to a PAST
# date — there is no per-symbol dividend-yield history in this tree to read
# as-of. Harmless for the structural-vs-transient question (a structurally
# low-vol payer's yield is stable, which is the point), but it is an anachronism
# and is marked as one rather than passed off as a historical read.
DIVIDEND_BASIS_ANACHRONISTIC = "current_yield_anachronistic"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fmt_day(ts) -> str:
    """A 'YYYY-MM-DD' day string from a pandas Timestamp / datetime / str."""
    try:
        return ts.strftime("%Y-%m-%d")
    except AttributeError:
        return str(ts)[:10]


# Parsed-store memo keyed on the file's (mtime, size) — the same invalidation
# trick data_handler uses for frames and dividends uses for its cache. A sweep
# reads capacity once per row across hundreds of tickers; re-opening and
# re-parsing the whole JSON per ticker would be the dominant cost of the read.
_parsed: tuple[tuple[float, int], dict] | None = None


def _load() -> dict:
    global _parsed
    try:
        stat = os.stat(CAPACITY_PATH)
        stamp = (stat.st_mtime, stat.st_size)
    except OSError:
        return {"symbols": {}}
    if _parsed is not None and _parsed[0] == stamp:
        return _parsed[1]
    try:
        with open(CAPACITY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"symbols": {}}
    if not (isinstance(data, dict) and isinstance(data.get("symbols"), dict)):
        return {"symbols": {}}
    _parsed = (stamp, data)
    return data


def _save(data: dict) -> None:
    global _parsed
    tmp = f"{CAPACITY_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, CAPACITY_PATH)
        _parsed = None            # force a re-read (and a fresh mtime stamp)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Pure extraction — an observation is a READ of an already-computed scan row.
# ---------------------------------------------------------------------------
def observation_from_row(row: dict, *, day: str | None = None,
                         source: str = SOURCE_LIVE) -> dict | None:
    """One persisted observation from one scorecard row, or None when the row
    carries no priceable juice (an unmeasurable name is UNMEASURED, never
    recorded as a zero — the same rule ``shadow_floor`` applies to its ``pass``).

    PURE: a read of values the scan already computed for display. No provider
    call, no recomputation, no clock beyond the caller-supplied ``day``.
    """
    if not isinstance(row, dict):
        return None
    ticker = str(row.get("ticker") or "").strip().upper()
    juice = row.get("juice_weekly_pct")
    if not ticker or juice is None:
        return None

    known = bool(row.get("dividend_known"))
    div_wk = row.get("dividend_weekly_pct") if known else None
    # An unresolved dividend contributes 0.0 — but the marker below keeps that
    # distinguishable from a resolved non-payer forever.
    combined = row.get("combined_weekly_yield_pct")
    if combined is None:
        combined = round(float(juice) + float(div_wk or 0.0), 4)

    obs = {
        "date": day or _today(),
        "schema": SCHEMA_VERSION,
        "source": source,
        # The juice leg — the scan's own GROSS/WK, the number the table shows.
        "achievable_juice_wk_pct": juice,
        # The dividend leg and the combined figure the median is taken over.
        "dividend_wk_pct": div_wk if div_wk is not None else 0.0,
        "dividend_known": known,
        "dividend_basis": (DIVIDEND_BASIS_QUOTED if known else None),
        "combined_wk_pct": combined,
        # Provenance for the strike the juice was priced at, so a future move to a
        # regime-aware basis is a re-derivation over retained inputs rather than a
        # lost history (see config.CAPACITY_STRIKE_ATR_MULT).
        "strike_used": row.get("short_strike"),
        "spot": row.get("price"),
        "atr_mult": config.CAPACITY_STRIKE_ATR_MULT,
        "regime": row.get("regime_color"),
        "income_profile": row.get("income_profile"),
    }
    if not known:
        obs["markers"] = [DIVIDEND_STUBBED]
    return obs


# ---------------------------------------------------------------------------
# Writes — append-only, one observation per symbol per calendar day.
# ---------------------------------------------------------------------------
def _trim_to_days(obs: list[dict], max_days: int) -> list[dict]:
    """Keep only the observations falling in the newest ``max_days`` distinct
    dates. Retention is by DATE, not by record count — the store holds one point
    per date by construction, but trimming by date keeps that an invariant of the
    retention rule rather than a coincidence of the write path."""
    days = sorted({o.get("date", "") for o in obs})
    if len(days) <= max_days:
        return obs
    keep = set(days[-max_days:])
    return [o for o in obs if o.get("date", "") in keep]


def _merge_point(existing: list[dict], point: dict) -> list[dict]:
    """Insert ``point`` into a symbol's series under the day-idempotence rule.

    Last write of the day wins — EXCEPT that a BACKFILLED observation never
    overwrites a LIVE one. A live point was taken from a scan the operator
    actually ran; a replayed one is a reconstruction, and letting the
    reconstruction win would quietly rewrite real history.
    """
    day = point.get("date")
    for i, prior in enumerate(existing):
        if prior.get("date") != day:
            continue
        if (point.get("source") != SOURCE_LIVE
                and prior.get("source") == SOURCE_LIVE):
            return existing               # keep the live point
        out = list(existing)
        out[i] = point
        return out
    out = list(existing)
    out.append(point)
    out.sort(key=lambda o: o.get("date", ""))
    return out


def record_observations(rows: list[dict], day: str | None = None,
                        source: str = SOURCE_LIVE,
                        max_days: int | None = None) -> dict:
    """Persist one observation per scan row, in one load/save.

    Idempotent per symbol per calendar day: re-running the same day's scan
    replaces that day's point with identical values rather than appending a
    second one (see ``_merge_point`` for the one provenance exception).

    Best-effort — a malformed row is skipped and an I/O failure is reported, never
    raised, so a telemetry append can never sink the sweep that called it. Returns
    ``{ok, recorded, skipped, day}``.
    """
    max_days = max_days or config.CAPACITY_RETENTION_DAYS
    day = day or _today()
    try:
        with _lock:
            data = _load()
            # _load may hand back the memoized dict; copy before mutating so the
            # memo is never edited in place under another reader.
            symbols = {k: list(v) for k, v in data.get("symbols", {}).items()}
            recorded = skipped = 0
            for row in rows or []:
                obs = observation_from_row(row, day=day, source=source)
                if obs is None:
                    skipped += 1
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                merged = _merge_point(symbols.get(ticker, []), obs)
                symbols[ticker] = _trim_to_days(merged, max_days)
                recorded += 1
            _save({"schema": SCHEMA_VERSION, "symbols": symbols})
        return {"ok": True, "recorded": recorded, "skipped": skipped, "day": day}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Reads — the capacity metric, PURE over the persisted observations.
# ---------------------------------------------------------------------------
def series(symbol: str) -> list[dict]:
    """Every stored observation for one symbol, chronological (oldest first)."""
    return list(_load()["symbols"].get((symbol or "").strip().upper(), []))


def juice_capacity_wk_pct(symbol: str, *,
                          window_days: int | None = None,
                          min_obs: int | None = None) -> float | str:
    """The capacity number, or the INSUFFICIENT_HISTORY sentinel.

    The median of ``combined_wk_pct`` over the trailing ``window_days``
    observations. The MEDIAN, not the mean: it is what makes the metric a
    CAPACITY read rather than a recent-history read — fifteen compressed weeks
    inside a year of normal premium move a mean materially and a median barely at
    all, which is exactly the transient-vs-structural discrimination this exists
    for.

    Returns the sentinel string ``INSUFFICIENT_HISTORY`` — never a number, and
    never None — below ``min_obs`` observations. Callers must not coerce it: "not
    measured yet" and "yields nothing" are opposite facts, and prompt 2 treats the
    sentinel as unsuppressible.

    PURE over the persisted observations: recomputed from the raw series on every
    call, never read from a stored aggregate.
    """
    window = config.CAPACITY_WINDOW_DAYS if window_days is None else window_days
    floor_n = config.CAPACITY_MIN_OBS if min_obs is None else min_obs
    values = [o["combined_wk_pct"] for o in series(symbol)
              if o.get("combined_wk_pct") is not None]
    # The window is the newest `window` OBSERVATIONS. The store holds one point
    # per date, so that is equivalently the newest `window` distinct dates —
    # robust to holidays and gaps in a way a calendar-day cut is not, and the same
    # convention indicators.hv_rank's 252-day lookback uses.
    values = values[-window:]
    if len(values) < floor_n:
        return INSUFFICIENT_HISTORY
    return round(float(statistics.median(values)), 4)


def capacity(symbol: str, *, floor_pct: float | None = None,
             window_days: int | None = None,
             min_obs: int | None = None) -> dict:
    """The full capacity readout for one symbol — the display/telemetry shape.

    ``floor_pct`` is the SHADOW income floor this capacity should be read against
    (``row["shadow_floor"]["floor_pct"]`` — share-denominated, profile-aware).
    Passed in rather than looked up so this stays pure and the readout can never
    disagree with the floor shown elsewhere on the same row. NOTE it is
    deliberately NOT ``row["juice_target_pct"]``, which is the LEAP-denominated
    bar (~1.9%/wk) and is not on the same scale as a share-notional capacity.

    ``clears_floor`` is an OBSERVATION, not a verdict: it moves nothing. None when
    either side is unknown.
    """
    symbol = (symbol or "").strip().upper()
    rows = series(symbol)
    window = config.CAPACITY_WINDOW_DAYS if window_days is None else window_days
    floor_n = config.CAPACITY_MIN_OBS if min_obs is None else min_obs
    measured = [o for o in rows if o.get("combined_wk_pct") is not None][-window:]
    value = juice_capacity_wk_pct(symbol, window_days=window, min_obs=floor_n)
    insufficient = value == INSUFFICIENT_HISTORY

    clears = None
    if not insufficient and floor_pct is not None:
        clears = bool(value >= floor_pct)

    return {
        "symbol": symbol,
        # None when unmeasured — `status` carries the reason. A reader must key
        # off `status`, never off a null capacity.
        "capacity_wk_pct": None if insufficient else value,
        "status": INSUFFICIENT_HISTORY if insufficient else OK,
        "obs": len(measured),
        "window_days": window,
        "min_obs": floor_n,
        "floor_pct": floor_pct,
        "clears_floor": clears,
        "first_obs": measured[0]["date"] if measured else None,
        "last_obs": measured[-1]["date"] if measured else None,
        "sources": sorted({o.get("source") for o in measured if o.get("source")}),
        "dividend_stubbed_obs": sum(1 for o in measured if not o.get("dividend_known")),
        # SHADOW is a literal, not a flag read from config: there is no switch
        # that can make this blocking, and a reader (UI, log, test) can rely on
        # that — the same statement scan_triggers.shadow_floor makes.
        "shadow": True,
        "blocking": False,
    }


# ---------------------------------------------------------------------------
# Backfill (bootstrap) — replayed from cached bars, so legitimate here
# ---------------------------------------------------------------------------
def backfill(tickers: list[str] | None = None, *, force: bool = False,
             step: int = 1, max_days: int | None = None) -> dict:
    """Bootstrap the capacity history by REPLAYING the live juice computation over
    each symbol's cached daily bars.

    This is not an approximation of the metric — it IS the metric. The scan's
    juice (``account_gate.juice_estimate``) is a pure function of the daily frame:
    a Black-Scholes weekly short priced at trailing REALIZED vol, with no IV
    input, no provider call, no state and no clock. So evaluating it on the prefix
    ``df.iloc[:i+1]`` yields the number the scan WOULD have shown on that date.

    ANCHORING: every replay starts at bar 0. Wilder ATR is an EWM seeded from the
    first bar, so it is prefix-causal only across prefixes sharing bar 0 — a
    shifted start re-seeds and diverges (measurably, if slightly). This is the
    same canonical-start rule ``regime_history.backfill`` states and the SAR
    regression tests pin.

    TWO DISCLOSED ANACHRONISMS, both marked on the records rather than hidden:
      * DIVIDEND — there is no per-symbol dividend-yield HISTORY in this tree, so
        a backfilled observation carries TODAY's yield against a past date. Marked
        ``dividend_basis: current_yield_anachronistic``. The juice and dividend
        legs stay separable on the record, so a real distribution history could
        later re-derive the combined figure without invalidating the juice series.
      * REGIME — recorded best-effort from ``regime_history`` as-of the date, and
        left None where that store has no record. It is provenance only: the
        replayed strike is the same flat ``config.CAPACITY_STRIKE_ATR_MULT`` the
        live scan uses, so the regime does not enter the arithmetic.

    Backfilled points NEVER overwrite live ones (``_merge_point``). ``force``
    re-replays symbols that already have backfilled history; without it a symbol
    whose store already holds backfilled observations is skipped.

    Offline/opt-in: this walks every cached bar for every named symbol and is NOT
    wired into the nightly sweep or any request path. Best-effort — never raises.
    Returns a per-run summary.
    """
    try:
        import account_gate
        import data_handler
        import dividends
        import scan_triggers
        import sector_data

        names = [str(t).strip().upper() for t in (tickers or sector_data.all_tickers())
                 if str(t).strip()]
        max_days = max_days or config.CAPACITY_RETENTION_DAYS
        step = max(1, int(step))

        # The published regime per date, if the store has been populated. Built
        # once, not per bar. Best-effort: an empty store just leaves regime None.
        regime_by_day: dict[str, str | None] = {}
        try:
            import regime_history
            regime_by_day = {r.get("date"): r.get("published_regime")
                             for r in regime_history.series()}
        except Exception:  # noqa: BLE001 — regime is provenance, never arithmetic
            regime_by_day = {}

        summary = {"ok": True, "symbols": 0, "observations": 0,
                   "skipped": [], "source": SOURCE_BACKFILL_BAR_REPLAY}
        with _lock:
            data = _load()
            symbols = {k: list(v) for k, v in data.get("symbols", {}).items()}

            for ticker in names:
                have = symbols.get(ticker, [])
                if not force and any(o.get("source") == SOURCE_BACKFILL_BAR_REPLAY
                                     for o in have):
                    summary["skipped"].append({"ticker": ticker, "why": "already backfilled"})
                    continue
                df = data_handler.get_daily(ticker)
                if df is None or df.empty:
                    summary["skipped"].append({"ticker": ticker, "why": "no cached bars"})
                    continue

                # Today's dividend yield, resolved ONCE per symbol — the
                # anachronism above. Cache-only by contract; never a fetch.
                try:
                    annual_div_pct = dividends.cached_annual_yield_pct(ticker)
                except Exception:  # noqa: BLE001 — an unknown yield is not a zero
                    annual_div_pct = None

                emitted = 0
                # Warm-up: juice_estimate needs ATR (ATR_WINDOW + 1 bars) and a
                # 20-bar realized vol (21 bars). It returns an all-None estimate
                # below that, which is skipped rather than recorded as a zero.
                start = max(config.ATR_WINDOW + 1, 21)
                for i in range(start, len(df), step):
                    est = account_gate.juice_estimate(ticker, df.iloc[: i + 1])
                    juice = est.get("weekly_yield_pct")
                    if juice is None:
                        continue
                    day = _fmt_day(df.index[i])
                    parts = scan_triggers.combined_weekly_yield(juice, annual_div_pct)
                    known = bool(parts["dividend_known"])
                    obs = {
                        "date": day,
                        "schema": SCHEMA_VERSION,
                        "source": SOURCE_BACKFILL_BAR_REPLAY,
                        "achievable_juice_wk_pct": juice,
                        "dividend_wk_pct": parts["dividend_weekly_pct"] or 0.0,
                        "dividend_known": known,
                        "dividend_basis": (DIVIDEND_BASIS_ANACHRONISTIC if known else None),
                        "combined_wk_pct": parts["combined_weekly_yield_pct"],
                        "strike_used": est.get("short_strike"),
                        "spot": est.get("stock_price"),
                        "atr_mult": config.CAPACITY_STRIKE_ATR_MULT,
                        "regime": regime_by_day.get(day),
                    }
                    if not known:
                        obs["markers"] = [DIVIDEND_STUBBED]
                    have = _merge_point(have, obs)
                    emitted += 1

                symbols[ticker] = _trim_to_days(have, max_days)
                summary["symbols"] += 1
                summary["observations"] += emitted

            _save({"schema": SCHEMA_VERSION, "symbols": symbols})
        return summary
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def summary(window: int | None = None) -> dict:
    """A calibration-oriented rollup over the store: how many names are measurable
    yet, and how their capacity sits against the shares juice floor.

    Like ``scan_rejection_log.summary`` this is the EVIDENCE a future decision to
    give capacity authority would rest on; reporting it grants none. The floor
    used here is the JUICE_ENGINE share floor — a single reference line for the
    rollup, not the per-name profile-aware floor the row displays.
    """
    floor = config.SHARES_JUICE_FLOOR_PCT
    data = _load()["symbols"]
    measurable = insufficient = below = at_or_above = 0
    total_obs = 0
    by_source: dict[str, int] = {}
    for ticker, obs in data.items():
        total_obs += len(obs)
        for o in obs:
            src = o.get("source") or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
        value = juice_capacity_wk_pct(ticker, window_days=window)
        if value == INSUFFICIENT_HISTORY:
            insufficient += 1
            continue
        measurable += 1
        if value < floor:
            below += 1
        else:
            at_or_above += 1
    return {
        "symbols": len(data),
        "observations": total_obs,
        "observations_by_source": dict(sorted(by_source.items())),
        "measurable": measurable,
        "insufficient_history": insufficient,
        "reference_floor_pct": floor,
        "below_floor": below,
        "at_or_above_floor": at_or_above,
        "window_days": config.CAPACITY_WINDOW_DAYS if window is None else window,
        "min_obs": config.CAPACITY_MIN_OBS,
        # Counterfactual, exactly like the shadow-floor rollup: capacity suppresses
        # nothing today. This is the record a future decision would rest on.
        "shadow": True,
        "blocking": False,
    }
