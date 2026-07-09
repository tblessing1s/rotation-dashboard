"""Entry-context snapshots — the immutable freeze of every feature value that
produced a GO verdict, captured synchronously when the opening execution is
appended.

Why this exists: the calibration harness (calibration.py) cannot validate any
threshold unless every closed cycle carries the entry-time inputs that led to
the trade. Those inputs live in short-TTL caches that get overwritten, so if we
don't freeze them at trade time they are lost forever — fabricating them later
from cached bars would be worse than missing data (R5). So we snapshot ONCE, on
the buy_leap, onto the immutable execution (and mirror onto the position).

Two hard rules govern this module (config.SNAPSHOT_*):
  * SNAPSHOT_NEVER_BLOCKS_EXECUTION — capture is best-effort and TOTALLY
    swallowed on error. It must never raise into the execution path and never
    make a NEW provider call (it reads caches / memoized scans / local files
    only). Anything that would need a fresh fetch, or whose cached datum is
    stale beyond its tier max-age, is recorded as null with a ``missing_reason``.
  * SNAPSHOT_SCHEMA_VERSION — every snapshot is stamped with its own version,
    independent of state.json's schema_version.

The snapshot is a raw record (like an execution), NOT derived data:
recompute_derived never reads or regenerates it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config

# The market-data scalar fields whose null-ness measures snapshot data quality.
# Operator-supplied intent fields (strike/expiry/posture) and the greek-only
# leap_delta are deliberately excluded — they are inputs, not fetched telemetry,
# so a missing one is not a data-quality failure and must not trip the alert.
_TRACKED_FIELDS = (
    "scorecard.verdict",
    "regime.status", "regime.vix", "regime.breadth",
    "sector.rs3m_vs_spy", "sector.breadth",
    "stock.rs3m_vs_spy", "stock.rs3m_vs_sector", "stock.atr_pct",
    "stock.atr_value", "stock.rsi", "stock.pct_above_ma21", "stock.price",
    "iv.iv_rank", "iv.iv_percentile",
)


def _utc(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _bars_stale(ticker: str, now: datetime) -> bool:
    """True only when data_cache holds an EXPLICIT bars record for the ticker
    that is stale beyond its tier max-age. Absent record -> not stale here (we
    fall through to computing from the parquet cache); unknown-fresh is handled
    per-field, not as a blanket null, so warm scans / offline tests behave."""
    try:
        import data_cache
        from market_scheduler import BARS
        if data_cache.record(ticker, BARS) is None:
            return False
        _, _, is_stale = data_cache.get_with_staleness(
            ticker, BARS, now=now.timestamp())
        return bool(is_stale)
    except Exception:  # noqa: BLE001 — staleness is advisory; degrade to "fresh"
        return False


def _staleness_detail(ticker: str, now: datetime) -> dict | None:
    try:
        import data_cache
        return data_cache.symbol_staleness(ticker, now=now.timestamp())
    except Exception:  # noqa: BLE001
        return None


def capture(ticker: str, payload: dict | None = None,
            account_gate: dict | None = None, *, now: datetime | None = None) -> dict:
    """Build the entry_context snapshot for ``ticker``. Never raises, never
    fetches. Returns a dict with every R1 section present; any field that could
    not be read is null and its reason is recorded under
    ``data_quality.missing``. ``account_gate`` defaults to the Level-5 result
    already stashed on ``payload['_account_gate']`` (no re-evaluation)."""
    payload = payload or {}
    ticker = (ticker or "").upper()
    ts = _utc(now)
    if account_gate is None:
        account_gate = payload.get("_account_gate")

    missing: list[dict] = []

    def track(path: str, value, reason: str):
        """Record a tracked field; note it (with reason) when null. Only paths in
        _TRACKED_FIELDS count toward the data-quality fraction."""
        if value is None:
            missing.append({"field": path, "missing_reason": reason})
        return value

    snap: dict = {
        "snapshot_schema_version": config.SNAPSHOT_SCHEMA_VERSION,
        "captured_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_session": _market_session(ts),
        "scorecard": None,
        "regime": None,
        "sector": None,
        "stock": None,
        "iv": None,
        "gates": {"entry_gate": None, "account_gate": None, "override": None},
        "execution_intent": _execution_intent(payload),
        "data_quality": None,
    }

    bars_stale = _bars_stale(ticker, ts)
    # When the ticker's cached bars are known-stale, every market-derived scalar
    # is recorded null with reason "stale" rather than computed off aged data —
    # the whole point of the missing-data policy (R4). IV (local file) and the
    # account gate (already computed) are unaffected.
    market_reason = "stale" if bars_stale else "unavailable"

    # --- Scorecard row: verdict + every scalar metric at entry ---------------
    row = None if bars_stale else _scorecard_row(ticker)
    snap["scorecard"] = _scorecard_section(row, track, market_reason)

    # --- Entry gate: regime / sector / stock detail + per-level pass flags ----
    gate = None if bars_stale else _entry_gate(ticker)
    snap["gates"]["entry_gate"] = _gate_levels(gate)
    snap["regime"] = _regime_section(gate, track, market_reason)
    snap["sector"] = _sector_section(gate, track, market_reason)
    snap["stock"] = _stock_section(ticker, gate, row, bars_stale, track, market_reason)

    # --- IV rank / percentile (local history file — never a provider call) ----
    snap["iv"] = _iv_section(ticker, track)

    # --- Gates: account-gate per-check detail + typed override ----------------
    snap["gates"]["account_gate"] = _account_gate_section(account_gate)
    snap["gates"]["override"] = _override(payload, account_gate)

    # --- Data quality: staleness provenance + null-field accounting -----------
    denom = len(_TRACKED_FIELDS)
    null_fraction = round(len(missing) / denom, 4) if denom else 0.0
    snap["data_quality"] = {
        "staleness": _staleness_detail(ticker, ts),
        "bars_stale": bars_stale,
        "tracked_fields": denom,
        "null_fields": len(missing),
        "null_field_fraction": null_fraction,
        "alert_threshold": config.SNAPSHOT_NULL_FIELD_ALERT_FRACTION,
        "over_null_threshold": null_fraction > config.SNAPSHOT_NULL_FIELD_ALERT_FRACTION,
        "missing": missing,
    }
    return snap


# ---------------------------------------------------------------------------
# Section builders — each fully guarded; a failure degrades to null-with-reason.
# ---------------------------------------------------------------------------
def _market_session(ts: datetime) -> str:
    try:
        import market_scheduler
        return "open" if market_scheduler.is_market_open(ts) else "closed"
    except Exception:  # noqa: BLE001
        return "unknown"


def _scorecard_row(ticker: str) -> dict | None:
    """One scorecard row for the ticker (computed off cached bars). Best-effort:
    offline/missing data returns None, never raises."""
    try:
        from metrics import scorecard as sc
        rows = sc.scorecard([ticker]).get("results") or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001 — a snapshot must never block an entry
        return None


def _entry_gate(ticker: str) -> dict | None:
    try:
        import screening
        return screening.entry_gate(ticker)
    except Exception:  # noqa: BLE001
        return None


def _scorecard_section(row: dict | None, track, reason: str) -> dict:
    row = row or {}
    verdict = track("scorecard.verdict", row.get("verdict"), reason)
    # Every scalar metric the scorecard exposes, verbatim (None-safe copy).
    metric_keys = (
        "price", "rs3m_vs_spy", "rs3m_vs_sector", "pct_above_ma21",
        "pct_above_ma200", "atr_extension", "below_ma50", "below_ma200",
        "ma50_slope", "volume_ratio", "volume_acceleration", "obv_above_ema",
        "obv_pct_distance", "mfi", "atr_momentum", "juice_weekly_pct",
        "juice_target_pct", "juice_ok",
    )
    return {
        "verdict": verdict,
        "reasons": row.get("reasons"),
        "metrics": {k: row.get(k) for k in metric_keys},
    }


def _level_detail(gate: dict | None, level: int) -> dict:
    if not gate:
        return {}
    for lv in gate.get("levels") or []:
        if lv.get("level") == level:
            return lv.get("detail") or {}
    return {}


def _gate_levels(gate: dict | None) -> dict | None:
    """Per-level pass/fail for entry-gate levels 1-4 with their check detail."""
    if not gate:
        return None
    return {
        "verdict": gate.get("verdict"),
        "cleared_level": gate.get("cleared_level"),
        "levels": [
            {"level": lv.get("level"), "name": lv.get("name"),
             "pass": lv.get("pass"), "checks": lv.get("checks")}
            for lv in gate.get("levels") or []
        ],
    }


def _regime_section(gate: dict | None, track, reason: str) -> dict:
    d = _level_detail(gate, 1)
    # v1 fields (unchanged, still tracked for data-quality) + the full Genius
    # four-light decision trace added in SNAPSHOT_SCHEMA_VERSION 2. All new fields
    # are additive: a v1 snapshot simply lacks them and still loads.
    return {
        "status": track("regime.status", d.get("status"), reason),
        "breadth": track("regime.breadth", d.get("breadth"), reason),
        "vix": track("regime.vix", d.get("vix"), reason),
        "vix_source": d.get("vix_source"),
        "spy_trend": d.get("spy_trend"),
        "spy_dist_ma21": d.get("spy_dist_ma21"),
        # --- Genius four-light decision trace (v2) ---
        "published_regime": d.get("published_regime"),
        "raw_condition": d.get("raw_condition"),
        "dwell_regime": d.get("dwell_regime"),
        "lights": d.get("lights"),
        "vote": d.get("vote"),
        "dwell": d.get("dwell"),
        "vetoes": d.get("vetoes"),
    }


def _sector_section(gate: dict | None, track, reason: str) -> dict:
    d = _level_detail(gate, 2)
    return {
        "etf": d.get("sector"),
        "name": d.get("name"),
        "rs3m_vs_spy": track("sector.rs3m_vs_spy", d.get("rs3m"), reason),
        "breadth": track("sector.breadth", d.get("breadth"), reason),
        "atr_expanding": d.get("atr_expanding"),
        "status": d.get("status"),
    }


def _stock_section(ticker: str, gate: dict | None, row: dict | None,
                   bars_stale: bool, track, reason: str) -> dict:
    d = _level_detail(gate, 3)   # the _stock_row (rs3m pair, atr_pct, consolidating)
    row = row or {}
    # ATR value + RSI are not on the scorecard/gate row — compute from cached
    # bars (unless bars are stale, in which case they stay null with reason).
    atr_value = rsi = None
    if not bars_stale:
        atr_value, rsi = _atr_rsi(ticker)
    return {
        "rs3m_vs_spy": track("stock.rs3m_vs_spy", d.get("rs3m_vs_spy"), reason),
        "rs3m_vs_sector": track("stock.rs3m_vs_sector", d.get("rs3m_vs_sector"), reason),
        "atr_pct": track("stock.atr_pct", d.get("atr_pct"), reason),
        "atr_value": track("stock.atr_value", atr_value, reason),
        "rsi": track("stock.rsi", rsi, reason),
        "pct_above_ma21": track("stock.pct_above_ma21", row.get("pct_above_ma21"), reason),
        "price": track("stock.price", row.get("price"), reason),
        "consolidating": d.get("consolidating"),
        "is_etf": row.get("is_etf"),
    }


def _atr_rsi(ticker: str) -> tuple[float | None, float | None]:
    try:
        import data_handler
        import indicators
        df = data_handler.get_daily(ticker)
        if df is None:
            return None, None
        atr = indicators.atr(df)
        rsi = indicators.rsi(df)
        return (round(atr, 4) if atr is not None else None,
                round(rsi, 2) if rsi is not None else None)
    except Exception:  # noqa: BLE001
        return None, None


def _iv_section(ticker: str, track) -> dict:
    try:
        import iv_history
        iv = iv_history.iv_rank(ticker)
    except Exception:  # noqa: BLE001
        iv = {}
    return {
        "iv_rank": track("iv.iv_rank", iv.get("iv_rank"), "unavailable"),
        "iv_percentile": track("iv.iv_percentile", iv.get("iv_percentile"), "unavailable"),
        "iv_now": iv.get("iv_now"),
        "iv_min": iv.get("iv_min"),
        "iv_max": iv.get("iv_max"),
        "days": iv.get("days"),
    }


def _account_gate_section(gate: dict | None) -> dict | None:
    """The Level-5 account/juice gate: per-check pass/fail + blocking flags.
    Reuses the already-computed result (payload['_account_gate']); never
    re-evaluates (which would touch Schwab)."""
    if not gate:
        return None
    return {
        "pass": gate.get("pass"),
        "blocking_failures": gate.get("blocking_failures"),
        "warnings": gate.get("warnings"),
        "checks": [
            {"id": c.get("id"), "label": c.get("label"), "pass": c.get("pass"),
             "blocking": c.get("blocking")}
            for c in gate.get("checks") or []
        ],
        "juice": gate.get("juice"),
    }


def _override(payload: dict, account_gate: dict | None) -> dict | None:
    """The typed Level-5 override, if the entry overrode a blocking gate."""
    reason = (payload.get("override_reason") or "").strip()
    if not reason:
        return None
    return {"reason": reason,
            "failed_checks": (account_gate or {}).get("blocking_failures", [])}


def _execution_intent(payload: dict) -> dict:
    """Operator-chosen trade parameters at entry. leap_delta needs a live chain
    greek, so it is usually null (unavailable) — recorded, not fetched."""
    dte = payload.get("dte")
    if dte is None:
        dte = config.LEAP_TARGET_DTE
    return {
        "posture": payload.get("posture"),
        "strike_policy_row": payload.get("strike_policy_row") or payload.get("strike_policy"),
        "short_strike": payload.get("short_strike"),
        "short_expiry": payload.get("short_expiration"),
        "leap_strike": payload.get("strike"),
        "leap_dte": dte,
        "leap_delta": payload.get("leap_delta") or payload.get("delta"),
    }


def summary(entry_context: dict | None) -> dict:
    """A compact digest of a snapshot for the closed-cycle record and the juice
    journal CSV (R6): verdict, regime, IV rank, and the RS3M pair — the full
    snapshot stays available via the /api/history detail, not the CSV."""
    ec = entry_context or {}
    stock = ec.get("stock") or {}
    return {
        "verdict": (ec.get("scorecard") or {}).get("verdict"),
        "regime": (ec.get("regime") or {}).get("status"),
        "iv_rank": (ec.get("iv") or {}).get("iv_rank"),
        "rs3m_vs_spy": stock.get("rs3m_vs_spy"),
        "rs3m_vs_sector": stock.get("rs3m_vs_sector"),
    }


# ---------------------------------------------------------------------------
# Exit-time counterpart metrics — the SAME stock-level set as entry, captured on
# the close so calibration can compute entry->exit deltas (R3). Network-free.
# ---------------------------------------------------------------------------
def exit_metrics(ticker: str, *, now: datetime | None = None) -> dict:
    """Stock-level metrics at exit time, mirroring the entry snapshot's ``stock``
    block so a closed cycle carries both endpoints. Best-effort; never raises,
    never fetches beyond the cache."""
    ticker = (ticker or "").upper()
    out = {"captured_at": _utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "rs3m_vs_spy": None, "rs3m_vs_sector": None, "atr_pct": None,
           "atr_value": None, "rsi": None, "pct_above_ma21": None, "price": None}
    try:
        import data_handler
        import indicators
        import sector_data
        from metrics import scorecard as sc
        spy = data_handler.get_daily(config.BENCHMARK)
        df = data_handler.get_daily(ticker)
        etf = sector_data.sector_for(ticker) or ""
        sector_df = data_handler.get_daily(etf) if etf else None
        m = sc.metrics_for(df, spy, sector_df)
        atr, rsi = _atr_rsi(ticker)
        out.update({
            "rs3m_vs_spy": m.get("rs3m_vs_spy"),
            "rs3m_vs_sector": m.get("rs3m_vs_sector"),
            "atr_pct": indicators.atr_pct(df) if df is not None else None,
            "atr_value": atr, "rsi": rsi,
            "pct_above_ma21": m.get("pct_above_ma21"),
            "price": m.get("price"),
        })
    except Exception:  # noqa: BLE001 — exit metrics are telemetry, not a blocker
        pass
    return out
