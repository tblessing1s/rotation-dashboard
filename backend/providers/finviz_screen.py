"""Finviz screener — scan the full US market by price, volume, and ATR%."""

import logging
import pandas as pd

log = logging.getLogger(__name__)

# Finviz exposes average daily volume only as a fixed set of dropdown options.
# For a *minimum* volume filter the only correct family is the "Over X" options;
# the "A to B" range options would exclude the most-liquid names (the opposite of
# what a floor means). These are the exact labels Finviz accepts — any other
# string makes finvizfinance raise "Invalid filter option". Finviz's highest
# floor is "Over 2M", so that is the effective cap.
#
# (threshold_in_shares, finviz_label) — ascending by threshold.
_AVG_VOL_OVER_OPTIONS = [
    (50_000,     "Over 50K"),
    (100_000,    "Over 100K"),
    (200_000,    "Over 200K"),
    (300_000,    "Over 300K"),
    (400_000,    "Over 400K"),
    (500_000,    "Over 500K"),
    (750_000,    "Over 750K"),
    (1_000_000,  "Over 1M"),
    (2_000_000,  "Over 2M"),
]


def vol_filter(vol_min_shares: float) -> str:
    """Map a minimum average-volume request (in shares) to a valid Finviz option.

    Picks the highest "Over X" floor that does not exceed the request, so the
    server-side pre-filter never excludes a name the caller wanted. Requests
    below Finviz's smallest floor (50K) apply no volume filter ("Any"); requests
    above its largest floor (2M) are capped at "Over 2M" — Finviz offers nothing
    stricter, so the remaining precision is left to the caller.
    """
    label = "Any"
    for threshold, opt_label in _AVG_VOL_OVER_OPTIONS:
        if vol_min_shares >= threshold:
            label = opt_label
        else:
            break
    return label


def run(price_min: float, price_max: float, vol_min_shares: float,
        atr_min: float, atr_max: float, limit: int = 50) -> dict:
    """Query Finviz for the top `limit` US stocks matching the given criteria.

    Pre-filters by average volume via Finviz's server-side dropdown, then
    computes ATR% = ATR(14)$ / Price * 100 on the returned data and applies
    the remaining price and ATR% bounds. Results are sorted by ATR% descending.

    Returns a dict with ``results`` (list of matches) and ``volFilterApplied``
    (the Finviz average-volume option actually used — useful because Finviz
    caps its floor at "Over 2M", so a larger request can't be enforced
    server-side).
    """
    try:
        from finvizfinance.screener.technical import Technical
    except ImportError:
        raise RuntimeError(
            "finvizfinance is not installed; add it to requirements.txt and redeploy."
        )

    applied = vol_filter(vol_min_shares)
    try:
        screener = Technical()
        if applied != "Any":
            screener.set_filter(filters_dict={"Average Volume": applied})
        df = screener.screener_view(verbose=0)
    except Exception as exc:
        log.error("Finviz screener request failed: %s", exc)
        raise RuntimeError(f"Finviz screener unavailable: {exc}") from exc

    if df is None or df.empty:
        return {"results": [], "volFilterApplied": applied}

    try:
        # Normalize column names — finvizfinance has used "Ticker" and "No." historically
        ticker_col = "Ticker" if "Ticker" in df.columns else df.columns[1]
        df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
        df["ATR"] = pd.to_numeric(df["ATR"], errors="coerce")
        df = df.dropna(subset=["Price", "ATR"])
        df = df[df["Price"] > 0]

        df["atrPct"] = (df["ATR"] / df["Price"] * 100).round(2)

        mask = (
            (df["Price"] >= price_min) &
            (df["Price"] <= price_max) &
            (df["atrPct"] >= atr_min) &
            (df["atrPct"] <= atr_max)
        )
        filtered = df[mask].sort_values("atrPct", ascending=False).head(limit)

        results = []
        for _, row in filtered.iterrows():
            results.append({
                "symbol": str(row[ticker_col]),
                "price": round(float(row["Price"]), 2),
                "atrPct": round(float(row["atrPct"]), 2),
                "sector": str(row.get("Sector", "")) if "Sector" in df.columns else "",
                "source": "finviz",
            })
    except Exception as exc:
        log.error("Finviz result processing failed: %s", exc)
        raise RuntimeError(f"Finviz result processing error: {exc}") from exc

    return {"results": results, "volFilterApplied": applied}
