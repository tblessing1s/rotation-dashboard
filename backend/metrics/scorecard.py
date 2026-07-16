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
import rs_state as rss
import scan_score
import scan_verdict
import sector_data
import structure_classifier
import symbol_genius

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
    Pure over the three frames; relative strength reuses indicators.rs3m. RS3M vs
    Sector is the DIRECT rs3m(stock, sector_etf) ratio over the same 63-day
    lookback (the same figure the kill switch and entry gate now use), NOT the
    vs-SPY difference approximation."""
    inp = compute_inputs(df)

    rs_vs_spy = indicators.rs3m(df, spy_df) if (df is not None and spy_df is not None) else None
    rs_vs_sector = (indicators.rs3m(df, sector_df)
                    if (df is not None and sector_df is not None) else None)

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
    layer it on top of the existing 4-level entry gate (see `scorecard`).

    ETFs run as a lower-vol income sleeve, not growth leaders, so — matching the
    lower juice / beats-SPY bars and the entry gate's beats-sector waiver — the
    growth-momentum filters are waived when `metrics["is_etf"]` is set: the
    beats-assigned-sector AVOID and the MFI-band / thin-volume / ATR-expansion
    CAUTIONs. The genuine risk rails still apply to ETFs — below MA200,
    over-extension, and the MA50 trend filters — so a broken-trend or overextended
    ETF is still caught."""
    avoid: list[str] = []
    caution: list[str] = []
    is_etf = bool(metrics.get("is_etf"))

    # --- AVOID rules ---
    rs_sec = metrics.get("rs3m_vs_sector")
    # An ETF isn't required to outrun its (assigned or own) broad sector — the
    # income sleeve waives this leg, same as the entry gate does.
    if not is_etf and rs_sec is not None and rs_sec < T.RS3M_VS_SECTOR_MIN:
        avoid.append(f"rs3m_vs_sector negative ({rs_sec:+.1f}%)")
    if metrics.get("below_ma200") is True:
        avoid.append("price below MA200")
    ext = metrics.get("atr_extension")
    if ext is not None and ext > T.ATR_EXTENSION_MAX:
        avoid.append(f"ATR extension {ext:.1f} > {T.ATR_EXTENSION_MAX:g} (overextended)")
    if avoid:
        return {"verdict": "AVOID", "reasons": avoid}

    # --- CAUTION rules (only when not already AVOID) ---
    # The MFI band, thin-participation volume floor, and ATR-expansion check are
    # growth-stock momentum filters (a coiling single name); a low-vol ETF income
    # sleeve is judged on trend health only, so these three are waived for ETFs.
    m = metrics.get("mfi")
    if not is_etf and m is not None and (m < T.MFI_MIN or m > T.MFI_MAX):
        caution.append(f"MFI {m:.0f} outside {T.MFI_MIN:g}–{T.MFI_MAX:g} band")
    vr = metrics.get("volume_ratio")
    if not is_etf and vr is not None and vr < T.VOLUME_RATIO_MIN:
        caution.append(f"volume ratio {vr:.2f} < {T.VOLUME_RATIO_MIN:g} (thin participation)")
    atrm = metrics.get("atr_momentum")
    if not is_etf and atrm is not None and atrm > T.ATR_MOMENTUM_MAX:
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
                     3: "stock beating peers", 3.5: "structure", 4: "consolidating"}

# Only the stock's OWN gate legs decide the verdict: Level 3 (beats peers),
# Level 3.5 (structure — the classifier's entrability), and Level 4 (consolidating).
# The market-wide legs — Level 1 (regime) and Level 2 (sector strength) — are
# EXCLUDED: they're context, not a property of the stock, and letting them blanket
# the table to AVOID would defeat the per-stock comparison exactly when it's most
# wanted (e.g. a yellow regime, or a sector that's merely lagging while the stock
# itself leads its peers and consolidates).
_STOCK_GATE_LEVELS = (3, 3.5, 4)


def _round_row(metrics: dict) -> dict:
    out = dict(metrics)
    for key, digits in _ROUND.items():
        if out.get(key) is not None:
            out[key] = round(out[key], digits)
    return out


def _gate_level_detail(gate: dict | None, level: int) -> dict:
    """The ``detail`` dict for one entry-gate level (or {} when absent). Used to
    lift the stock lights / right-spot off the gate onto the scorecard row."""
    if not gate:
        return {}
    for lv in gate.get("levels") or []:
        if lv.get("level") == level:
            return lv.get("detail") or {}
    return {}


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


