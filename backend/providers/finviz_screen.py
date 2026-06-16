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


# finvizfinance Custom-view column ids (see finvizfinance.constants
# .CUSTOM_SCREENER_COLUMNS): Ticker, Sector, Average Volume, Relative Volume,
# Price. The Technical view carries ATR but not average/relative volume; the
# Custom view carries average/relative volume but not ATR — so the two views are
# merged by ticker to get both.
_CUSTOM_COLUMNS = [1, 3, 63, 64, 65]


def _enrich_volume(applied: str):
    """Fetch per-ticker average and relative volume from Finviz's Custom view.

    Returns a DataFrame indexed by ticker with ``avgVol`` and ``rvol`` columns,
    or ``None`` if the request/parse fails (the caller then degrades to the
    Technical view alone). Kept best-effort on purpose: the screener's core
    output is price/ATR%, and a flaky enrichment call must never sink the run.
    """
    try:
        from finvizfinance.screener.custom import Custom

        screener = Custom()
        if applied != "Any":
            screener.set_filter(filters_dict={"Average Volume": applied})
        df = screener.screener_view(verbose=0, columns=list(_CUSTOM_COLUMNS))
        if df is None or df.empty:
            return None

        ticker_col = "Ticker" if "Ticker" in df.columns else df.columns[0]
        out = pd.DataFrame({
            "ticker": df[ticker_col].astype(str),
            "avgVol": pd.to_numeric(df.get("Avg Volume"), errors="coerce"),
            "rvol": pd.to_numeric(df.get("Rel Volume"), errors="coerce"),
        })
        if "Sector" in df.columns:
            out["sector"] = df["Sector"].astype(str)
        return out.dropna(subset=["avgVol"]).set_index("ticker")
    except Exception as exc:
        log.warning("Finviz volume enrichment unavailable, "
                    "falling back to Technical view only: %s", exc)
        return None


def run(price_min: float, price_max: float, vol_min_shares: float,
        atr_min: float, atr_max: float, limit: int = 50) -> dict:
    """Query Finviz for the top `limit` US stocks matching the given criteria.

    Pre-filters by average volume via Finviz's server-side dropdown, then
    computes ATR% = ATR(14)$ / Price * 100 on the returned data and applies
    the remaining price and ATR% bounds. When the average/relative-volume
    enrichment is available, the requested volume floor is enforced *exactly*
    (Finviz's server-side dropdown only floors at "Over 2M"). Results are sorted
    by ATR% descending.

    Returns a dict with:
      ``results``         list of matches (symbol, price, atrPct, avgVol, rvol, sector)
      ``volFilterApplied`` the Finviz average-volume option used server-side
      ``volPrecise``      True when the exact share floor was enforced client-side
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

    vol_df = _enrich_volume(applied)
    vol_precise = vol_df is not None

    if df is None or df.empty:
        return {"results": [], "volFilterApplied": applied, "volPrecise": vol_precise}

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
        filtered = df[mask].sort_values("atrPct", ascending=False)

        results = []
        for _, row in filtered.iterrows():
            symbol = str(row[ticker_col])
            avg_vol = rvol = None
            sector = str(row.get("Sector", "")) if "Sector" in df.columns else ""
            if vol_df is not None and symbol in vol_df.index:
                vrow = vol_df.loc[symbol]
                avg_vol = None if pd.isna(vrow["avgVol"]) else float(vrow["avgVol"])
                rvol = None if pd.isna(vrow["rvol"]) else round(float(vrow["rvol"]), 2)
                if not sector and "sector" in vol_df.columns:
                    sector = str(vrow["sector"])

            # Exact volume floor — only enforceable when enrichment succeeded.
            if vol_precise:
                if avg_vol is None or avg_vol < vol_min_shares:
                    continue

            results.append({
                "symbol": symbol,
                "price": round(float(row["Price"]), 2),
                "atrPct": round(float(row["atrPct"]), 2),
                "avgVol": None if avg_vol is None else int(avg_vol),
                "rvol": rvol,
                "sector": sector,
                "source": "finviz",
            })
            if len(results) >= limit:
                break
    except Exception as exc:
        log.error("Finviz result processing failed: %s", exc)
        raise RuntimeError(f"Finviz result processing error: {exc}") from exc

    return {"results": results, "volFilterApplied": applied, "volPrecise": vol_precise}
