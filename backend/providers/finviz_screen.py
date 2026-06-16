"""Finviz screener — scan the full US market by price, volume, and ATR%."""

import logging
import pandas as pd

log = logging.getLogger(__name__)

_AVG_VOL_BRACKETS = [
    (0,      0.05,  "Under 50K"),
    (0.05,   0.1,   "50K to 100K"),
    (0.1,    0.5,   "100K to 500K"),
    (0.5,    1,     "500K to 1M"),
    (1,      2,     "1M to 2M"),
    (2,      5,     "2M to 5M"),
    (5,      10,    "5M to 10M"),
    (10,     float("inf"), "Over 10M"),
]


def _vol_filter(vol_min_shares: float) -> str:
    vol_m = vol_min_shares / 1_000_000
    for lo, hi, label in _AVG_VOL_BRACKETS:
        if vol_m <= hi:
            return label
    return "Over 10M"


def run(price_min: float, price_max: float, vol_min_shares: float,
        atr_min: float, atr_max: float, limit: int = 50) -> list[dict]:
    """Query Finviz for the top `limit` US stocks matching the given criteria.

    Pre-filters by average volume via Finviz's server-side dropdown, then
    computes ATR% = ATR(14)$ / Price * 100 on the returned data and applies
    the remaining price and ATR% bounds. Results are sorted by ATR% descending.
    """
    try:
        from finvizfinance.screener.technical import Technical
    except ImportError:
        raise RuntimeError(
            "finvizfinance is not installed; add it to requirements.txt and redeploy."
        )

    screener = Technical()
    screener.set_filter(filters_dict={"Average Volume": _vol_filter(vol_min_shares)})

    try:
        df = screener.screener_view(verbose=0)
    except Exception as exc:
        log.error("Finviz screener request failed: %s", exc)
        raise RuntimeError(f"Finviz screener unavailable: {exc}") from exc

    if df is None or df.empty:
        return []

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
            "symbol": str(row["Ticker"]),
            "price": round(float(row["Price"]), 2),
            "atrPct": round(float(row["atrPct"]), 2),
            "sector": str(row.get("Sector", "")) if "Sector" in df.columns else "",
            "source": "finviz",
        })

    return results