def _apply_price_override(df, price_override):
    """Return a copy of the daily frame with its LAST bar's close set to a live
    quote (and its high/low widened to stay coherent), or the frame unchanged
    when there's no override / no data. Never mutates the cached frame."""
    if price_override is None or df is None or df.empty:
        return df
    df = df.copy()
    px = float(price_override)
    ci = df.columns.get_loc("Close")
    hi = df.columns.get_loc("High")
    lo = df.columns.get_loc("Low")
    df.iat[-1, ci] = px
    df.iat[-1, hi] = max(float(df.iat[-1, hi]), px)
    df.iat[-1, lo] = min(float(df.iat[-1, lo]), px)
    return df


def _ext_trigger_context(df: pd.DataFrame | None) -> dict:
    """The extra observed values the two Level-4 ESTIMATED triggers need for a
    crude days-to-trigger (``scan_triggers._estimate_days``): the ATR beyond the
    max in ATR units, the recent MA21 daily rise ($/day), and the current ATR.
    Pure over the frame; None-safe. All estimation is labelled EST downstream."""
    if df is None or len(df) < 26:
        return {}
    price = indicators.last(df)
    atr = indicators.atr(df)
    ma21 = indicators.sma(df, 21)
    ma21_prev = indicators.sma(df.iloc[:-5], 21)
    ext = indicators.atr_extension(df)
    momentum = indicators.atr_momentum(df)
    out: dict = {}
    if None not in (ext, atr) and atr:
        excess = ext - config.SPOT_ATR_EXTENSION_MAX
        rise = (ma21 - ma21_prev) / 5.0 if (ma21 is not None and ma21_prev is not None) else None
        out["extension"] = {"excess_atr": excess if excess > 0 else None,
                            "ma21_rise_per_day": rise, "atr": atr}
    if momentum is not None:
        excess_m = momentum - config.SPOT_ATR_MOMENTUM_MAX
        out["atr_5d_ema"] = {"momentum_excess": excess_m if excess_m > 0 else None}
    return out


