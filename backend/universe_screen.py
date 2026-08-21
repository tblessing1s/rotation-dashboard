"""Internal approximation of the operator's Finviz momentum screen, from the
daily bars the app already caches — the wider-universe intake filter.

The operator's manual screen is: Perf Quarter > +15%, RSI 50–70, mid/large cap,
avg volume > 500K, optionable (weekly options). Three of those five are computable
OFFLINE from cached OHLCV; the other two need data the app does not ingest today
and are handled honestly (per AUDIT_SCAN_PIPELINE_PHASE0.md Q7):

  * Perf Quarter > +15%   — ``indicators.roc`` over ~a quarter of bars.   OFFLINE
  * RSI 50–70             — ``indicators.rsi``.                           OFFLINE
  * avg volume > 500K     — mean of the trailing ``Volume`` (new here).   OFFLINE
  * mid/large cap         — market cap is NOT in cached bars or any ingested Schwab
                            field; it needs a budgeted AV OVERVIEW call. DESCOPED —
                            reported, never guessed, never fetched here.
  * optionable / weeklies — a live Schwab chain probe (``weeklies.has_weeklies``),
                            7-day cached. PROVIDER-DEPENDENT: passed in when known,
                            SKIPPED (never a false fail) when unknown offline.

A DESCOPED or SKIPPED criterion never blocks the overall pass (same discipline as a
None metric in the scorecard) — the screen filters on what it can actually measure
and says so. Every threshold is ``PROPOSED_DEFAULT``.

PURE: ``evaluate`` takes a frame + scalars and does no I/O; ``screen`` takes a
frames map the caller already warmed.
"""
from __future__ import annotations

import pandas as pd

import indicators

# PROPOSED_DEFAULT — the screen thresholds (the operator's Finviz values).
PERF_QUARTER_MIN = 15.0        # PROPOSED_DEFAULT — Perf Quarter > +15%
PERF_QUARTER_WINDOW = 63       # PROPOSED_DEFAULT — ~a quarter of trading days
RSI_MIN = 50.0                 # PROPOSED_DEFAULT — RSI band low
RSI_MAX = 70.0                 # PROPOSED_DEFAULT — RSI band high (not overbought)
AVG_VOLUME_MIN = 500_000.0     # PROPOSED_DEFAULT — avg volume > 500K
AVG_VOLUME_WINDOW = 50         # PROPOSED_DEFAULT — trailing window for avg volume

# Descope reasons (surfaced on the criterion so the gap is legible, not silent).
_MARKET_CAP_DESCOPE = ("market cap is not in cached bars or any ingested Schwab "
                       "field; needs a budgeted AV OVERVIEW call (Q7 descope)")


def avg_volume(df: pd.DataFrame | None, window: int = AVG_VOLUME_WINDOW) -> float | None:
    """Mean of the trailing ``window`` daily volumes. None with insufficient
    history. Pure over the cached ``Volume`` column (no new provider call)."""
    if df is None or df.empty or "Volume" not in df:
        return None
    vol = df["Volume"].astype(float)
    if len(vol) < window:
        return None
    return float(vol.tail(window).mean())


def _crit(value, passed, computable=True, **extra) -> dict:
    return {"value": value, "pass": (None if not computable else bool(passed)),
            "computable": computable, **extra}


def evaluate(df: pd.DataFrame | None, *, has_weeklies: bool | None = None) -> dict:
    """Every screen criterion for one symbol from its cached bars. PURE.

    ``has_weeklies`` (True/False/None) is the provider-probed optionability the
    caller already resolved (``weeklies.has_weeklies``); None = unknown offline and
    the criterion is SKIPPED. Returns {criteria, pass, computed_pass} where ``pass``
    is the offline-computable verdict (descoped/unknown criteria never block)."""
    perf_q = indicators.roc(df, PERF_QUARTER_WINDOW)
    # `indicators.rsi` takes a REQUIRED frame (like sma/atr — only the helpers
    # typed `| None` guard internally), so the None case is the caller's to
    # handle. `roc` and `avg_volume` already return None here; without this the
    # nightly sweep raised on the first uncached symbol and lost the whole screen
    # (maintenance.py catches it into report["errors"]) — a cold cache, a
    # provider outage, or a newly-added ticker is enough to hit it.
    rsi_v = indicators.rsi(df) if df is not None and not df.empty else None
    av = avg_volume(df)

    criteria = {
        "perf_quarter": _crit(perf_q, perf_q is not None and perf_q > PERF_QUARTER_MIN,
                              computable=perf_q is not None, min=PERF_QUARTER_MIN),
        "rsi": _crit(rsi_v, rsi_v is not None and RSI_MIN <= rsi_v <= RSI_MAX,
                     computable=rsi_v is not None, band=[RSI_MIN, RSI_MAX]),
        "avg_volume": _crit(av, av is not None and av > AVG_VOLUME_MIN,
                            computable=av is not None, min=AVG_VOLUME_MIN),
        # Descoped — reported, never fetched, never blocks.
        "market_cap": _crit(None, None, computable=False, descoped=True,
                            reason=_MARKET_CAP_DESCOPE),
        # Provider-dependent — pass/fail only when known; None = skipped offline.
        "optionable": _crit(has_weeklies, has_weeklies is True,
                            computable=has_weeklies is not None, provider=True),
    }
    # Overall pass = every COMPUTABLE criterion passes AND optionability is not a
    # known-False (a name we KNOW has no weeklies is excluded). Descoped/unknown
    # criteria are skipped, never a false fail.
    blocking = [c for k, c in criteria.items()
                if c["computable"] and c["pass"] is not None]
    # ...but a pass must rest on the BARS. `optionable` is provider-probed, not
    # bar-derived, so a symbol whose frame is missing entirely (uncached, a
    # provider outage, a brand-new ticker) would otherwise clear the screen on a
    # successful weeklies probe alone — every bar criterion skipped as
    # "not computable", `blocking` holding only optionable, and a name we know
    # NOTHING about admitted to the candidate universe. Requiring at least one
    # computable bar criterion keeps the partial-data tolerance above (a short
    # history still skips the criteria it can't form) while making a no-data
    # name a fail rather than a vacuous pass.
    from_bars = any(criteria[k]["computable"]
                    for k in ("perf_quarter", "rsi", "avg_volume"))
    passed = from_bars and bool(blocking) and all(c["pass"] for c in blocking)
    if has_weeklies is False:
        passed = False
    return {"criteria": criteria, "pass": passed,
            "perf_quarter": perf_q, "rsi": rsi_v, "avg_volume": av}


def failing_criteria(result: dict) -> list[str]:
    """The ids of the computable criteria a symbol failed (for the change-log
    'why dropped' reason). Descoped/skipped criteria are never listed."""
    return [k for k, c in (result.get("criteria") or {}).items()
            if c.get("computable") and c.get("pass") is False]


def screen(symbols: list[str], frames: dict, weeklies: dict | None = None) -> dict:
    """Run the screen over ``symbols`` using the ``frames`` map the caller already
    warmed ({ticker: DataFrame}). ``weeklies`` maps ticker -> has_weeklies (or is
    None to skip optionability offline). PURE. Returns {passed: [tickers],
    results: {ticker: result}} — ``passed`` is the candidate universe."""
    weeklies = weeklies or {}
    results, passed = {}, []
    for t in symbols:
        t = (t or "").upper()
        if not t:
            continue
        res = evaluate(frames.get(t), has_weeklies=weeklies.get(t))
        results[t] = res
        if res["pass"]:
            passed.append(t)
    return {"passed": sorted(passed), "results": results}
