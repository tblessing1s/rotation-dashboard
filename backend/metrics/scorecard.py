"""CFM numeric scorecard: pure metric functions + a composite verdict, plus the
orchestration that turns a holdings list into one row per ticker.

Every metric is a pure function over already-computed inputs (or OHLCV series),
so the indicator inputs (MA21/50/200, ATR, ATR_5EMA, OBV, OBV_20EMA, Volume_20MA,
Volume_5MA) are computed ONCE per ticker in `compute_inputs` and passed in — the
metric functions never recompute a moving average.

All inputs come from the already-cached daily OHLCV frames (data_handler); this
module makes no provider calls of its own. None propagates through every metric
(insufficient history -> None) the same way the existing indicators behave.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
import data_handler
import indicators
import sector_data

from . import thresholds as T


# ---------------------------------------------------------------------------
# Metric functions (Section 2) — pure, one concern each.
# ---------------------------------------------------------------------------
def pct_above_ma21(price: float | None, ma21: float | None) -> float | None:
    """Percent distance of price above (+) / below (-) MA21: (price-ma21)/ma21*100."""
    if price is None or ma21 is None or ma21 == 0:
        return None
    return (price - ma21) / ma21 * 100


def pct_above_ma200(price: float | None, ma200: float | None) -> float | None:
    """Percent distance of price above (+) / below (-) MA200: (price-ma200)/ma200*100."""
    if price is None or ma200 is None or ma200 == 0:
        return None
    return (price - ma200) / ma200 * 100


def atr_extension(price: float | None, ma21: float | None, atr: float | None) -> float | None:
    """How stretched above MA21, in ATR units: (price-ma21)/atr. The primary
    'is it extended' number — ATR units, not percent. Returns None when ATR is 0
    or missing (never divides by zero)."""
    if price is None or ma21 is None or atr is None or atr == 0:
        return None
    return (price - ma21) / atr


def below_ma50(price: float | None, ma50: float | None) -> bool | None:
    """True when price is below its 50-day MA."""
    if price is None or ma50 is None:
        return None
    return bool(price < ma50)


def below_ma200(price: float | None, ma200: float | None) -> bool | None:
    """True when price is below its 200-day MA (the trend-is-broken line)."""
    if price is None or ma200 is None:
        return None
    return bool(price < ma200)


def ma50_slope(ma50_series: pd.Series | None, lookback: int = T.MA50_SLOPE_LOOKBACK) -> float | None:
    """MA50 today minus MA50 `lookback` days ago. Negative = rolling over."""
    if ma50_series is None:
        return None
    s = ma50_series.dropna()
    if len(s) < lookback + 1:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - lookback])


def volume_ratio(volume: float | None, volume_20ma: float | None) -> float | None:
    """Today's volume vs its 20-day average: volume / volume_20ma."""
    if volume is None or volume_20ma is None or volume_20ma == 0:
        return None
    return volume / volume_20ma


def volume_acceleration(volume_5ma: float | None, volume_20ma: float | None) -> float | None:
    """Short- vs long-run volume: volume_5ma / volume_20ma. >1 = picking up."""
    if volume_5ma is None or volume_20ma is None or volume_20ma == 0:
        return None
    return volume_5ma / volume_20ma


