"""Trailing juice CAPACITY — the median of what a name has been ABLE to pay.

TRAVIS_EXTENSION. SHADOW ONLY, and more strictly so than the income floors: this
module has ZERO consumers outside display, its own observation store, and tests.
It does not gate, hide, rank, bench or reorder anything, and no config switch
exists that would let it. Nothing here is ever appended to the ``blocks`` list
that feeds ``scan_triggers.compose_row_verdict`` — the same load-bearing
invariant the shadow floors carry.

WHY IT EXISTS
-------------
The scan's weekly juice reading answers "what does this name pay THIS week". It
cannot tell apart two names that both read 0.35%/wk today:

  * a normally-juicy name in IV compression (post-earnings crush, a quiet
    regime) — recoverable, and it will pay again;
  * an instrument that is simply built low-vol (a midstream MLP, a diversified
    sector ETF) — never recoverable, and no amount of waiting changes it.

The discriminator is not the current reading but the trailing MEDIAN of the
readings: what the name has demonstrated it CAN pay. That median is capacity.
A compressed name carries a high capacity and a low current; a structurally
low-vol name carries both low.

WHAT AN OBSERVATION IS
----------------------
One record per symbol per scan DAY:

    {symbol, date, achievable_juice_wk_pct, strike_used, regime,
     dividend_wk_pct, combined_wk_pct, dividend_known, source, schema}

``combined_wk_pct`` is the juice leg plus the dividend leg's weekly equivalent,
computed by ``scan_triggers.combined_weekly_yield`` — the SAME function the scan
row uses, so a capacity median and a displayed combined yield can never disagree
about what "combined" means.

ONE PER SYMBOL PER DAY [CAPACITY_ONE_PER_DAY]. The store follows
``iv_history``'s convention (last write of the day wins), NOT
``scan_rejection_log``'s append-per-scan-run. A regime flip re-fingerprints the
scan cache and forces a second sweep the same session; letting that day
contribute several points would weight the median toward exactly the days juice
is least representative. ``CAPACITY_MIN_OBS`` counts DISTINCT DAYS for the same
reason.

SOURCES, AND WHY THEY STAY DISTINGUISHABLE FOREVER
--------------------------------------------------
``SOURCE_LIVE``    — emitted by the nightly sweep from the row the scan just
                     computed. Carries a real dividend leg.
``SOURCE_SEED``    — recovered from ``scan_rejection_log``, which has been
                     persisting ``combined_weekly_yield_pct`` per candidate as
                     gate-calibration telemetry since schema v21. These were
                     computed live at the time and carry a real dividend leg;
                     they are seeded, not synthesized.
``SOURCE_BACKFILL`` — replayed offline from cached daily bars (see ``backfill``).

The backfilled leg is JUICE-ONLY: no dividend-yield history exists anywhere in
the tree (``dividends`` caches one current value per ticker on a 24h TTL), so a
backfilled ``combined_wk_pct`` equals its juice leg and is flagged
``dividend_known: False``. It is never a silent zero — the same "unknown is not
zero" rule ``dividends.cached_annual_yield_pct`` and
``scan_triggers.combined_weekly_yield`` already enforce.

That matters directionally: for a dividend payer the dividend leg can be most of
the combined number, so a backfill-heavy median UNDERSTATES a payer's capacity.
``capacity_detail`` therefore reports the per-source counts alongside the median
rather than a single opaque figure, so a later consumer can require live
provenance before treating a low capacity as structural.

STORAGE
-------
A standalone append-only JSON store under ``DATA_DIR`` — market observations, not
a trading record, so like ``iv_history`` / ``regime_history`` /
``scan_rejection_log`` it stays OUT of ``state.json`` and is not rebuilt by
``recompute_derived`` (which keys off the executions ledger). The median is
recomputed from the stored observations on every read; no running aggregate is
ever persisted, so capacity is always reproducible from history.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

LOG_PATH = os.path.join(config.DATA_DIR, "juice_capacity_log.json")
_lock = threading.RLock()

# Observation schema version. Bumped when the persisted record gains or changes
# fields, so a later pass can tell which observations carry which columns rather
# than inferring from absence.
#   1 — juice + dividend legs, strike/regime provenance, source tagging.
SCHEMA_VERSION = 1

# Capacity is never a number below the minimum-observations guard. A sentinel
# STRING, not None: None is what an unpriceable name returns, and "we have not
# watched this name long enough" must never be confused with "this name cannot
# be priced". Consumers branch on identity, not truthiness.
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

SOURCE_LIVE = "live"
SOURCE_SEED = "seed_scan_rejection_log"
SOURCE_BACKFILL = "backfill_bar_replay"

# Bars a replayed as-of date needs before juice_estimate can price it: 21 closes
# for the 20-day realized vol (indicators.hist_vol) and ATR_WINDOW+1 for the
# Wilder ATR. Derived rather than hardcoded so a window change can't silently
# leave the replay pricing off a half-warm indicator.
_HV_WINDOW_BARS = 21


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Parsed-store memo, keyed on the file's path+mtime+size — the same invalidation
# trick `dividends` and `scan_cache` use. A full-universe sweep asks for a
# capacity readout per ticker; re-opening and re-parsing the whole store ~500
# times per sweep would be the dominant cost of a display-only metric.
_parsed: tuple[tuple, dict] | None = None


def _load_raw() -> dict:
    """A fresh parse straight off disk. Writers use this: the memo below hands
    back a SHARED dict, and mutating that in place would let a concurrent reader
    observe a half-applied batch."""
    try:
        with open(LOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"symbols": {}}


def _load() -> dict:
    """The store, re-parsed only when the file actually changed. READ PATH ONLY —
    the returned dict is shared, so callers must treat it as immutable."""
    global _parsed
    try:
        st = os.stat(LOG_PATH)
        stamp = (LOG_PATH, st.st_mtime_ns, st.st_size)
    except OSError:
        _parsed = None
        return {"symbols": {}}
    memo = _parsed
    if memo is not None and memo[0] == stamp:
        return memo[1]
    data = _load_raw()
    _parsed = (stamp, data)
    return data


def _save(data: dict) -> None:
    tmp = f"{LOG_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, LOG_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _trim_to_days(obs: list[dict], max_days: int) -> list[dict]:
    """Keep only the newest ``max_days`` distinct observation dates."""
    days = sorted({o.get("date", "") for o in obs})
    if len(days) <= max_days:
        return obs
    keep = set(days[-max_days:])
    return [o for o in obs if o.get("date", "") in keep]


# ---------------------------------------------------------------------------
# Building an observation — PURE
# ---------------------------------------------------------------------------
def observation(symbol: str,
                day: str,
                juice_wk_pct: float | None,
                annual_dividend_yield_pct: float | None = None,
                *,
                strike_used: float | None = None,
                regime: str | None = None,
                source: str = SOURCE_LIVE) -> dict | None:
    """One capacity observation, or None when the name can't be priced.

    The combined leg goes through ``scan_triggers.combined_weekly_yield`` — the
    SAME function the scan row uses — so the median can never be computed on a
    different definition of "combined" than the one displayed. An unresolved
    dividend yield stays UNKNOWN (``dividend_known: False``, ``dividend_wk_pct``
    None) and contributes nothing, rather than being recorded as a confident 0.

    A None juice leg yields no observation at all: an unpriceable name is
    unmeasured, never a recorded zero that would drag its own median down. PURE.
    """
    import scan_triggers

    symbol = (symbol or "").strip().upper()
    if not symbol or juice_wk_pct is None:
        return None
    parts = scan_triggers.combined_weekly_yield(juice_wk_pct,
                                                annual_dividend_yield_pct)
    return {
        "symbol": symbol,
        "date": day,
        "schema": SCHEMA_VERSION,
        "achievable_juice_wk_pct": parts["juice_weekly_pct"],
        "dividend_wk_pct": parts["dividend_weekly_pct"],
        "combined_wk_pct": parts["combined_weekly_yield_pct"],
        "dividend_known": parts["dividend_known"],
        "annual_dividend_yield_pct": parts["annual_dividend_yield_pct"],
        "strike_used": strike_used,
        "regime": regime,
        "source": source,
    }


def observation_from_row(row: dict, day: str, regime: str | None = None,
                         source: str = SOURCE_LIVE) -> dict | None:
    """A capacity observation READ off an already-computed scan row.

    Every input is a value the sweep has already produced — no recomputation, no
    provider call, and no second opinion about what this name's juice is."""
    return observation(
        row.get("ticker"), day,
        row.get("juice_weekly_pct"),
        row.get("annual_dividend_yield_pct"),
        strike_used=row.get("short_strike"),
        regime=regime,
        source=source,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _merge(obs_by_symbol: dict[str, dict], *, overwrite: bool) -> int:
    """Merge one observation per symbol into the store under the lock.

    ``overwrite`` is the live-vs-synthesized distinction [CAPACITY_ONE_PER_DAY]:
    a LIVE emission replaces that day's point (last write of the day wins, as in
    ``iv_history``), while a backfill or seed only FILLS GAPS — synthesized
    history must never overwrite an observation actually taken that day.
    """
    n = 0
    with _lock:
        data = _load_raw()
        for symbol, obs in obs_by_symbol.items():
            recs = data["symbols"].setdefault(symbol, [])
            day = obs.get("date")
            at = next((i for i, r in enumerate(recs) if r.get("date") == day), None)
            if at is not None:
                if not overwrite:
                    continue
                recs[at] = obs
            else:
                recs.append(obs)
                recs.sort(key=lambda r: r.get("date", ""))
            data["symbols"][symbol] = _trim_to_days(recs, config.CAPACITY_LOG_DAYS)
            n += 1
        _save(data)
    return n


def record_scan(rows: list[dict], day: str | None = None,
                regime: str | None = None) -> dict:
    """Emit one live observation per scan row, in a single load/save.

    Called by the nightly maintenance sweep off the rows it has already
    computed — a pure byproduct, adding no provider call to a sweep that has
    already priced every name. Best-effort: a malformed row is skipped and an
    unpriceable one produces no observation, so a telemetry append can never
    sink the sweep that called it. Returns {ok, recorded, day}."""
    day = day or _today()
    try:
        batch: dict[str, dict] = {}
        for row in rows or []:
            obs = observation_from_row(row, day, regime=regime, source=SOURCE_LIVE)
            if obs is not None:
                batch[obs["symbol"]] = obs
        return {"ok": True, "recorded": _merge(batch, overwrite=True), "day": day}
    except Exception as e:  # noqa: BLE001 — telemetry must never sink its caller
        return {"ok": False, "error": str(e), "day": day}


# ---------------------------------------------------------------------------
# Reads — the metric itself
# ---------------------------------------------------------------------------
def series(symbol: str) -> list[dict]:
    """Every stored observation for one symbol, chronological (oldest first)."""
    return list(_load()["symbols"].get((symbol or "").strip().upper(), []))


def _median(values: list[float]) -> float | None:
    """Median of a non-empty list; the mean of the middle pair when even."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _windowed(obs: list[dict]) -> list[dict]:
    """The observations falling in the newest ``CAPACITY_WINDOW_DAYS`` distinct
    dates. Windowing by DATE rather than by record count means a day that
    somehow carries two points can never shorten the effective window."""
    days = sorted({o.get("date", "") for o in obs if o.get("date")})
    if len(days) <= config.CAPACITY_WINDOW_DAYS:
        keep = set(days)
    else:
        keep = set(days[-config.CAPACITY_WINDOW_DAYS:])
    return [o for o in obs if o.get("date", "") in keep]


def juice_capacity_wk_pct(symbol: str):
    """The trailing median combined weekly-equivalent yield for ``symbol``, in
    percent — or ``INSUFFICIENT_HISTORY`` when fewer than
    ``config.CAPACITY_MIN_OBS`` DISTINCT observation days are stored.

    Never returns a number the sample can't support: below the guard the answer
    is the sentinel, not a provisional median that a consumer might round-trip
    into a decision. PURE over the persisted observations — no clock beyond the
    stored dates, no provider call, and no cached aggregate; the median is
    recomputed on every read so it always reflects the history on disk."""
    return capacity_detail(symbol)["capacity"]


def capacity_detail(symbol: str, profile: str | None = None) -> dict:
    """``juice_capacity_wk_pct`` plus everything a reader needs to judge it.

    Returns {capacity, insufficient_history, obs_days, obs, window_days,
    min_obs, by_source, floor_pct, floor_basis, current_wk_pct, symbol}.

    ``by_source`` is the per-source observation-day count. It is reported
    alongside the median rather than folded into it because the three sources
    are not interchangeable: a backfilled point carries no dividend leg (see the
    module docstring), so a median dominated by backfill understates a dividend
    payer. A consumer that wants to act on a low capacity can require live
    provenance; one that only displays it does not have to care.

    ``floor_pct`` is the PROFILE-AWARE income floor this name's yield is already
    judged against — read through ``scan_triggers.floor_for_profile``, the same
    resolution ``shadow_floor`` uses, so the capacity readout and the shadow
    floor can never quote different bars for one name."""
    import scan_triggers

    symbol = (symbol or "").strip().upper()
    obs = _windowed(series(symbol))
    by_day: dict[str, dict] = {}
    for o in obs:
        by_day[o.get("date", "")] = o          # one per day; last wins on reads too
    points = [o for o in by_day.values() if o.get("combined_wk_pct") is not None]

    by_source: dict[str, int] = {}
    for o in points:
        src = o.get("source") or SOURCE_LIVE
        by_source[src] = by_source.get(src, 0) + 1

    enough = len(points) >= config.CAPACITY_MIN_OBS
    capacity = (round(_median([o["combined_wk_pct"] for o in points]), 4)
                if enough else INSUFFICIENT_HISTORY)
    newest = max(by_day) if by_day else None
    floor = scan_triggers.floor_for_profile(profile)
    return {
        "symbol": symbol,
        "capacity": capacity,
        "insufficient_history": not enough,
        "obs": len(points),
        "obs_days": len(by_day),
        "window_days": config.CAPACITY_WINDOW_DAYS,
        "min_obs": config.CAPACITY_MIN_OBS,
        "by_source": by_source,
        "floor_pct": floor["floor_pct"],
        "floor_basis": floor["basis"],
        "latest_date": newest,
        "current_wk_pct": by_day[newest]["combined_wk_pct"] if newest else None,
        # SHADOW is a literal, not a flag read from config: no switch can make
        # this authoritative, and a reader (UI, log, test) can rely on that.
        "shadow": True,
        "blocking": False,
    }


# ---------------------------------------------------------------------------
# Backfill — offline replay from cached bars
# ---------------------------------------------------------------------------
def backfill(tickers: list[str] | None = None, force: bool = False,
             step: int | None = None) -> dict:
    """Bootstrap the observation history by replaying the juice computation over
    cached daily bars.

    LEGITIMACY. This is a full replay of DERIVED data, the same standard
    ``regime_history.backfill`` is held to: the scan's juice number is computed
    entirely from daily bars (Wilder ATR -> strike, 20-day realized vol ->
    sigma, Black-Scholes -> weekly extrinsic, over spot), with NO option-chain
    input at any point. So replaying ``account_gate.juice_estimate`` against
    ``df.iloc[:i+1]`` does not approximate the historical reading — it
    reproduces the number the live scan WOULD have printed on that date. The
    HV-for-IV substitution people reach for as the error term here is already
    the live metric's own convention (see ``iv_history``'s docstring), so the
    replay introduces no approximation the live series doesn't already carry.

    What it CANNOT reproduce is the dividend leg — no yield history exists — so
    every replayed observation is juice-only and flagged ``dividend_known:
    False``. See the module docstring on why that is reported rather than
    smoothed over.

    Offline: reads the parquet/bar cache only, never a provider. Gap-filling —
    an existing observation for a date is never overwritten, so a backfill run
    after live data has accrued cannot clobber a real reading. No-ops for a
    symbol that already has history unless ``force``. Best-effort; never raises.
    Returns {ok, symbols, recorded, skipped}."""
    import account_gate
    import data_handler
    import sector_data

    step = max(int(step or config.CAPACITY_BACKFILL_STEP), 1)
    names = tickers or sector_data.all_tickers()
    warmup = max(_HV_WINDOW_BARS, config.ATR_WINDOW + 1)
    recorded = 0
    skipped: list[str] = []
    try:
        existing = _load_raw()["symbols"]
        for raw in names:
            t = (raw or "").strip().upper()
            if not t:
                continue
            if existing.get(t) and not force:
                skipped.append(t)
                continue
            df = data_handler.get_daily(t)
            if df is None or len(df) <= warmup:
                skipped.append(t)
                continue
            batch: dict[str, dict] = {}
            for i in range(warmup, len(df), step):
                # Anchored at index 0, never a rolling sub-window: every as-of
                # date sees the same history the live scan would have seen.
                est = account_gate.juice_estimate(t, df.iloc[: i + 1])
                obs = observation(
                    t, str(df.index[i])[:10], est.get("weekly_yield_pct"),
                    # No dividend history exists to replay — UNKNOWN, not zero.
                    None,
                    strike_used=est.get("short_strike"),
                    # The replayed strike is regime-blind (config.SHORT_ATR_MULT),
                    # exactly as the live scan's is, so no regime is reconstructed
                    # for a value it could not have influenced.
                    regime=None,
                    source=SOURCE_BACKFILL,
                )
                if obs is not None:
                    batch[obs["date"]] = obs
            # Saved one symbol at a time: a universe-wide replay accumulated
            # entirely in memory before a single save would risk losing all of
            # it, and would hold the lock for the whole sweep.
            recorded += _merge_days(t, batch)
        return {"ok": True, "symbols": len(names) - len(skipped),
                "recorded": recorded, "skipped": skipped}
    except Exception as e:  # noqa: BLE001 — a backfill failure is never fatal
        return {"ok": False, "error": str(e), "recorded": recorded,
                "skipped": skipped}


def _merge_days(symbol: str, obs_by_day: dict[str, dict]) -> int:
    """Gap-fill many days for ONE symbol in a single load/save. Never overwrites
    an existing observation — synthesized history yields to a real reading."""
    if not obs_by_day:
        return 0
    n = 0
    with _lock:
        data = _load_raw()
        recs = data["symbols"].setdefault(symbol, [])
        have = {r.get("date") for r in recs}
        for day, obs in obs_by_day.items():
            if day in have:
                continue
            recs.append(obs)
            n += 1
        recs.sort(key=lambda r: r.get("date", ""))
        data["symbols"][symbol] = _trim_to_days(recs, config.CAPACITY_LOG_DAYS)
        _save(data)
    return n


def seed_from_scan_rejection_log(tickers: list[str] | None = None) -> dict:
    """Recover real observations from the gate-calibration telemetry.

    ``scan_rejection_log`` has been persisting ``combined_weekly_yield_pct`` per
    candidate per scan run since schema v21 — those are LIVE readings, computed
    at the time with a real dividend leg, that predate this store. Seeding them
    is recovery, not synthesis, which is why they carry their own source tag
    rather than the backfill one.

    That log is append-per-scan-RUN, so a day may hold several records; the last
    record of each day is taken, collapsing to one observation per day
    [CAPACITY_ONE_PER_DAY]. Gap-filling: an existing observation is never
    overwritten. Best-effort; never raises."""
    import scan_rejection_log

    recorded = 0
    try:
        names = tickers or list(scan_rejection_log._load()["symbols"].keys())
        for raw in names:
            t = (raw or "").strip().upper()
            if not t:
                continue
            by_day: dict[str, dict] = {}
            for rec in scan_rejection_log.series(t):
                day = rec.get("date")
                combined = rec.get("combined_weekly_yield_pct")
                juice = rec.get("juice_weekly_pct")
                if not day or (combined is None and juice is None):
                    continue
                div_wk = rec.get("dividend_weekly_pct")
                obs = observation(
                    t, day, juice,
                    # The log stores the dividend leg already divided by 52; the
                    # observation builder wants the annual rate, so it is
                    # multiplied back rather than re-derived from a second
                    # source that could disagree.
                    None if div_wk is None else div_wk * config.DIVIDEND_WEEKS_PER_YEAR,
                    source=SOURCE_SEED,
                )
                if obs is not None:
                    by_day[day] = obs          # later record of the day wins
            recorded += _merge_days(t, by_day)
        return {"ok": True, "recorded": recorded}
    except Exception as e:  # noqa: BLE001 — a seed failure is never fatal
        return {"ok": False, "error": str(e), "recorded": recorded}
