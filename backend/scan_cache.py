"""Day-scoped, disk-backed cache for the full-universe scan sweep.

WHY THIS EXISTS. The scorecard sweep (~500 names x symbol-genius + structure
classifier + the 4-level entry gate + indicator math) used to be memoized only in
process memory on a 5-minute TTL, with the alert scheduler re-warming it every
``SCAN_WARM_INTERVAL_MINUTES``. That recomputed the whole universe roughly every
8 minutes through the session — tens of sweeps a day — over daily OHLCV frames
that ``data_handler`` itself only refreshes every 12 hours. The sweep was being
redone constantly against inputs that had not moved.

Worse, an in-memory memo dies with the process. The Fly config auto-stops
machines, so every restart/deploy dropped the cache and handed the next visitor a
cold ~500-name sweep on the request path — the "stocks won't load" stall.

WHAT THIS DOES. Persist ONE sweep per trading day to the volume, so the universe
is scanned once a day instead of dozens of times, and a restart re-reads the last
sweep instead of recomputing it.

THE SCAN DAY. One sweep per TRADING DAY, run OUTSIDE trading hours: the key rolls
at ``SCAN_ROLL_ET`` (just after the close), so the sweep the scheduler runs at the
post-close slot reads that session's final bars and then serves the evening, the
next pre-market, and the whole next session. Weekends and holidays keep pointing
at the last completed session, so a Saturday visit replays Friday's sweep instead
of paying for an identical one.

THE FINGERPRINT. Anything that would make yesterday's answer *wrong* rather than
merely old is folded into the key, so a change re-scans instead of serving a
stale row: the market regime (a RED regime forces every verdict to BLOCKED, so
the composed verdicts must track it) and the demo/live mode. A miss on either is
a fresh sweep, which is the correct — and rare — cost.

The ticker universe is deliberately NOT in the fingerprint. It used to be, and
that made every universe edit an all-or-nothing invalidation: adding ONE name
discarded all ~500 cached rows and forced a full cold sweep on the request path,
where the browser's 60s abort was waiting. The cost was proportional to the
universe, not to the edit. Since rows are independent (see below), a universe
change is handled by ROW SET instead — ``reusable`` serves the rows that are
still in the universe, names with no row are computed and merged in, and rows for
removed names are dropped. Only a genuinely global input re-scans everything.

PER-STOCK REFRESHES. Rows are INDEPENDENT of one another: refreshing one stock
(or one sector) intraday writes that row straight back here via ``patch_rows``, so
it survives a reload and everyone else's rows are untouched. A refreshed row is
therefore fresher than the sweep around it — deliberately: each name is judged on
its own gates, and the row carries ``refreshed_at`` + ``price_source`` so the
mixed vintage is visible rather than implied. The next daily sweep re-scans the
WHOLE universe and overwrites every row, refreshed ones included.

NOT CACHED HERE. The account overlay (affordability, Level 5) is applied per
request in ``app`` against live state, so it is never frozen by this cache; and an
explicit ticker subset (an entry snapshot at trade time) always computes fresh.
The operator's Rescan button forces past this cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta, time as _time
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# The daily bars this sweep reads settle after the close, so the scan day rolls
# just after it — the sweep is always computed outside trading hours, on final
# bars. Matches the scheduler's post-close slot (config.POST_CLOSE_SLOT_ET).
SCAN_ROLL_ET = _time(16, 15)

# Bump when the row SHAPE changes (a new column, a renamed field) so an upgraded
# deploy never renders yesterday's payload through today's UI.
#   v3 — Level-4 chart-structure shadow keys (structure / structure_score /
#        structure_score_of / consolidation_phase / suitability_notes). The bump
#        is load-bearing beyond the new columns: `suitability` on a v2 row was
#        computed under the phase-BLIND volume rule, so serving one after this
#        deploy would keep emitting the thin-participation CAUTION the change
#        removes — and `suitability` gates the recommendation pool, the queue and
#        the hot-refresh set.
SCHEMA = "v3"

_FILENAME = "scan_scorecard.json"
_lock = threading.Lock()

# Parsed-file memo, keyed by (path, mtime, size). /api/scan/status polls every
# 2.5s and asks "is this day warm?", which would otherwise re-read and re-parse
# a ~500-row JSON document on every poll. Keyed on the file stat, so an external
# write (another worker, a cleared cache) is picked up immediately.
_parsed: tuple[tuple, dict] | None = None


def _path() -> str:
    # active_cache_dir() already separates demo from live, so a mode switch reads
    # a different file rather than a mistakenly shared one.
    return os.path.join(config.active_cache_dir(), _FILENAME)


def scan_day(now: datetime | None = None) -> str:
    """The trading day whose CLOSED bars the current sweep covers (``YYYY-MM-DD``).

    Today once its close has passed; otherwise the most recent completed session —
    so a pre-market visit, a weekend and a holiday all replay the last real
    session's sweep rather than triggering an identical one. PURE apart from the
    clock."""
    import market_calendar
    now = now.astimezone(ET) if now else datetime.now(ET)
    d = now.date()
    if market_calendar.is_trading_day(d) and now.time() >= SCAN_ROLL_ET:
        return d.isoformat()
    d -= timedelta(days=1)
    while not market_calendar.is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


def fingerprint(regime_color: str | None) -> str:
    """Identity of the GLOBAL inputs that would make every cached row WRONG, not
    just old — the market regime and the demo/live mode, plus the row schema.

    The ticker universe is NOT here on purpose (see the module docstring): a
    universe edit invalidates the rows it actually touches, never the whole sweep.
    """
    payload = json.dumps({
        "schema": SCHEMA,
        "regime": regime_color,
        "demo": bool(config.demo_enabled()),
    }, sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _read_blob() -> dict | None:
    """The stored blob, re-parsed only when the file actually changed. Never
    raises — an unreadable or corrupt cache is simply a miss."""
    global _parsed
    path = _path()
    try:
        st = os.stat(path)
    except OSError:
        _parsed = None
        return None
    stamp = (path, st.st_mtime_ns, st.st_size)
    memo = _parsed
    if memo is not None and memo[0] == stamp:
        return memo[1]
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception as e:  # noqa: BLE001 — a bad cache must never break the scan
        logger.warning("scan cache unreadable (%s); recomputing", e)
        return None
    if not isinstance(blob, dict):
        return None
    _parsed = (stamp, blob)
    return blob


def reusable(names, regime_color: str | None,
             now: datetime | None = None) -> dict | None:
    """What of the stored sweep can be reused for ``names`` right now.

    None on a scan-day or fingerprint miss — nothing is reusable and the caller
    must sweep from cold. Otherwise:

        {"result":    the stored result dict, rows filtered to `names`,
         "missing":   names with no stored row, in `names` order,
         "complete":  True when nothing is missing,
         "scanned_at": when the stored sweep ran}

    Rows for names no longer in the universe are dropped here, so a REMOVAL costs
    nothing and an ADDITION costs only the added names. Never raises — an
    unreadable or corrupt cache is simply a miss.
    """
    blob = _read_blob()
    if blob is None:
        return None
    want_day, want_fp = scan_day(now), fingerprint(regime_color)
    if blob.get("scan_day") != want_day or blob.get("fingerprint") != want_fp:
        return None
    result = blob.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return None

    wanted = [str(n).upper() for n in (names or [])]
    want_set = set(wanted)
    kept, have = [], set()
    for row in result["results"]:
        ticker = str(row.get("ticker") or "").upper()
        if ticker in want_set and ticker not in have:
            kept.append(row)
            have.add(ticker)
    missing = [n for n in dict.fromkeys(wanted) if n not in have]

    # Provenance for the UI: this sweep is a replay, and this is when it ran.
    out = dict(result)
    out["results"] = kept
    out["cached"] = True
    out["scanned_at"] = blob.get("scanned_at")
    out["scan_day"] = want_day
    return {"result": out, "missing": missing, "complete": not missing,
            "scanned_at": blob.get("scanned_at")}


def load(names, regime_color: str | None, now: datetime | None = None) -> dict | None:
    """The stored sweep when it matches the current scan day AND fingerprint AND
    covers every name, else None.

    This is the strict all-or-nothing read: "can I serve this request purely from
    cache". A sweep that covers only SOME of `names` is not a hit here — callers
    that can compute the remainder use ``reusable`` instead."""
    hit = reusable(names, regime_color, now=now)
    return hit["result"] if hit is not None and hit["complete"] else None


def store(regime_color: str | None, result: dict,
          now: datetime | None = None) -> None:
    """Persist a fresh full-universe sweep for the current scan day, replacing
    whatever was there — including any rows individually refreshed during the day.
    Best-effort: a full or read-only volume degrades to no caching, never a failed
    scan. Written via a temp file + atomic replace so a concurrent reader never
    sees a half-file."""
    global _parsed
    if not isinstance(result, dict) or not result.get("results"):
        return  # never pin an empty/failed sweep for a whole day
    blob = {
        "scan_day": scan_day(now),
        "fingerprint": fingerprint(regime_color),
        "scanned_at": (now or datetime.now(ET)).isoformat(timespec="seconds"),
        "result": result,
    }
    path = _path()
    tmp = f"{path}.tmp"
    try:
        with _lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            os.replace(tmp, path)
            _parsed = None  # force a re-stat/parse on the next read
    except Exception as e:  # noqa: BLE001 — caching is an optimization, never a dependency
        logger.warning("could not persist scan cache (%s)", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def patch_rows(names, regime_color: str | None, rows,
               now: datetime | None = None) -> int:
    """Write individually-refreshed rows back into the current scan day's sweep,
    replacing them by ticker. Returns how many rows were replaced.

    Rows are independent, so a refreshed stock persists (surviving a reload) and
    every other row is left exactly as the daily sweep computed it. Each patched
    row is stamped ``refreshed_at`` so the UI can show it as fresher than the
    sweep around it instead of silently mixing vintages.

    Only replaces tickers ALREADY in the sweep AND still in ``names`` — a refresh
    never grows the cached universe (a sector refresh also pulls its ETF, which is
    not a scan row) and never resurrects a row for a name that has since been
    removed. A miss on the scan day or fingerprint is a no-op: there is no sweep
    this belongs to.
    """
    global _parsed
    universe = {str(n).upper() for n in (names or [])}
    by_ticker = {r.get("ticker"): r for r in (rows or [])
                 if isinstance(r, dict) and r.get("ticker")
                 and str(r["ticker"]).upper() in universe}
    if not by_ticker:
        return 0
    blob = _read_blob()
    if blob is None:
        return 0
    if (blob.get("scan_day") != scan_day(now)
            or blob.get("fingerprint") != fingerprint(regime_color)):
        return 0
    result = blob.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return 0
    stamp = (now or datetime.now(ET)).isoformat(timespec="seconds")
    patched, out = 0, []
    for row in result["results"]:
        fresh = by_ticker.get(row.get("ticker"))
        if fresh is None:
            out.append(row)
            continue
        out.append(dict(fresh, refreshed_at=stamp))
        patched += 1
    if not patched:
        return 0
    result = dict(result, results=out)
    blob = dict(blob, result=result)
    path = _path()
    tmp = f"{path}.tmp"
    try:
        with _lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            os.replace(tmp, path)
            _parsed = None
    except Exception as e:  # noqa: BLE001 — persistence is a nicety, never a dependency
        logger.warning("could not persist refreshed rows (%s)", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0
    return patched


def clear() -> None:
    """Drop the persisted sweep — called when the universe changes or the operator
    switches demo/live, so the next scan can't serve rows for the old world."""
    global _parsed
    _parsed = None
    try:
        os.remove(_path())
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("could not clear scan cache (%s)", e)


def status(names, regime_color: str | None, now: datetime | None = None) -> dict:
    """Whether this scan day's sweep is already on disk — lets the scheduler skip
    a sweep instead of recomputing, which is what makes it once-a-day rather than
    once-a-tick."""
    hit = load(names, regime_color, now=now)
    return {"warm": hit is not None, "scan_day": scan_day(now),
            "scanned_at": hit.get("scanned_at") if hit else None}