def obv_vs_ema(obv: float | None, obv_20ema: float | None) -> tuple[bool | None, float | None]:
    """On-Balance-Volume vs its 20-EMA. Returns (above, pct_distance) where
    pct_distance = (obv-obv_20ema)/|obv_20ema|*100. (None, None) when missing or
    the EMA is 0 (no meaningful percent distance)."""
    if obv is None or obv_20ema is None or obv_20ema == 0:
        return None, None
    return bool(obv > obv_20ema), (obv - obv_20ema) / abs(obv_20ema) * 100


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
        period: int = 14) -> float | None:
    """Standard `period`-day Money Flow Index (default 14).

    typical price = (high+low+close)/3; raw money flow = typical * volume.
    Money flow is 'positive' on days the typical price rose, 'negative' when it
    fell. MFI = 100 - 100/(1 + positive_sum/negative_sum) over the window. When
    the window has no down-days (negative_sum == 0) the ratio is infinite and MFI
    pins to 100. Returns None with insufficient history.
    """
    if any(s is None for s in (high, low, close, volume)):
        return None
    if min(len(high), len(low), len(close), len(volume)) < period + 1:
        return None
    tp = (high.astype(float) + low.astype(float) + close.astype(float)) / 3.0
    raw_mf = tp * volume.astype(float)
    delta = tp.diff()
    pos = raw_mf.where(delta > 0, 0.0)
    neg = raw_mf.where(delta < 0, 0.0)
    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum()
    ratio = pos_sum / neg_sum.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + ratio))
    # All up-days in the window (neg_sum == 0) -> MFI 100, by definition.
    out = out.where(neg_sum != 0, 100.0)
    val = out.iloc[-1]
    return None if pd.isna(val) else float(val)


def atr_momentum(atr: float | None, atr_5ema: float | None) -> float | None:
    """ATR / ATR_5EMA. >1 = volatility expanding (a CFM negative), <1 = contracting."""
    if atr is None or atr_5ema is None or atr_5ema == 0:
        return None
    return atr / atr_5ema


# ---------------------------------------------------------------------------
# Input computation — every indicator input, computed once per ticker.
# ---------------------------------------------------------------------------
def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["High"].astype(float), df["Low"].astype(float), df["Close"].astype(float)
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def _atr_series(df: pd.DataFrame, window: int = config.ATR_WINDOW) -> pd.Series:
    """Wilder ATR as a series (matches indicators.atr's last value)."""
    return _true_range(df).ewm(alpha=1 / window, min_periods=window, adjust=False).mean()


def _obv_series(df: pd.DataFrame) -> pd.Series:
    close, vol = df["Close"].astype(float), df["Volume"].astype(float)
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * vol).cumsum()


def compute_inputs(df: pd.DataFrame | None) -> dict:
    """Compute all indicator inputs for one ticker, once. Returns scalars plus the
    two series the metric functions consume directly (the MA50 series for its
    slope and the OHLCV columns for MFI). Missing history yields None scalars."""
    if df is None or df.empty:
        return {"price": None, "ma21": None, "ma50": None, "ma200": None,
                "atr": None, "atr_5ema": None, "obv": None, "obv_20ema": None,
                "volume": None, "volume_5ma": None, "volume_20ma": None,
                "ma50_series": None, "high": None, "low": None, "close": None, "volume_series": None}

    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    ma50_series = close.rolling(50).mean()

    atr_valid = _atr_series(df).dropna()
    atr = float(atr_valid.iloc[-1]) if not atr_valid.empty else None
    atr_5ema = (float(atr_valid.ewm(span=5, adjust=False).mean().iloc[-1])
                if not atr_valid.empty else None)

    obv_series = _obv_series(df)
    obv = float(obv_series.iloc[-1]) if not obv_series.empty else None
    obv_20ema = float(obv_series.ewm(span=20, adjust=False).mean().iloc[-1]) if not obv_series.empty else None

    vol_5 = vol.rolling(5).mean().iloc[-1]
    vol_20 = vol.rolling(config.VOL_AVG_WINDOW).mean().iloc[-1]

    return {
        "price": indicators.last(df),
        "ma21": indicators.sma(df, 21),
        "ma50": indicators.sma(df, 50),
        "ma200": indicators.sma(df, 200),
        "atr": atr,
        "atr_5ema": atr_5ema,
        "obv": obv,
        "obv_20ema": obv_20ema,
        "volume": float(vol.iloc[-1]),
        "volume_5ma": None if pd.isna(vol_5) else float(vol_5),
        "volume_20ma": None if pd.isna(vol_20) else float(vol_20),
        "ma50_series": ma50_series,
        "high": df["High"].astype(float),
        "low": df["Low"].astype(float),
        "close": close,
        "volume_series": vol,
    }


