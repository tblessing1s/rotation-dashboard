"""Weekly LEAP theta-burn marks + realized-vs-projected divergence.

Each week we snapshot, per open position, the LEAP's MODEL extrinsic at a
consistent time (mid inputs) plus the forward burn projection at that moment
(re-run against current spot & IV). From two consecutive marks we get the
REALIZED burn for the intervening week:

    realized_burn_week = previous_mark.extrinsic - current_mark.extrinsic

(may be NEGATIVE if IV spiked and extrinsic grew — recorded as-is; negative
realized burn is information, not an error). Comparing realized against the burn
we PROJECTED a week earlier gives a live verification harness for the
Black-Scholes engine + put-IV substitution — one of the known open verification
items. Persistent divergence beyond ``BURN_DIVERGENCE_WARN_PCT`` surfaces a
warning badge, never a hard failure.

Marks are telemetry, not a trading record, so — like ``iv_history`` — they live
in ``DATA_DIR/burn_marks.json``, OUT of the append-only state.json execution
record. The divergence math here is pure and offline-testable; the weekly job
that feeds it (resolving live spot/IV) lives in maintenance.py.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import config

BURN_MARKS_PATH = os.path.join(config.DATA_DIR, "burn_marks.json")
_MAX_POINTS = 104          # ~2 years of weekly marks per ticker
_lock = threading.RLock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        with open(BURN_MARKS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    tmp = f"{BURN_MARKS_PATH}.tmp.{os.getpid()}"
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, BURN_MARKS_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _days_between(a: str, b: str) -> float:
    try:
        da = datetime.strptime(a[:10], "%Y-%m-%d").date()
        db = datetime.strptime(b[:10], "%Y-%m-%d").date()
        return abs((db - da).days)
    except (TypeError, ValueError):
        return 7.0


def record_mark(ticker: str, projection: dict, *, spot: float | None,
                iv: float | None, current_dte: int | None = None,
                day: str | None = None, now_iso: str | None = None) -> dict | None:
    """Append this week's burn mark for a ticker from a ``burn.burn_projection``
    result. Computes ``realized_burn_week`` against the prior mark's model
    extrinsic, normalized to a per-week rate by the days between marks. One mark
    per calendar day (last write wins). Returns the stored mark, or None when the
    projection isn't priceable (a gap never poisons the series)."""
    ticker = (ticker or "").strip().upper()
    if not ticker or not projection or not projection.get("priceable"):
        return None
    day = day or _today()
    extrinsic_now = projection.get("extrinsic_now")
    mark = {
        "date": day,
        "ts": now_iso or _now_iso(),
        "spot": round(spot, 4) if spot is not None else None,
        "iv": round(iv, 2) if iv is not None else None,
        "current_dte": current_dte,
        "planned_exit_dte": projection.get("planned_exit_dte"),
        "extrinsic_now": extrinsic_now,
        # The MODEL burn/week we project for the coming week (no slippage — that's
        # a one-time exit cost, not weekly extrinsic decay). This is the figure the
        # next mark's realized burn is checked against.
        "projected_burn_per_week": projection.get("projected_burn_per_week"),
        "burn_per_week_with_slippage": projection.get("burn_per_week_with_slippage"),
        "low_extrinsic_flag": projection.get("low_extrinsic_flag", False),
        "realized_burn_week": None,
        "projected_last_week": None,
    }
    with _lock:
        data = _load()
        series_ = data.setdefault(ticker, [])
        prev = None
        if series_ and series_[-1].get("date") == day:
            # Replace today's mark; the one before it is the comparison base.
            prev = series_[-2] if len(series_) >= 2 else None
            series_.pop()
        elif series_:
            prev = series_[-1]
        if prev is not None and prev.get("extrinsic_now") is not None and extrinsic_now is not None:
            weeks = max(_days_between(prev["date"], day) / 7.0, 1e-6)
            mark["realized_burn_week"] = round((prev["extrinsic_now"] - extrinsic_now) / weeks, 2)
            mark["projected_last_week"] = prev.get("projected_burn_per_week")
        series_.append(mark)
        del series_[:-_MAX_POINTS]
        _save(data)
    return mark


def series(ticker: str) -> list[dict]:
    return list(_load().get((ticker or "").strip().upper(), []))