def score_ticker(ticker: str, spy_df: pd.DataFrame | None, sector_etf: str,
                 sector_df: pd.DataFrame | None, gate: dict | None = None,
                 has_weeklies: bool | None = None, price_override: float | None = None,
                 regime_color: str | None = None) -> dict:
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
    verdict (a strong name that simply lacks weeklies still scores on its merits).

    `price_override` (a live quote) replaces the last daily-bar close before the
    metrics are computed, so an on-demand refresh shows the CURRENT price and the
    price-derived legs (%>MA, below-MA, ATR extension) all reflect it together —
    daily bars are end-of-day and would otherwise leave the row stale intraday."""
    df = data_handler.get_daily(ticker)
    df = _apply_price_override(df, price_override)
    metrics = metrics_for(df, spy_df, sector_df)
    # A sector ETF scored as its own candidate has no distinct peer sector to
    # beat — rs3m_vs_sector would otherwise compute to a tautological ~0 every
    # time (same frame vs itself), which reads as a real number, not "N/A".
    is_sector_etf = bool(sector_etf) and ticker.upper() == sector_etf.upper()
    if is_sector_etf:
        metrics["rs3m_vs_sector"] = None
    row = _round_row(metrics)
    row["ticker"] = ticker.upper()
    row["sector"] = sector_etf
    row["is_sector_etf"] = is_sector_etf
    row["has_weeklies"] = has_weeklies
    if gate is not None:
        row["gate_cleared_level"] = gate.get("cleared_level", 0)
        # Surface the per-name Genius lights + verdict + right-spot from the gate so
        # the Scorecard and Ready-to-Enter can render the four-light row at a glance
        # (they're already computed in the entry gate's Level 3/4 detail — no
        # recompute). None-safe for the synthetic gate dicts used in tests.
        l3 = _gate_level_detail(gate, 3)
        l4 = _gate_level_detail(gate, 4)
        row["lights"] = l3.get("lights")
        row["stock_greens"] = l3.get("greens")
        row["stock_verdict"] = l3.get("verdict")
        row["stock_vetoes"] = l3.get("vetoes")
        row["right_spot"] = l4.get("right_spot") or l3.get("right_spot")

    # Juice adequacy (history-implied weekly extrinsic / LEAP cost) + next
    # earnings — so weak-premium and earnings-soon names are visible BEFORE the
    # Execute tab. Earnings is cache/override-only here: this sweeps hundreds of
    # tickers and must never trigger a provider fetch storm.
    import account_gate
    import earnings as earnings_mod
    est = account_gate.juice_estimate(ticker, df)
    # ETFs are judged against the lower ETF income-sleeve bar, not the growth bar.
    row["is_etf"] = sector_data.is_etf(ticker)
    target = account_gate.weekly_yield_target_pct(ticker)
    row["juice_weekly_pct"] = est["weekly_yield_pct"]
    # NET juice/week (gross minus LEAP model burn, with slippage) — the ranking
    # key. Kept alongside gross so the panel can show both; ranking sorts on net.
    row["net_juice_weekly_pct"] = est.get("net_weekly_yield_pct")
    row["burn_weekly_per_share"] = est.get("burn_weekly_per_share")
    row["juice_target_pct"] = target
    row["juice_ok"] = (None if est["weekly_yield_pct"] is None
                       else bool(est["weekly_yield_pct"] >= target))
    earn = earnings_mod.cached_earnings(ticker)
    row["earnings_date"] = earn.get("date")
    row["earnings_days"] = earn.get("days_until")

    # IV Rank (drawer context) — sourced from the local IV-history store the app
    # already accrues (option-chain views + nightly maintenance); NO new provider
    # call. A juicy row sitting at a high IVR deserves suspicion ("don't be lured
    # by high juice"). None below the store's minimum sample, never a guess.
    try:
        import iv_history
        ivr = iv_history.iv_rank(ticker)
        row["iv_rank"] = ivr.get("iv_rank")
        row["iv_percentile"] = ivr.get("iv_percentile")
    except Exception:  # noqa: BLE001 — IVR is a drawer readout, never sinks a row
        row["iv_rank"] = None
        row["iv_percentile"] = None

    # Scan-restructure signals — the per-symbol columns SYM | BASE | INST | VERDICT.
    # Symbol Genius (the four-light SYM), the structure classifier (BASE + INST from
    # ONE call, display-only split), and the composed, CANONICAL `verdict`
    # (worst-signal-wins of the INVISIBLE market regime + SYM + structure
    # entrability). A RED regime forces every verdict to BLOCKED. Computed for every
    # row (even a gate short-circuit below) so the scan table is complete.
    #
    # The older GO/CAUTION/AVOID CFM-suitability lens is retained as `suitability`
    # (a demoted drawer readout) and is what the internal queue / recommendation /
    # refresh pipeline reads — it carries its own regime handling and measures a
    # different thing (stock-momentum suitability, not the regime-aware composition).
    sym = symbol_genius.compute(df)
    cls = structure_classifier.classify(df)
    composed = scan_verdict.compose_verdict(regime_color, sym["color"],
                                            cls["base_stage"], cls["inst_flow"])
    row["sym"] = sym["color"]
    row["sym_greens"] = sym["greens"]
    row["base_stage"] = cls["base_stage"]
    row["inst_flow"] = cls["inst_flow"]
    row["structure_entrability"] = composed["structure_entrability"]

    # VERDICT COMPLETENESS (Phase-0 fix): the canonical verdict is worst-of the
    # three SIGNAL inputs composed above AND every failing FULL-gate block — so a
    # name extended past the Level-4 right spot can never read READY (the AAPL bug).
    # Blocks are a READ of the gate ALREADY computed for this row (never a re-eval);
    # Level 5 (account) is layered as a per-request overlay in /api/scan/ready where
    # the account context Execute uses is loaded (see app.api_scan_ready). Triggers
    # are the forward-looking "path to READY" annotations over the SAME evaluation.
    import scan_triggers
    ext_context = _ext_trigger_context(df)
    blocks = scan_triggers.gate_blocks(gate, ext_context=ext_context)
    rv = scan_triggers.compose_row_verdict(composed, blocks)
    row["verdict"] = rv["verdict"]                       # the canonical scan verdict
    row["verdict_reasons"] = list(rv["reasons"])
    row["binding"] = rv["binding"]                       # structured first-fail (Q9)
    row["triggers"] = rv["triggers"]                     # per-block forward triggers
    row["path_to_ready"] = scan_triggers.path_to_ready(rv["triggers"])
    row["eligible_days"] = scan_triggers.earliest_eligible_days(rv["triggers"])
    row["bench"] = scan_triggers.is_bench(rv["verdict"], rv["triggers"])

    # Two-speed RS SHADOW — vs Sector (the table's primary) + vs SPY (drawer). Level
    # reuses the displayed RS3M; slope is the RS-line-EMA direction. A sector ETF has
    # no distinct peer sector, so its vs-Sector RS is N/A (same rule as rs3m_vs_sector).
    # SHADOW ONLY: never feeds the composed verdict above, never blocks, never sizes.
    rs_sec = (rss.rs_state(df, sector_df) if (not is_sector_etf and sector_df is not None)
              else {"state": None, "level": None, "slope": None})
    rs_spy = rss.rs_state(df, spy_df) if spy_df is not None else {"state": None, "level": None, "slope": None}
    row["rs_state"] = rs_sec["state"]            # vs Sector — the table column
    row["rs_level"] = rs_sec["level"]
    row["rs_slope"] = rs_sec["slope"]
    row["rs_state_spy"] = rs_spy["state"]        # vs SPY — the drawer readout
    row["rs_spy_level"] = rs_spy["level"]
    row["rs_spy_slope"] = rs_spy["slope"]

    # Gated Phase-0 exception: a TURNING vs-Sector RS on an already-non-READY row is
    # an informational WATCH annotation (relative strength recovering) appended to the
    # CANONICAL verdict's reasons — never a second verdict, never a verdict change.
    annotation = rss.turning_watch_reason(row["verdict"], row["rs_state"])
    if annotation:
        row["verdict_reasons"].append(annotation)

    # Composite SCORE (0–10) SHADOW — a pure rank over the already-computed row
    # inputs. ZERO authority: not read by the verdict, gate, /api/scan/ready, sizing,
    # or recommendations (see scan_score.py). sector_rs1m is the sector ETF's own
    # strength vs SPY (one cheap arithmetic call over the already-cached frames).
    sector_rs1m = (indicators.rs1m(sector_df, spy_df)
                   if (sector_df is not None and spy_df is not None) else None)
    scored = scan_score.compute_score(
        inst_flow=cls["inst_flow"], base_stage=cls["base_stage"],
        base_count=cls["signals"].get("base_count"), rs_state_value=row["rs_state"],
        sector_rs1m=sector_rs1m, atr_momentum=row.get("atr_momentum"),
        pct_above_ma21=row.get("pct_above_ma21"),
        net_juice_weekly_pct=row.get("net_juice_weekly_pct"))
    row["score"] = scored["score"]
    row["score_parts"] = scored["parts"]
    row["sector_rs1m"] = None if sector_rs1m is None else round(sector_rs1m, 2)

    # `suitability` = the CFM-suitability lens (stock-level gate short-circuit, else
    # the GO/CAUTION/AVOID metric rules). Not the headline verdict — a demoted signal.
    failed = _failed_stock_gate_level(gate)
    if failed is not None:
        name = _GATE_LEVEL_NAMES.get(failed, "")
        row["suitability"] = "AVOID"
        row["suitability_reasons"] = [f"fails entry gate level {failed}"
                                      + (f" ({name})" if name else "")]
        return row

    # Judge the rounded values the UI actually shows, so the suitability can never
    # disagree with the number displayed next to it (sub-rounding boundaries).
    suitability = compute_verdict(row)
    row["suitability"] = suitability["verdict"]
    row["suitability_reasons"] = suitability["reasons"]
    return row


def _compute_scorecard(names: list[str], price_overrides: dict | None = None) -> dict:
    import logging_handler as log
    import screening  # local imports avoid any import-time cycle
    import weeklies

    price_overrides = price_overrides or {}

    # Resolve each ticker's sector once; collect the sector ETFs we'll need.
    sector_of = {t: (sector_data.sector_for(t) or "") for t in names}
    etfs = sorted({e for e in sector_of.values() if e})

    data_handler.prefetch([config.BENCHMARK] + etfs + names)
    weeklies.prefetch(names)  # warm the weeklies cache in parallel (no-op if disabled)
    spy = data_handler.get_daily(config.BENCHMARK)
    sector_frames = {e: data_handler.get_daily(e) for e in etfs}

    # The invisible market regime — ONE read for the whole sweep — feeds the composed
    # scan verdict (a RED regime blocks every row). Best-effort: a regime failure
    # degrades regime_color to None (composed verdict then never emits READY).
    try:
        regime_color = screening.regime().get("status")
    except Exception:  # noqa: BLE001
        regime_color = None

    rows = []
    for t in names:
        etf = sector_of[t]
        try:
            gate = screening.entry_gate(t) if etf else None
        except Exception:  # noqa: BLE001 — a gate failure must never sink the row
            gate = None
        rows.append(score_ticker(t, spy, etf, sector_frames.get(etf), gate,
                                 has_weeklies=weeklies.has_weeklies(t),
                                 price_override=price_overrides.get(t.upper()),
                                 regime_color=regime_color))

    rows.sort(key=lambda r: (r["sector"], r["ticker"]))
    return {"as_of": log.utcnow(), "results": rows}


def scorecard(tickers: list[str] | None = None, price_overrides: dict | None = None) -> dict:
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
        return _compute_scorecard(names, price_overrides=price_overrides)

    import screening  # local import avoids any import-time cycle
    names = sector_data.all_tickers()
    return screening._cached("scorecard:full", lambda: _compute_scorecard(names))
