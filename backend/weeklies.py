"""Weekly-options detection.

CFM sells a *weekly* ITM call every week, so a monthly-only option chain can't run
the strategy no matter how strong the stock scores. `has_weeklies(ticker)` reads
the near-term expirations from Schwab and returns:

  True  — a genuine weekly expiration exists (a Friday that is NOT the standard
          monthly 3rd-Friday within the lookahead window),
  False — only monthly expirations are listed,
  None  — undeterminable (Schwab not connected, fetch error, or no options at
          all). Callers treat None as "don't hide" so a data hiccup never wrongly
          drops a tradeable name.

Results are cached for a long TTL (weeklies status is near-static — a name gains
weeklies and keeps them), and can be overridden by hand via
`metadata.weeklies_overrides` (e.g. {"JBHT": false, "AAPL": true}). The whole
check can be disabled with SCORECARD_CHECK_WEEKLIES=0.

Detection uses a deliberately tiny chain request (a couple of strikes, ~5 weeks
out) — only the expiration dates matter, not the strikes — so it's far cheaper
than the full LEAP-spanning chain the option-chain tab pulls.
"""
from __future__ import annotations

import calendar
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import config
import data_handler
import logging_handler as log
import schwab_api

# Weeklies status barely changes, so cache it for a week by default.
_TTL = int(os.environ.get("WEEKLIES_TTL", str(config.WEEKLIES_CACHE_TTL)))
# 40 days guarantees the window always contains at least one monthly (3rd-Friday)
# expiration — consecutive monthlies are ≤35 days apart — so a monthly-only name
# resolves to False (not an undeterminable None), while weeklies still show up.
_LOOKAHEAD_DAYS = 40

_cache: dict[str, tuple[float, bool | None]] = {}
_cache_guard = threading.Lock()
_ov_cache: tuple[float, dict[str, bool]] = (0.0, {})
_ov_guard = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="weeklies")


def _enabled() -> bool:
    return os.environ.get("SCORECARD_CHECK_WEEKLIES", "1").strip() not in ("0", "false", "no")


def _third_friday(year: int, month: int) -> date:
    """The standard monthly options expiration: the 3rd Friday of the month."""
    weeks = calendar.monthcalendar(year, month)
    fridays = [w[calendar.FRIDAY] for w in weeks if w[calendar.FRIDAY] != 0]
    return date(year, month, fridays[2])


def _overrides() -> dict[str, bool]:
    """metadata.weeklies_overrides, memoized briefly so a full-universe scorecard
    doesn't re-read state.json once per ticker."""
    global _ov_cache
    if time.time() - _ov_cache[0] < 60:
        return _ov_cache[1]
    with _ov_guard:
        if time.time() - _ov_cache[0] < 60:
            return _ov_cache[1]
        try:
            raw = (log.load_state().get("metadata", {}) or {}).get("weeklies_overrides", {}) or {}
            ov = {str(k).upper(): bool(v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001 — overrides are optional, never block
            ov = {}
        _ov_cache = (time.time(), ov)
        return ov


def _detect(ticker: str) -> bool | None:
    """Inspect near-term call expirations for a non-monthly (weekly) Friday."""
    if not schwab_api.configured():
        return None
    today = datetime.now().date()
    to_date = (today + timedelta(days=_LOOKAHEAD_DAYS + 3)).strftime("%Y-%m-%d")
    try:
        payload = data_handler.client().get_option_chain(
            ticker, strike_count=2, from_date=today.strftime("%Y-%m-%d"), to_date=to_date)
    except Exception:  # noqa: BLE001 — undeterminable, not tradeable-negative
        return None
    exp_map = (payload or {}).get("callExpDateMap") or {}
    if not exp_map:
        return None
    saw_expiration = False
    for key in exp_map:  # keys look like "2026-07-10:5"
        datestr = key.split(":", 1)[0]
        try:
            d = datetime.strptime(datestr, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < today or (d - today).days > _LOOKAHEAD_DAYS:
            continue
        saw_expiration = True
        # A Friday that isn't the month's 3rd Friday is a weekly expiration.
        if d.weekday() == calendar.FRIDAY and d != _third_friday(d.year, d.month):
            return True
    # Saw only monthly (3rd-Friday) expirations -> monthly-only. Saw nothing in
    # the window -> undeterminable.
    return False if saw_expiration else None


def has_weeklies(ticker: str, refresh: bool = False) -> bool | None:
    """True/False/None (see module docstring). Override wins; else cached; else
    detected. None results aren't pinned for the full TTL so they retry sooner."""
    t = ticker.upper()
    ov = _overrides().get(t)
    if ov is not None:
        return ov
    if not _enabled():
        return None
    if not refresh:
        hit = _cache.get(t)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    val = _detect(t)
    if val is not None:
        with _cache_guard:
            _cache[t] = (time.time(), val)
    return val


def prefetch(tickers) -> None:
    """Warm the weeklies cache for many tickers in parallel so a full-universe
    scorecard doesn't serialize one chain fetch after another."""
    if not _enabled():
        return
    names = [t for t in dict.fromkeys(str(x).upper() for x in tickers) if t]
    if names:
        list(_pool.map(has_weeklies, names))