def monthly_realized_burn(clamp_negative: bool = True) -> dict[str, float]:
    """Realized LEAP extrinsic burn per calendar month ('YYYY-MM' -> whole-position
    dollars), summed across every tracked ticker.

    Burn for the span between two consecutive marks is the drop in model extrinsic
    ``prev.extrinsic_now - cur.extrinsic_now`` (whole-position $, same units as the
    juice ledger), attributed to the month of the later mark. By default negative
    spans are clamped to 0 so a LEAP roll or an IV spike that GROWS extrinsic can't
    masquerade as income against the monthly payout — burn is only ever a cost.
    Returns {} when there aren't two comparable marks yet (feature degrades to
    juice-only cleanly)."""
    out: dict[str, float] = {}
    for rows in _load().values():
        prev = None
        for m in sorted(rows, key=lambda r: str(r.get("date"))):
            ext = m.get("extrinsic_now")
            if ext is None:
                continue
            if prev is not None:
                drop = float(prev) - float(ext)
                if clamp_negative and drop < 0:
                    drop = 0.0
                month = str(m.get("date"))[:7]
                if len(month) == 7 and month[4] == "-":
                    out[month] = out.get(month, 0.0) + drop
            prev = ext
    return {k: round(v, 2) for k, v in out.items()}


def weekly_due(day: str | None = None) -> bool:
    """True at most once per ISO week — the weekly-mark cadence gate for the
    nightly job. Fires at end of week (Friday onward) and only if no mark has yet
    been recorded for the current ISO week, so a missed Friday nightly (holiday,
    restart) still gets caught over the weekend without double-marking."""
    day = day or _today()
    try:
        d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    if d.weekday() < 4:  # Mon-Thu: wait for the end of the week
        return False
    wk = d.isocalendar()[:2]
    for series_ in _load().values():
        for m in series_:
            try:
                md = datetime.strptime(str(m.get("date"))[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if md.isocalendar()[:2] == wk:
                return False   # already marked this ISO week
    return True


def _divergence_pct(realized: float | None, projected: float | None) -> float | None:
    """Signed divergence of realized burn from what we projected, as a % of the
    projection. None when either side is missing or the projection is ~zero (a
    near-zero denominator would explode — treated as not-comparable)."""
    if realized is None or projected is None or abs(projected) < 1e-6:
        return None
    return round((realized - projected) / abs(projected) * 100, 1)


def divergence(ticker: str, trailing_weeks: int | None = None) -> dict:
    """Per-position realized-vs-projected burn divergence over the trailing marks.

    For each week we compare the realized burn against the burn projected the
    PRIOR week (``projected_last_week``). Returns the trailing mean absolute
    divergence %, the per-week detail, and a ``warn`` flag when the trailing mean
    exceeds ``BURN_DIVERGENCE_WARN_PCT``. ``warn`` is a soft badge, never a hard
    failure."""
    rows = series(ticker)
    weeks = []
    for m in rows:
        pct = _divergence_pct(m.get("realized_burn_week"), m.get("projected_last_week"))
        if pct is not None:
            weeks.append({"date": m.get("date"), "realized": m.get("realized_burn_week"),
                          "projected": m.get("projected_last_week"), "divergence_pct": pct})
    if trailing_weeks:
        weeks = weeks[-int(trailing_weeks):]
    mean_abs = round(sum(abs(w["divergence_pct"]) for w in weeks) / len(weeks), 1) if weeks else None
    return {
        "ticker": (ticker or "").strip().upper(),
        "weeks": weeks,
        "sample": len(weeks),
        "mean_abs_divergence_pct": mean_abs,
        "warn": bool(mean_abs is not None and mean_abs > config.BURN_DIVERGENCE_WARN_PCT),
        "threshold_pct": config.BURN_DIVERGENCE_WARN_PCT,
    }


def aggregate_divergence(trailing_weeks: int | None = None) -> dict:
    """Book-wide divergence: the mean absolute divergence % across every ticker's
    trailing weeks (the live BS-engine verification headline). ``warn`` when the
    pooled mean crosses the threshold."""
    data = _load()
    all_pcts = []
    per_ticker = {}
    for ticker in data:
        d = divergence(ticker, trailing_weeks=trailing_weeks)
        per_ticker[ticker] = d["mean_abs_divergence_pct"]
        all_pcts.extend(abs(w["divergence_pct"]) for w in d["weeks"])
    mean_abs = round(sum(all_pcts) / len(all_pcts), 1) if all_pcts else None
    return {
        "mean_abs_divergence_pct": mean_abs,
        "sample": len(all_pcts),
        "per_ticker": per_ticker,
        "warn": bool(mean_abs is not None and mean_abs > config.BURN_DIVERGENCE_WARN_PCT),
        "threshold_pct": config.BURN_DIVERGENCE_WARN_PCT,
    }
