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
import scan_triggers
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


def metrics_for(df: pd.DataFrame | None, spy_df: pd.DataFrame | None) -> dict:
    """All scorecard metric values for one ticker (the row's numeric fields).
    Pure over the two frames; relative strength reuses indicators.rs3m — the
    DIRECT ratio over a 63-day lookback, the same figure the kill switch uses.

    The peer/sector frame this used to take went with RS3M-vs-Sector on
    2026-08-21 (docs/decision-2026-08-21-remove-sector-rs.md). Dropping the
    PARAMETER matters as much as dropping the metric: every caller was fetching
    a frame for it, and a warm-cache read per dividend-sleeve row per sweep is
    not free."""
    inp = compute_inputs(df)

    rs_vs_spy = indicators.rs3m(df, spy_df) if (df is not None and spy_df is not None) else None

    obv_above, obv_dist = obv_vs_ema(inp["obv"], inp["obv_20ema"])
    return {
        "price": inp["price"],
        "rs3m_vs_spy": rs_vs_spy,
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
    lower juice / beats-SPY bars — the growth-momentum filters are waived when
    `metrics["is_etf"]` is set: the MFI-band / thin-volume / ATR-expansion
    CAUTIONs. The genuine risk rails still apply to ETFs — below MA200,
    over-extension, and the MA50 trend filters — so a broken-trend or overextended
    ETF is still caught."""
    avoid: list[str] = []
    caution: list[str] = []
    # Non-blocking observations. A note NEVER changes the verdict — it is how a
    # suppressed rule still says what it saw, so "no CAUTION" is distinguishable
    # from "nothing looked at".
    notes: list[str] = []
    is_etf = bool(metrics.get("is_etf"))

    # --- AVOID rules ---
    # The rs3m_vs_sector AVOID led this list until 2026-08-21, when
    # RS3M-vs-Sector was removed system-wide
    # (docs/decision-2026-08-21-remove-sector-rs.md). Note what that widened:
    # `suitability` gates the recommendation pool, the internal queue and the
    # intraday hot-refresh set, so names that used to be excluded here now reach
    # all three. Deliberate, not an oversight.
    if metrics.get("below_ma200") is True:
        avoid.append("price below MA200")
    ext = metrics.get("atr_extension")
    if ext is not None and ext > T.ATR_EXTENSION_MAX:
        avoid.append(f"ATR extension {ext:.1f} > {T.ATR_EXTENSION_MAX:g} (overextended)")
    if avoid:
        return {"verdict": "AVOID", "reasons": avoid, "notes": notes}

    # --- CAUTION rules (only when not already AVOID) ---
    # The MFI band, thin-participation volume floor, and ATR-expansion check are
    # growth-stock momentum filters (a coiling single name); a low-vol ETF income
    # sleeve is judged on trend health only, so these three are waived for ETFs.
    m = metrics.get("mfi")
    if not is_etf and m is not None and (m < T.MFI_MIN or m > T.MFI_MAX):
        caution.append(f"MFI {m:.0f} outside {T.MFI_MIN:g}–{T.MFI_MAX:g} band")
    # PHASE-AWARE thin-volume floor (TRAVIS_EXTENSION). The THRESHOLD IS UNCHANGED
    # (T.VOLUME_RATIO_MIN, still 0.8) — what changes is where it APPLIES.
    #
    # The flat floor is phase-blind, and that made it fire on exactly the setups
    # Level 4 exists to find: inside a consolidation, LOW volume is CONSTRUCTIVE
    # (supply drying up), not thin participation. Note that the rule two lines
    # below already penalizes ATR EXPANSION — i.e. this lens already assumes a
    # coiling name — so firing on the quiet volume that same coil produces had the
    # two CAUTIONs pulling opposite directions on one chart.
    #
    # Outside a consolidation (a breakout / entry-day confirmation context) the
    # behavior is byte-identical to before: same threshold, same message. The phase
    # flag is a pure READ of the already-computed Level-4 check results
    # (chart_structure.consolidation_phase) and fails CLOSED — absent a gate it is
    # False, which is the pre-change path.
    vr = metrics.get("volume_ratio")
    if not is_etf and vr is not None and vr < T.VOLUME_RATIO_MIN:
        if metrics.get("consolidation_phase"):
            notes.append(f"volume: drying up (constructive) — {vr:.2f} < "
                         f"{T.VOLUME_RATIO_MIN:g} inside a consolidation")
        else:
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
        return {"verdict": "CAUTION", "reasons": caution, "notes": notes}

    return {"verdict": "GO", "reasons": [], "notes": notes}


# ---------------------------------------------------------------------------
# Orchestration — holdings list -> rows. Layers the verdict on the entry gate.
# ---------------------------------------------------------------------------
_ROUND = {  # display rounding per field (verdict is computed from full precision)
    "price": 2, "rs3m_vs_spy": 2, "pct_above_ma21": 1,
    "pct_above_ma200": 1, "atr_extension": 2, "ma50_slope": 3, "volume_ratio": 2,
    "volume_acceleration": 2, "obv_pct_distance": 1, "mfi": 1, "atr_momentum": 2,
}

def _round_row(metrics: dict) -> dict:
    out = dict(metrics)
    for key, digits in _ROUND.items():
        if out.get(key) is not None:
            out[key] = round(out[key], digits)
    return out


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


def score_ticker(ticker: str, spy_df: pd.DataFrame | None, sector_etf: str,
                 sector_df: pd.DataFrame | None, gate: dict | None = None,
                 has_weeklies: bool | None = None, price_override: float | None = None,
                 regime_color: str | None = None,
                 profile_overrides: dict | None = None,
                 profile: str | None = None,
                 annual_dividend_yield_pct: float | None = None) -> dict:
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
    daily bars are end-of-day and would otherwise leave the row stale intraday.

    `profile` / `annual_dividend_yield_pct` are the income sleeve and the trailing
    yield the bulk caller already resolved for this ticker (schema v21) — passing
    them avoids resolving the same thing twice per row. Omitted, they are resolved
    here from `profile_overrides` (the operator's explicit ticker -> profile map)."""
    df = data_handler.get_daily(ticker)
    df = _apply_price_override(df, price_override)

    # --- Income profile + peer benchmark (schema v21, TRAVIS_EXTENSION) ---------
    # Resolved BEFORE the metrics so the vs-peer leg is computed against the right
    # peer group. The bulk caller resolves the profile once and passes it in; the
    # standalone path resolves it here. For JUICE_ENGINE the benchmark IS the sector
    # ETF, so every line below reduces to the pre-v21 behavior exactly.
    import income_profile
    import screening
    if profile is None or annual_dividend_yield_pct is None:
        profile, annual_div_pct = screening.resolve_profile_detail(
            ticker, overrides=profile_overrides)
    else:
        annual_div_pct = annual_dividend_yield_pct
    peer = income_profile.resolve(ticker, profile, sector_etf)
    profile = peer["profile"]
    metrics = metrics_for(df, spy_df)
    # The self-comparison guard that lived here (a name scored against ITSELF
    # computes a tautological ~0) went with the vs-sector metric it protected —
    # docs/decision-2026-08-21-remove-sector-rs.md.
    row = _round_row(metrics)
    row["ticker"] = ticker.upper()
    row["sector"] = sector_etf
    row["is_sector_etf"] = peer["is_sector_etf"]
    row["peer_benchmark"] = peer["benchmark"]
    row["is_own_benchmark"] = peer["is_own_benchmark"]
    row["has_weeklies"] = has_weeklies
    if gate is not None:
        rk0 = gate.get("ranking") or {}
        # Surface the per-name Genius lights + verdict + right-spot from the gate so
        # the Scorecard and Ready-to-Enter can render the four-light row at a glance
        # (they're already computed in the entry gate's Level 3/4 detail — no
        # recompute). None-safe for the synthetic gate dicts used in tests.
        row["lights"] = gate.get("lights")
        row["stock_greens"] = rk0.get("stock_greens")
        row["stock_verdict"] = gate.get("stock_verdict")
        row["stock_vetoes"] = gate.get("stock_vetoes")
        row["right_spot"] = rk0.get("right_spot")

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
    # What one 100-share lot of this name actually COSTS (schema v21). Pure — spot
    # x SHARES_PER_LOT, no account state — so the memoized market sweep stays
    # account-free and this row can be cached across requests. The affordability
    # COMPARISON happens at the API boundary, where the account context lives.
    row["lot_cost"] = est.get("shares_cost_per_lot")
    row["shares_per_lot"] = config.SHARES_PER_LOT
    row["juice_weekly_pct"] = est["weekly_yield_pct"]
    # NET juice/week (gross minus LEAP model burn, with slippage) — the ranking
    # key. Kept alongside gross so the panel can show both; ranking sorts on net.
    row["net_juice_weekly_pct"] = est.get("net_weekly_yield_pct")
    row["burn_weekly_per_share"] = est.get("burn_weekly_per_share")
    # LEAP theta burn as a % of LEAP cost/week = gross − net (the decay the income
    # has to overcome). Derived so the table can show gross · burn · net side by side
    # with gross − burn = net legible at a glance.
    row["burn_weekly_pct"] = (None if (row["juice_weekly_pct"] is None
                                       or row["net_juice_weekly_pct"] is None)
                              else round(row["juice_weekly_pct"] - row["net_juice_weekly_pct"], 2))
    row["juice_target_pct"] = target
    row["juice_ok"] = (None if est["weekly_yield_pct"] is None
                       else bool(est["weekly_yield_pct"] >= target))
    earn = earnings_mod.cached_earnings(ticker)
    row["earnings_date"] = earn.get("date")
    row["earnings_days"] = earn.get("days_until")

    # --- Combined weekly-equivalent yield (schema v21) --------------------------
    # TRAVIS_EXTENSION. Purely ADDITIVE row keys, using the profile already
    # resolved above: nothing here is appended to `blocks` below, so the canonical
    # verdict is bit-for-bit what it was before this block existed.
    _st = scan_triggers
    row["annual_dividend_yield_pct"] = annual_div_pct
    row["income_profile"] = profile
    row["income_profile_badge"] = income_profile.badge(profile)
    combined = _st.combined_weekly_yield(row["juice_weekly_pct"], annual_div_pct)
    row["combined_weekly_yield_pct"] = combined["combined_weekly_yield_pct"]
    row["dividend_weekly_pct"] = combined["dividend_weekly_pct"]
    row["dividend_known"] = combined["dividend_known"]
    # SHADOW ONLY — see scan_triggers.shadow_floor. Deliberately NOT appended to
    # `blocks`: that list is what carries verdict authority.
    row["shadow_floor"] = _st.shadow_floor(
        profile, row["juice_weekly_pct"], annual_div_pct,
        est.get("weekly_extrinsic_per_share"))

    # --- Trailing juice CAPACITY (SHADOW) --------------------------------------
    # TRAVIS_EXTENSION. Purely ADDITIVE row keys. Juice above is a SPOT reading and
    # cannot tell a normally-juicy name in IV compression (transient) from one that
    # is BUILT low-vol (structural); the trailing median of the combined weekly
    # yield can. ZERO AUTHORITY — juice_capacity's module docstring states the full
    # contract: nothing here is appended to `blocks`, so `row["verdict"]` (and
    # `bench` / `triggers` / `path_to_ready` with it) is bit-for-bit what it was
    # before this block existed.
    #
    # `short_strike` / `regime_color` are surfaced onto the row purely as
    # observation PROVENANCE — they record which strike and which tape the juice
    # figure was priced under, so a future move to a regime-aware capacity basis is
    # a re-derivation over retained inputs rather than a lost history. Both are
    # already in hand (the estimate dict, the sweep's single regime read): no
    # recompute, no provider call.
    #
    # The capacity READ is a pure lookup in the DATA_DIR telemetry store (memoized
    # on the file's mtime+size, so a full sweep parses it once, not once per row).
    # The floor it is shown against is the SHARE-denominated shadow floor resolved
    # immediately above — never `juice_target_pct`, which is LEAP-denominated and
    # not on the same scale.
    row["short_strike"] = est.get("short_strike")
    row["regime_color"] = regime_color
    try:
        import juice_capacity
        row["juice_capacity"] = juice_capacity.capacity(
            ticker, floor_pct=(row["shadow_floor"] or {}).get("floor_pct"))
    except Exception:  # noqa: BLE001 — a shadow readout never sinks a row
        row["juice_capacity"] = None

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

    # HV-RANK (HVR) — the pure-from-bars volatility-rank PROXY: where realized vol
    # sits in its own trailing-252d range. Available for EVERY swept name (unlike
    # true IVR, which only covers held/viewed chains), so the drawer always has a
    # volatility read. Labelled HVR — never mistaken for true IV rank.
    hvr = indicators.hv_rank(df)
    row["hv_rank"] = hvr.get("hv_rank")
    row["hv_percentile"] = hvr.get("hv_percentile")
    row["hv"] = hvr.get("hv")

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
    entrability = structure_classifier.structure_entrability(
        cls["base_stage"], cls["inst_flow"])
    row["sym"] = sym["color"]
    row["sym_greens"] = sym["greens"]
    # The individual SYM lights + the mandatory-core state, so the recalibration
    # record can re-derive the vote offline instead of trusting the composite.
    row["sym_lights"] = {k: (v or {}).get("signal")
                         for k, v in (sym.get("lights") or {}).items()}
    row["sym_core_green"] = sym.get("core_green")
    row["base_stage"] = cls["base_stage"]
    row["inst_flow"] = cls["inst_flow"]
    row["structure_entrability"] = entrability

    # ---- THE VETO SET. This is the only thing that blocks. -------------------
    # The scan answers "may I enter?" here and "how good is it?" in the ranker
    # below, and keeps the two apart. Only the exit mirrors and hard account
    # constraints veto (scan_verdict.VETOES); everything the old four-level filter
    # blocked on that did not mirror an exit rule now RANKS and cannot block.
    #
    # Level 5 (account) and staleness are layered by the caller — /api/scan/ready
    # has the account context and the freshness read; a ~500-name sweep has
    # neither. The executor enforces L5 independently at the ticket, so this row
    # is advisory in exactly the way it was before.
    blocks = list((gate or {}).get("blocks") or [])
    composed = scan_verdict.compose(blocks)
    row["verdict"] = composed["verdict"]                 # ELIGIBLE | BLOCKED
    row["blocks"] = composed["blocks"]
    row["blocked_by"] = composed["blocked_by"]
    row["verdict_reasons"] = list(composed["blocked_by"])
    # The one surviving forward trigger: earnings clears on a DETERMINISTIC date.
    row["earnings_trigger"] = scan_triggers.earnings_trigger(blocks)
    # The entry ROUTE — advisory output only, never an order. Reads the same
    # extension the ranker consumes, so route and rank cannot disagree.
    row["route"] = (gate or {}).get("route") or scan_verdict.route(
        extension_atr=row.get("atr_extension"), regime_color=regime_color,
        ma21=None)

    # ---- Per-gate evaluation telemetry (READ-ONLY OBSERVABILITY) -------------
    # TRAVIS_EXTENSION. A purely ADDITIVE row key. `gate_results` is one
    # structured entry per gate — id, authority, pass, value, threshold — built
    # by a pure READ of the gate levels already computed above plus this row's
    # shadow income floor. It is what makes the SOLE-BLOCKER RATE computable (see
    # gate_telemetry): Levels 1-4 are evaluated unconditionally by
    # `screening.entry_gate`, so every gate carries a real pass/fail even for a
    # candidate that failed at Level 1.
    #
    # ZERO AUTHORITY. Nothing here is appended to `blocks`, so `row["verdict"]` —
    # already finalized above — is bit-for-bit what it was before this block
    # existed, and `bench` / `triggers` / `path_to_ready` with it. It re-runs no
    # gate, fetches nothing, and reads no clock. A failure to build it leaves the
    # key absent (the recorder skips such rows) rather than sinking the row:
    # telemetry is a side channel and must never perturb a scan.
    try:
        import gate_telemetry
        row["gate_results"] = gate_telemetry.build_results(gate, row)
    except Exception:  # noqa: BLE001 — a telemetry readout never sinks a row
        row["gate_results"] = None

    # Two-speed RS SHADOW — vs SPY only. Level reuses the displayed RS3M; slope is
    # the RS-line-EMA direction. SHADOW ONLY: never feeds the composed verdict,
    # never blocks, never sizes.
    #
    # The vs-SECTOR variant was removed 2026-08-21 along with everything else
    # keyed off a cap-weighted sector benchmark
    # (docs/decision-2026-08-21-remove-sector-rs.md). It took three things with
    # it: the scan table's `RS` column, its contribution to the shadow SCORE, and
    # the TURNING WATCH annotation that could append a reason string to a
    # non-READY row. The vs-SPY read below is what remains.
    rs_spy = rss.rs_state(df, spy_df) if spy_df is not None else {"state": None, "level": None, "slope": None}
    row["rs_state_spy"] = rs_spy["state"]
    row["rs_spy_level"] = rs_spy["level"]
    row["rs_spy_slope"] = rs_spy["slope"]

    # ---- Level-4 CHART STRUCTURE (SHADOW) + the volume phase flag -------------
    # TRAVIS_EXTENSION. Purely ADDITIVE row keys. Level 4 measures quietness but
    # not structure, so these four metrics measure the structure it misses:
    # distance below the trailing high, MA21 slope in ATR/bar, coil tightness, and
    # successive higher lows — plus `structure_score`, the count in their
    # constructive bands.
    #
    # ZERO AUTHORITY (chart_structure's module docstring states the full contract):
    # nothing here is appended to `blocks`, so `row["verdict"]` — already finalized
    # ~100 lines above — is bit-for-bit what it was before this block existed, and
    # `bench` / `triggers` / `path_to_ready` with it. Graduating a metric to
    # blocking authority is a deliberate future code change contingent on the
    # calibration these logged values exist to feed.
    #
    # Attached BEFORE the Level-4 short-circuit below, deliberately: "gate failed,
    # structure N/4" is half the comparison the calibration is FOR, and computing
    # it only for gate-passing rows would throw that half away. The metrics are
    # pure reads over the frame already in hand — no extra fetch.
    import chart_structure
    struct = chart_structure.structure_metrics(df)
    row["structure"] = struct
    row["structure_score"] = struct["structure_score"]
    row["structure_score_of"] = struct["structure_score_of"]

    # The consolidation phase flag the volume check reads — a pure READ of the
    # Level-4 right-spot checks already computed for this row's gate, never a
    # re-derivation. `gate is None` (the many score_ticker callers that pass no
    # gate) yields False, i.e. exactly today's volume behavior.
    row["consolidation_phase"] = chart_structure.consolidation_phase(
        ((gate or {}).get("ranking") or {}).get("right_spot"))

    # `suitability` = the CFM-suitability lens (stock-level gate short-circuit, else
    # the GO/CAUTION/AVOID metric rules). Not the headline verdict — a demoted signal.
    # Placed HERE, after every ranking input is on the row. The ranker consumes
    # chart structure and juice capacity, both attached above — computing the rank
    # before them would silently score every name against a missing input (the
    # neutral 0.5), which reads as "measured and average" rather than "not
    # measured yet". Ordering is load-bearing, not incidental.
    # ---- THE RANK. Carries ranking authority; never veto authority. ----------
    # Every input the old Levels 2/3/3.5/4 vetoed on arrives here as a VALUE, plus
    # the shadow features. `scan_score` normalizes each to 0..1 before weighting
    # (raw units are not comparable across names) and returns per-input
    # contributions, so a rank is explainable without re-running anything.
    #
    # Nothing below is ever appended to `blocks` above — a rank orders the
    # eligible, it cannot make a name ineligible. A row scoring 0.0 is ELIGIBLE
    # with a rank of 0.0, never BLOCKED. That invariant is the whole safety
    # property of this layer and is asserted directly by test_scan_score /
    # test_juice_capacity.
    sector_rs1m = (indicators.rs1m(sector_df, spy_df)
                   if (sector_df is not None and spy_df is not None) else None)
    rk = (gate or {}).get("ranking") or {}
    scored = scan_score.compute_score(
        inst_flow=cls["inst_flow"], base_stage=cls["base_stage"],
        base_count=cls["signals"].get("base_count"),
        entrability=entrability,
        extension_atr=row.get("atr_extension"),
        stock_greens=row.get("stock_greens"),
        rs3m_vs_spy=row.get("rs3m_vs_spy"),
        sector_rs1m=sector_rs1m,
        sector_breadth=rk.get("sector_breadth"),
        sector_atr_expanding=rk.get("sector_atr_expanding"),
        atr_momentum=row.get("atr_momentum"),
        structure_score=row.get("structure_score"),
        structure_score_of=row.get("structure_score_of"),
        capacity_pct=(row.get("juice_capacity") or {}).get("capacity_pct"),
        shadow_floor_pct=(row.get("shadow_floor") or {}).get("floor_pct"),
        net_juice_weekly_pct=row.get("net_juice_weekly_pct"))
    row["score"] = scored["score"]
    row["score_quality"] = scored["score_quality"]
    row["score_parts"] = scored["parts"]
    row["score_contributions"] = scored["contributions"]
    row["sector_rs1m"] = None if sector_rs1m is None else round(sector_rs1m, 2)

    # `suitability` is the OLDER GO/CAUTION/AVOID CFM-suitability lens, retained
    # deliberately: it is a different measurement from the scan verdict (stock
    # momentum suitability, not entry eligibility) and it is what the internal
    # queue / recommendation / refresh pipeline reads. Rewiring those is out of
    # scope for this change. A BLOCKED row short-circuits it, exactly as a failed
    # gate level used to.
    if row["verdict"] == scan_verdict.BLOCKED:
        row["suitability"] = "AVOID"
        row["suitability_reasons"] = [f"blocked: {b}" for b in row["blocked_by"]]
        row["suitability_notes"] = []
        return row

    # Judge the rounded values the UI actually shows, so the suitability can never
    # disagree with the number displayed next to it (sub-rounding boundaries).
    suitability = compute_verdict(row)
    row["suitability"] = suitability["verdict"]
    row["suitability_reasons"] = suitability["reasons"]
    # Non-blocking observations (today: the phase-suppressed volume note), so a
    # suppressed rule is still visible rather than silently absent.
    row["suitability_notes"] = suitability.get("notes") or []
    return row


def affordability(state: dict) -> dict:
    """The account's current lot-affordability bar (schema v21).

    A shares-primary entry buys a whole 100-share lot, so a name is only a real
    candidate if one lot fits the dry powder available RIGHT NOW. This reads the
    bar from ``position_manager.capital_summary`` — the same operating cash,
    defensive reserve and capital-cap figures Level 5 gates on — so the scan and
    the Execute gate can never disagree about what is affordable."""
    import position_manager
    cap = position_manager.capital_summary(state)
    return {
        "max_lot_cost": cap.get("max_lot_cost"),
        "deployable": cap.get("deployable"),
        "operating_cash": cap.get("operating_cash"),
        "operating_cash_source": cap.get("operating_cash_source"),
        "cash_above_reserve": cap.get("cash_above_reserve"),
        "capital_headroom": cap.get("capital_headroom"),
        "per_position_cap": cap.get("per_position_cap"),
        "slots_open": cap.get("slots_open"),
        "shares_per_lot": cap.get("shares_per_lot"),
        # Which ceiling is binding, so the UI explains the number instead of just
        # showing it. "unknown" means operating cash was never configured, and the
        # filter is therefore INACTIVE — not that nothing is affordable.
        "binding": (
            "unknown" if cap.get("max_lot_cost") is None
            else "per_position_cap" if cap.get("per_position_cap", 0) <= cap.get("deployable", 0)
            else "capital_cap" if cap.get("capital_headroom", 0) <= cap.get("cash_above_reserve", 0)
            else "cash_above_reserve"),
        "active": cap.get("max_lot_cost") is not None,
    }


def affordable(row: dict, max_lot_cost: float | None) -> bool | None:
    """Does one 100-share lot of this row fit the bar? None when the lot cost
    can't be priced — an UNKNOWN is never treated as unaffordable, because hiding a
    name we simply failed to price would be a silent, invisible exclusion."""
    if max_lot_cost is None:
        return None
    lot_cost = row.get("lot_cost")
    if lot_cost is None:
        return None
    return float(lot_cost) <= float(max_lot_cost)


def split_by_affordability(rows: list[dict], state: dict) -> tuple[list[dict], list[dict], dict]:
    """``(affordable_rows, priced_out_rows, affordability)``.

    Rows are ANNOTATED in place with ``affordable`` / ``lot_cost_over_by`` so a
    priced-out name can explain itself wherever it is shown. An unpriceable lot
    cost stays in the affordable list (see ``affordable``) — this filter removes
    only names we can positively say are too expensive."""
    bar = affordability(state)
    max_lot_cost = bar.get("max_lot_cost")
    keep, priced_out = [], []
    for row in rows:
        ok = affordable(row, max_lot_cost)
        row["affordable"] = ok
        row["max_lot_cost"] = max_lot_cost
        row["lot_cost_over_by"] = (
            round(float(row["lot_cost"]) - float(max_lot_cost), 2)
            if ok is False else None)
        (priced_out if ok is False else keep).append(row)
    bar["priced_out"] = len(priced_out)
    bar["shown"] = len(keep)
    return keep, priced_out, bar


def _current_regime_color() -> str | None:
    """The invisible market regime for a sweep. Read ONCE per sweep (and once
    more to key the day cache — it is itself memoized, so that is not a second
    compute). Best-effort: a regime failure degrades to None, and the composed
    verdict then never emits READY."""
    import screening
    try:
        return screening.regime().get("status")
    except Exception:  # noqa: BLE001
        return None


def _compute_scorecard(names: list[str], price_overrides: dict | None = None,
                       regime_color: str | None = None) -> dict:
    import logging_handler as log
    import screening  # local imports avoid any import-time cycle
    import weeklies

    price_overrides = price_overrides or {}

    # Resolve each ticker's sector once; collect the sector ETFs we'll need.
    sector_of = {t: (sector_data.sector_for(t) or "") for t in names}
    etfs = sorted({e for e in sector_of.values() if e})

    data_handler.prefetch(config.scan_base_frames() + etfs + names)
    weeklies.prefetch(names)  # warm the weeklies cache in parallel (no-op if disabled)
    spy = data_handler.get_daily(config.BENCHMARK)
    sector_frames = {e: data_handler.get_daily(e) for e in etfs}

    # The regime feeds the composed scan verdict (a RED regime blocks every row).
    # Resolved by the caller on the cached path so it can key the day cache; read
    # here for a direct/subset call.
    if regime_color is None:
        regime_color = _current_regime_color()

    # State is loaded ONCE for the whole sweep (schema v21) and threaded into the
    # profile resolution, which uses it for BOTH the assignment lookup and the
    # dividend override check. Resolving per ticker would mean a state.json read per
    # name — ~500 of them on a full sweep, on the request path.
    try:
        sweep_state = log.load_state()
    except Exception:  # noqa: BLE001 — no state just means the heuristic decides
        sweep_state = None

    rows = []
    for t in names:
        etf = sector_of[t]
        # Resolved ONCE per ticker and threaded into both the gate and the row —
        # re-resolving inside score_ticker cost a second cache read per name.
        try:
            profile, annual_div_pct = screening.resolve_profile_detail(
                t, state=sweep_state)
        except Exception:  # noqa: BLE001 — an unresolvable profile is the CFM default
            profile, annual_div_pct = None, None
        try:
            gate = screening.entry_gate(t, profile=profile) if etf else None
        except Exception:  # noqa: BLE001 — a gate failure must never sink the row
            gate = None
        rows.append(score_ticker(t, spy, etf, sector_frames.get(etf), gate,
                                 has_weeklies=weeklies.has_weeklies(t),
                                 price_override=price_overrides.get(t.upper()),
                                 regime_color=regime_color,
                                 profile=profile,
                                 annual_dividend_yield_pct=annual_div_pct))

    rows.sort(key=lambda r: (r["sector"], r["ticker"]))
    return {"as_of": log.utcnow(), "results": rows}


def scorecard_warm(price_overrides: dict | None = None) -> dict | None:
    """The full-universe sweep IF it can be served WITHOUT computing one, else None.

    A strictly READ-ONLY peek. It never calls ``_compute_scorecard``, never takes
    the ``scorecard:full`` memo lock, and so can never block behind an in-flight
    background sweep — which is the whole point of it existing.

    ``scorecard()`` is not a reader: on a cold memo it IS the sweep, and it holds
    a per-key lock for the duration (``screening._cached``). The background scan
    kicked by /api/scan/refresh holds that exact lock, so any request that called
    ``scorecard()`` during a sweep waited for the entire sweep and the client
    aborted at its 60s timeout. Read paths call this instead and report "not warm"
    rather than hanging.

    Two sources, in order — the in-process memo, then the day's disk cache (warm
    across a process restart, which the memo is not). An incomplete disk hit is
    NOT served: a partial universe silently rendered as the whole one is exactly
    the kind of quiet wrongness this dashboard must never show.

    A price-override request always needs fresh numbers, so it is never served
    from a peek."""
    if price_overrides:
        return None
    import screening
    hit = screening.peek_cached("scorecard:full")
    if hit is not None:
        return hit
    try:
        import scan_cache
        names = sector_data.all_tickers()
        reuse = scan_cache.reusable(names, _current_regime_color())
    except Exception:  # noqa: BLE001 — a peek must never raise into a read path
        return None
    if reuse is not None and reuse.get("complete") and reuse["result"].get("results"):
        return reuse["result"]
    return None


def scorecard(tickers: list[str] | None = None, price_overrides: dict | None = None,
              force: bool = False) -> dict:
    """Build the scorecard for a list of tickers (default: every holding across
    every sector). Warms the cache for SPY + sector ETFs + the tickers in one
    parallel batch, then computes a row each — reusing the existing 4-level entry
    gate, where only a stock-level (Level 3/4) failure short-circuits the verdict
    (a yellow regime does not blanket the table). Rows are grouped-friendly (each
    carries its sector) and sorted by sector then ticker.

    The full-universe sweep (tickers=None) is expensive (indicator math across
    every holding) and purely market-driven — it doesn't depend on the operator's
    own account state — so it is cached at two levels:

      * ``scan_cache`` (disk, one sweep per DATA EPOCH — see that module). This is
        what makes the universe scan run ~twice a day instead of dozens of times,
        and what lets a restarted machine serve the last sweep instead of
        recomputing it on the request path.
      * screening's short-TTL memo in front of it, so the Scan tab mounting both
        the Scorecard and Ready-to-Enter panels doesn't even re-read/parse the
        cache file twice on one page load.

    ``force=True`` bypasses both — the operator's Rescan button, which must always
    be able to get fresh numbers on demand.

    An explicit ticker subset (e.g. one ticker's entry snapshot at trade time)
    always computes fresh."""
    if tickers:
        names = [t.strip().upper() for t in tickers if t.strip()]
        return _compute_scorecard(names, price_overrides=price_overrides)

    import logging_handler as log
    import scan_cache
    import screening  # local import avoids any import-time cycle
    names = sector_data.all_tickers()

    def _sweep():
        """The cached full-universe sweep, plus today's capacity observation.

        The capacity append wraps EVERY return path — including a cache HIT — so
        the series accrues once per calendar day regardless of which branch served
        the request. That is safe precisely because the store is idempotent per
        symbol per DAY (juice_capacity.record_observations): a hundred cache hits
        write the same day's point a hundred times with identical values rather
        than appending a hundred observations, which is what keeps a heavily-
        rescanned day from outvoting a quiet one in the median.

        Best-effort and OUTSIDE the returned result: a telemetry failure can
        neither sink the sweep nor alter a single row.
        """
        out = _sweep_uncounted()
        try:
            import juice_capacity
            juice_capacity.record_observations(out.get("results") or [])
        except Exception:  # noqa: BLE001 — telemetry never sinks the sweep
            pass
        return out

    def _sweep_uncounted():
        # Resolved before the cache read so the regime is part of the key: a
        # regime flip must re-scan rather than serve verdicts composed against
        # the old tape.
        regime_color = _current_regime_color()
        reuse = None if force else scan_cache.reusable(names, regime_color)
        if reuse is not None and reuse["complete"]:
            return reuse["result"]

        # INCREMENTAL. The day's sweep is still valid for every name it already
        # covers, so a universe edit re-scans only what actually changed: added
        # names are computed and merged in, removed names' rows were already
        # dropped by `reusable`. Before this, adding ONE ticker invalidated all
        # ~500 rows and forced a full cold sweep onto the request path — where the
        # client's 60s abort was waiting — because the universe was in the cache
        # key. The cost was proportional to the universe instead of to the edit.
        if reuse is not None and reuse["result"].get("results"):
            missing = reuse["missing"]
            merged = dict(reuse["result"])
            if missing:
                added = _compute_scorecard(missing, regime_color=regime_color)
                # New rows are stamped like a per-stock refresh, so the mixed
                # vintage stays visible rather than implied (same discipline as
                # scan_cache.patch_rows).
                stamp = log.utcnow()
                rows = list(merged["results"]) + [dict(r, refreshed_at=stamp)
                                                  for r in added["results"]]
                merged["results"] = sorted(rows, key=lambda r: (r["sector"], r["ticker"]))
                merged["partial_rescan"] = len(added["results"])
                scan_cache.store(regime_color, merged)
            return merged

        fresh = _compute_scorecard(names, regime_color=regime_color)
        scan_cache.store(regime_color, fresh)
        return dict(fresh, cached=False, scan_day=scan_cache.scan_day())

    if force:
        out = _sweep()
        screening.prime_cache("scorecard:full", out)  # keep the memo consistent
        return out
    return screening._cached("scorecard:full", _sweep)
