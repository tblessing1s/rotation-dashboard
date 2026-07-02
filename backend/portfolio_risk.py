"""Portfolio-level risk aggregation — "what is my book actually exposed to."

Greeks are computed per leg with the same Black-Scholes machinery the rest of
the app uses (vol implied from each leg's stored mark, so it works offline and
in demo mode), then aggregated:

- delta: share-equivalents and dollars, raw and SPY-beta-adjusted (each
  ticker's beta regressed from the cached daily history, so a high-beta book
  shows its true index exposure).
- theta/day: dollars the book collects (short legs) minus pays (long legs)
  per calendar day.
- vega: dollars per 1 vol point.
- capital deployed vs the cap, the 2xATR defensive reserve status, and the
  sector exposure breakdown (the entry filters funnel into the hottest
  sector — this is where that concentration becomes visible).
"""
from __future__ import annotations

import math

import config
import data_handler
import indicators


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _call_greeks_full(S, K, T, r, sigma, q=0.0):
    """(delta, theta_per_day, vega_per_volpt) for one call, per share."""
    if not (S and S > 0 and K and K > 0 and T and T > 0 and sigma and sigma > 0):
        return None, None, None
    d1 = indicators._d1(S, K, T, r, sigma, q)
    d2 = d1 - sigma * math.sqrt(T)
    delta = math.exp(-q * T) * indicators._norm_cdf(d1)
    theta_year = (-S * math.exp(-q * T) * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
                  - r * K * math.exp(-r * T) * indicators._norm_cdf(d2)
                  + q * S * math.exp(-q * T) * indicators._norm_cdf(d1))
    vega = S * math.exp(-q * T) * _norm_pdf(d1) * math.sqrt(T) / 100.0  # per vol point
    return delta, theta_year / 365.0, vega


def _leg_greeks(S, strike, dte, mark_per_share):
    """Greeks per share for a call leg, implying vol from its stored mark."""
    T = (dte or 0) / 365.0
    if not (S and strike and T > 0):
        return None, None, None
    sigma = indicators.implied_vol_call(mark_per_share, S, strike, T, config.RISK_FREE_RATE)
    if not sigma:
        return None, None, None
    return _call_greeks_full(S, strike, T, config.RISK_FREE_RATE, sigma)


def beta(df, spy_df, lookback: int = 250) -> float | None:
    """OLS beta of a ticker's daily returns vs SPY over `lookback` sessions."""
    if df is None or spy_df is None:
        return None
    r = df["Close"].astype(float).pct_change().dropna().tail(lookback)
    m = spy_df["Close"].astype(float).pct_change().dropna().tail(lookback)
    joined = r.to_frame("r").join(m.to_frame("m"), how="inner").dropna()
    if len(joined) < 60:
        return None
    var = joined["m"].var()
    if not var:
        return None
    return float(joined["r"].cov(joined["m"]) / var)


def position_risk(p: dict, spy_df) -> dict | None:
    ticker = p.get("ticker", "")
    df = data_handler.get_daily(ticker)
    price = indicators.last(df)
    if price is None:
        return None

    share_equiv = theta_day = vega_total = 0.0
    greeks_complete = True

    leap = p.get("leap") or {}
    contracts = int(leap.get("contracts") or 0)
    if leap and contracts:
        mark = (float(leap["current_bid"]) / (contracts * 100)
                if leap.get("current_bid") is not None else None)
        d, th, v = _leg_greeks(price, leap.get("strike"), leap.get("dte"), mark)
        if d is None:
            greeks_complete = False
        else:
            share_equiv += d * contracts * 100
            theta_day += th * contracts * 100
            vega_total += v * contracts * 100

    for sc in p.get("short_calls", []):
        n = int(sc.get("contracts") or 0)
        d, th, v = _leg_greeks(price, sc.get("strike"), sc.get("dte"), sc.get("current_bid"))
        if d is None:
            greeks_complete = False
            continue
        share_equiv -= d * n * 100          # short the call
        theta_day -= th * n * 100           # short theta is collected (th < 0)
        vega_total -= v * n * 100

    share_equiv += int((p.get("shares") or {}).get("count") or 0)

    b = beta(df, spy_df)
    dollar_delta = share_equiv * price
    return {
        "ticker": ticker,
        "sector": p.get("sector") or "",
        "price": round(price, 2),
        "beta": round(b, 2) if b is not None else None,
        "delta_shares": round(share_equiv, 1),
        "delta_dollars": round(dollar_delta, 2),
        "delta_dollars_spy_adj": round(dollar_delta * b, 2) if b is not None else None,
        "theta_per_day": round(theta_day, 2),
        "vega": round(vega_total, 2),
        "capital": round(float(leap.get("cost_basis") or 0), 2),
        "greeks_complete": greeks_complete,
    }


def portfolio_view(state: dict) -> dict:
    spy_df = data_handler.get_daily(config.BENCHMARK)
    rows = []
    for p in state.get("positions", []):
        if p.get("status") == "closed":
            continue
        try:
            row = position_risk(p, spy_df)
        except Exception:  # noqa: BLE001 — one bad position must not blank the card
            row = None
        if row:
            rows.append(row)

    def _sum(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals), 2) if vals else None

    deployed = float(state.get("metadata", {}).get("capital_deployed") or 0)

    # Live Schwab balance when connected, else the stored manual fallback
    # (same resolver the Level 5 gate and the Capital card use).
    import account_gate
    cash_info = account_gate.resolve_operating_cash(state)
    operating = cash_info["amount"]

    # The 2xATR defensive reserve across the book (same formula as the gate).
    reserves = [account_gate._position_reserve(p) for p in state.get("positions", [])
                if p.get("status") != "closed"]
    reserve_required = round(sum(r for r in reserves if r is not None), 2)

    sectors: dict[str, float] = {}
    for r in rows:
        sectors[r["sector"] or "?"] = round(sectors.get(r["sector"] or "?", 0.0) + r["capital"], 2)
    total_cap = sum(sectors.values()) or 1.0

    return {
        "positions": rows,
        "totals": {
            "delta_dollars": _sum("delta_dollars"),
            "delta_dollars_spy_adj": _sum("delta_dollars_spy_adj"),
            "theta_per_day": _sum("theta_per_day"),
            "vega": _sum("vega"),
            "greeks_complete": all(r["greeks_complete"] for r in rows) if rows else True,
        },
        "capital": {
            "deployed": deployed,
            "cap": config.MAX_DEPLOYED_CAPITAL,
            "pct_of_cap": round(deployed / config.MAX_DEPLOYED_CAPITAL * 100, 1)
                          if config.MAX_DEPLOYED_CAPITAL else None,
            "operating_cash": operating,
            "operating_cash_source": cash_info["source"],
            "reserve_required": reserve_required,
            "reserve_ok": operating >= reserve_required,
        },
        "sector_exposure": [
            {"sector": s, "capital": c, "pct": round(c / total_cap * 100, 1)}
            for s, c in sorted(sectors.items(), key=lambda kv: -kv[1])
        ],
    }