def metrics_for(df: pd.DataFrame | None, spy_df: pd.DataFrame | None,
                sector_df: pd.DataFrame | None) -> dict:
    """All scorecard metric values for one ticker (the row's numeric fields).
    Pure over the three frames; relative strength reuses indicators.rs3m (RS3M vs
    Sector = RS3M vs SPY - sector's RS3M vs SPY, exactly as the entry gate does)."""
    inp = compute_inputs(df)

    rs_vs_spy = indicators.rs3m(df, spy_df) if (df is not None and spy_df is not None) else None
    sector_rs_vs_spy = (indicators.rs3m(sector_df, spy_df)
                        if (sector_df is not None and spy_df is not None) else None)
    rs_vs_sector = (round(rs_vs_spy - sector_rs_vs_spy, 2)
                    if (rs_vs_spy is not None and sector_rs_vs_spy is not None) else None)

    obv_above, obv_dist = obv_vs_ema(inp["obv"], inp["obv_20ema"])
    return {
        "price": inp["price"],
        "rs3m_vs_spy": rs_vs_spy,
        "rs3m_vs_sector": rs_vs_sector,
        "pct_above_ma21": pct_above_ma21(inp["price"], inp["ma21"]),
        "pct_above_ma200": pct_above_ma200(inp["price"], inp["ma200"]),
        "atr_extension": atr_extension(inp["price"], inp["ma21"], inp["atr"]),
        "below_ma50": below_ma50(inp["price"], inp["ma50"]),
        "below_ma200": below_ma200(inp["price"], inp["ma200"]),
        "ma50_slope": ma50_slope(inp["ma50_series"]),
        "volume_ratio": volume_ratio(inp["volume"], inp["volume_20ma"]),
        "volume_acceleration": volume_acceleration(inp["volume_5ma"], inp["volume_20ma"]),
        "obv_above_ema": obv_above,
        "obv_pct_distance": obv_dist,
        "mfi": mfi(inp["high"], inp["low"], inp["close"], inp["volume_series"]),
        "atr_momentum": atr_momentum(inp["atr"], inp["atr_5ema"]),
    }


# ---------------------------------------------------------------------------
# Composite verdict (Section 3).
# ---------------------------------------------------------------------------
def compute_verdict(metrics: dict) -> dict:
    """Map a metrics dict to {"verdict": GO|CAUTION|AVOID, "reasons": [...]}.

    AVOID dominates CAUTION; within a tier every applicable reason is collected
    (not just the first). A metric that is None can't be judged, so its rule is
    skipped rather than firing. This is the CFM-suitability lens only — callers
    layer it on top of the existing 4-level entry gate (see `scorecard`)."""
    avoid: list[str] = []
    caution: list[str] = []

    # --- AVOID rules ---
    rs_sec = metrics.get("rs3m_vs_sector")
    if rs_sec is not None and rs_sec < T.RS3M_VS_SECTOR_MIN:
        avoid.append(f"rs3m_vs_sector negative ({rs_sec:+.1f}%)")
    if metrics.get("below_ma200") is True:
        avoid.append("price below MA200")
    ext = metrics.get("atr_extension")
    if ext is not None and ext > T.ATR_EXTENSION_MAX:
        avoid.append(f"ATR extension {ext:.1f} > {T.ATR_EXTENSION_MAX:g} (overextended)")
    if avoid:
        return {"verdict": "AVOID", "reasons": avoid}

    # --- CAUTION rules (only when not already AVOID) ---
    m = metrics.get("mfi")
    if m is not None and (m < T.MFI_MIN or m > T.MFI_MAX):
        caution.append(f"MFI {m:.0f} outside {T.MFI_MIN:g}–{T.MFI_MAX:g} band")
    vr = metrics.get("volume_ratio")
    if vr is not None and vr < T.VOLUME_RATIO_MIN:
        caution.append(f"volume ratio {vr:.2f} < {T.VOLUME_RATIO_MIN:g} (thin participation)")
    atrm = metrics.get("atr_momentum")
    if atrm is not None and atrm > T.ATR_MOMENTUM_MAX:
        caution.append(f"ATR expanding ({atrm:.2f} > {T.ATR_MOMENTUM_MAX:g}) — wants APP, not CFM")
    if metrics.get("below_ma50") is True:
        caution.append("price below MA50")
    slope = metrics.get("ma50_slope")
    if slope is not None and slope < 0:
        caution.append(f"MA50 rolling over (slope {slope:+.2f})")
    if caution:
        return {"verdict": "CAUTION", "reasons": caution}

    return {"verdict": "GO", "reasons": []}


