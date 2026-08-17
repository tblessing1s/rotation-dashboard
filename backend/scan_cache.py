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

WHAT THIS DOES. Persist one sweep per DATA EPOCH to the volume, so the universe
is scanned about twice a day instead of dozens of times, and a restart re-reads
the last sweep instead of recomputing it.

THE EPOCH. Keying purely on the calendar date would serve a pre-close sweep all
evening, after the session's own bar has landed — stale for the part of the day
the operator reviews. So the key is (ET date, session phase), where the phase
flips once at ``EPOCH_ROLL_ET``, shortly after the close. That is exactly when
the daily bars this sweep reads can change, and it matches data_handler's own
12-hour freshness window: one sweep on the prior session's closed bars, one on
today's. Anything finer would recompute identical numbers.

THE FINGERPRINT. Anything that would make yesterday's answer *wrong* rather than
merely old is folded into the key, so a change re-scans instead of serving a
stale row: the ticker universe (names added/removed), the market regime (a RED
regime forces every verdict to BLOCKED, so the composed verdicts must track it),
and the demo/live mode. A miss on any of these is a fresh sweep, which is the
correct — and rare — cost.

NOT CACHED HERE. The account overlay (affordability, Level 5) is applied per
request in ``app`` against live state, so it is never frozen by this cache; and
an explicit ticker subset (an entry snapshot at trade time) always computes
fresh. The operator's Rescan button forces past this cache, and the per-ticker /
per-sector Refresh buttons remain the intraday escape hatch for a single name.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, time as _time
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# The daily bars this sweep reads settle after the close; roll the epoch once,
# shortly after, so the evening's scan reflects the session that just ended
# rather than the pre-open snapshot taken that morning.
EPOCH_ROLL_ET = _time(16, 15)

# Bump when the row SHAPE changes (a new column, a renamed field) so an upgraded
# deploy never renders yesterday's payload through today's UI.
SCHEMA = "v1"

_FILENAME = "scan_scorecard.json"
_lock = threading.Lock()

# Parsed-file memo, keyed by (path, mtime, size). /api/scan/status polls every
# 2.5s and asks "is this epoch warm?", which would otherwise re-read and re-parse
# a ~500-row JSON document on every poll. Keyed on the file stat, so an external
# write (another worker, a cleared cache) is picked up immediately.
_parsed: tuple[tuple, dict] | None = None


def _path() -> str:
    # active_cache_dir() already separates demo from live, so a mode switch reads
    # a different file rather than a mistakenly shared one.
    return os.path.join(config.active_cache_dir(), _FILENAME)


def epoch(now: datetime | None = None) -> str:
    """The data epoch a sweep belongs to: ``YYYY-MM-DD/pre`` before the roll,
    ``YYYY-MM-DD/post`` after it. PURE apart from the clock."""
    now = now.astimezone(ET) if now else datetime.now(ET)
    return f"{now.strftime('%Y-%m-%d')}/{'post' if now.time() >= EPOCH_ROLL_ET else 'pre'}"


def fingerprint(names, regime_color: str | None) -> str:
    """Identity of the inputs that would make a cached sweep WRONG, not just old."""
    payload = json.dumps({
        "schema": SCHEMA,
        "names": sorted({str(n).upper() for n in (names or [])}),
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


def load(names, regime_color: str | None, now: datetime | None = None) -> dict | None:
    """The stored sweep when it matches the current epoch AND fingerprint, else
    None. Never raises — an unreadable or corrupt cache is simply a miss."""
    blob = _read_blob()
    if blob is None:
        return None
    want_epoch, want_fp = epoch(now), fingerprint(names, regime_color)
    if blob.get("epoch") != want_epoch or blob.get("fingerprint") != want_fp:
        return None
    result = blob.get("result")
    if not isinstance(result, dict):
        return None
    # Provenance for the UI: this sweep is a replay, and this is when it ran.
    result = dict(result)
    result["cached"] = True
    result["scanned_at"] = blob.get("scanned_at")
    result["epoch"] = want_epoch
    return result


def store(names, regime_color: str | None, result: dict,
          now: datetime | None = None) -> None:
    """Persist a fresh sweep for the current epoch. Best-effort: a full or
    read-only volume degrades to no caching, never a failed scan. Written via a
    temp file + atomic replace so a concurrent reader never sees a half-file."""
    global _parsed
    if not isinstance(result, dict) or not result.get("results"):
        return  # never pin an empty/failed sweep for a whole epoch
    blob = {
        "epoch": epoch(now),
        "fingerprint": fingerprint(names, regime_color),
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
    """Whether this epoch's sweep is already on disk — lets the scheduler skip a
    warm-up instead of recomputing, which is what makes the sweep once-an-epoch
    rather than once-a-tick."""
    hit = load(names, regime_color, now=now)
    return {"warm": hit is not None, "epoch": epoch(now),
            "scanned_at": hit.get("scanned_at") if hit else None}