# ---------------------------------------------------------------------------
# Orchestration — holdings list -> rows. Layers the verdict on the entry gate.
# ---------------------------------------------------------------------------
_ROUND = {  # display rounding per field (verdict is computed from full precision)
    "price": 2, "rs3m_vs_spy": 2, "rs3m_vs_sector": 2, "pct_above_ma21": 1,
    "pct_above_ma200": 1, "atr_extension": 2, "ma50_slope": 3, "volume_ratio": 2,
    "volume_acceleration": 2, "obv_pct_distance": 1, "mfi": 1, "atr_momentum": 2,
}

_GATE_LEVEL_NAMES = {1: "market regime", 2: "sector strength",
                     3: "stock beating peers", 4: "consolidating"}

# Only the stock's OWN gate legs decide the verdict: Level 3 (beats peers) and
# Level 4 (consolidating). The market-wide legs — Level 1 (regime) and Level 2
# (sector strength) — are EXCLUDED: they're context, not a property of the stock,
# and letting them blanket the table to AVOID would defeat the per-stock
# comparison exactly when it's most wanted (e.g. a yellow regime, or a sector
# that's merely lagging while the stock itself leads its peers and consolidates).
_STOCK_GATE_LEVELS = (3, 4)


def _round_row(metrics: dict) -> dict:
    out = dict(metrics)
    for key, digits in _ROUND.items():
        if out.get(key) is not None:
            out[key] = round(out[key], digits)
    return out


def _failed_stock_gate_level(gate: dict | None) -> int | None:
    """First failing stock-level gate leg (Level 3, then 4), or None.

    Reads the per-level pass flags from the gate's `levels` list when present, so
    a stock-level miss is caught even behind an earlier (regime/sector) failure —
    the gate computes all four levels regardless of stop-on-fail. Falls back to
    the stop-on-fail `cleared_level` (first failing = cleared+1) when `levels` is
    absent. The market-wide legs (Level 1 regime, Level 2 sector) never
    short-circuit here, by design."""
    if not gate:
        return None
    levels = gate.get("levels")
    if levels:
        by_level = {lv.get("level"): lv for lv in levels}
        for lvl in _STOCK_GATE_LEVELS:
            leg = by_level.get(lvl)
            if leg is not None and not leg.get("pass", False):
                return lvl
        return None
    first_failed = (gate.get("cleared_level", 0) or 0) + 1
    return first_failed if first_failed in _STOCK_GATE_LEVELS else None


def score_ticker(ticker: str, spy_df: pd.DataFrame | None, sector_etf: str,
                 sector_df: pd.DataFrame | None, gate: dict | None = None,
                 has_weeklies: bool | None = None) -> dict:
    """One scorecard row: numeric metrics + the composite verdict.

    Only the stock's own gate legs decide it: a beats-peers (L3) or consolidating
    (L4) failure short-circuits the row to AVOID. The market-wide legs — Level 1
    (regime) and Level 2 (sector strength) — are excluded, so stocks stay
    comparable on their own merits. The verdict is computed from the SAME rounded
    numbers shown in the row, so a displayed value can never silently disagree with
    its verdict. Numeric fields are always fully populated, even on a gate
    short-circuit.

    `has_weeklies` (True/False/None) is carried through untouched — CFM can't trade
    a monthly-only chain, so the UI hides/flags those, but it does NOT change the
    verdict (a strong name that simply lacks weeklies still scores on its merits)."""
    df = data_handler.get_daily(ticker)
    metrics = metrics_for(df, spy_df, sector_df)
    row = _round_row(metrics)
    row["ticker"] = ticker.upper()
    row["sector"] = sector_etf
    row["has_weeklies"] = has_weeklies
    if gate is not None:
        row["gate_cleared_level"] = gate.get("cleared_level", 0)

    # Juice adequacy (history-implied weekly extrinsic / LEAP cost) + next
    # earnings — so weak-premium and earnings-soon names are visible BEFORE the
    # Execute tab. Earnings is cache/override-only here: this sweeps hundreds of
    # tickers and must never trigger a provider fetch storm.
    import account_gate
    import earnings as earnings_mod
    est = account_gate.juice_estimate(ticker, df)
    target = account_gate.weekly_yield_target_pct()
    row["juice_weekly_pct"] = est["weekly_yield_pct"]
    row["juice_target_pct"] = target
    row["juice_ok"] = (None if est["weekly_yield_pct"] is None
                       else bool(est["weekly_yield_pct"] >= target))
    earn = earnings_mod.cached_earnings(ticker)
    row["earnings_date"] = earn.get("date")
    row["earnings_days"] = earn.get("days_until")

    failed = _failed_stock_gate_level(gate)
    if failed is not None:
        name = _GATE_LEVEL_NAMES.get(failed, "")
        row["verdict"] = "AVOID"
        row["reasons"] = [f"fails entry gate level {failed}"
                          + (f" ({name})" if name else "")]
        return row

    # Judge the rounded values the UI actually shows, so a verdict can never
    # disagree with the number displayed next to it (sub-rounding boundaries).
    verdict = compute_verdict(row)
    row["verdict"] = verdict["verdict"]
    row["reasons"] = verdict["reasons"]
    return row


def _compute_scorecard(names: list[str]) -> dict:
    import logging_handler as log
    import screening  # local imports avoid any import-time cycle
    import weeklies

    # Resolve each ticker's sector once; collect the sector ETFs we'll need.
    sector_of = {t: (sector_data.sector_for(t) or "") for t in names}
    etfs = sorted({e for e in sector_of.values() if e})

    data_handler.prefetch([config.BENCHMARK] + etfs + names)
    weeklies.prefetch(names)  # warm the weeklies cache in parallel (no-op if disabled)
    spy = data_handler.get_daily(config.BENCHMARK)
    sector_frames = {e: data_handler.get_daily(e) for e in etfs}

    rows = []
    for t in names:
        etf = sector_of[t]
        try:
            gate = screening.entry_gate(t) if etf else None
        except Exception:  # noqa: BLE001 — a gate failure must never sink the row
            gate = None
        rows.append(score_ticker(t, spy, etf, sector_frames.get(etf), gate,
                                 has_weeklies=weeklies.has_weeklies(t)))

    rows.sort(key=lambda r: (r["sector"], r["ticker"]))
    return {"as_of": log.utcnow(), "results": rows}


def scorecard(tickers: list[str] | None = None) -> dict:
    """Build the scorecard for a list of tickers (default: every holding across
    every sector). Warms the cache for SPY + sector ETFs + the tickers in one
    parallel batch, then computes a row each — reusing the existing 4-level entry
    gate, where only a stock-level (Level 3/4) failure short-circuits the verdict
    (a yellow regime does not blanket the table). Rows are grouped-friendly (each
    carries its sector) and sorted by sector then ticker.

    The full-universe sweep (tickers=None) is expensive (indicator math across
    every holding) and purely market-driven — it doesn't depend on the
    operator's own account state — so it's memoized with screening's short-TTL
    cache. This matters because the Scan tab mounts both the Scorecard panel
    and the Ready-to-Enter panel, which would otherwise each trigger their own
    full sweep concurrently on every page load. An explicit ticker subset
    (e.g. one ticker's entry snapshot at trade time) always computes fresh."""
    if tickers:
        names = [t.strip().upper() for t in tickers if t.strip()]
        return _compute_scorecard(names)

    import screening  # local import avoids any import-time cycle
    names = sector_data.all_tickers()
    return screening._cached("scorecard:full", lambda: _compute_scorecard(names))
