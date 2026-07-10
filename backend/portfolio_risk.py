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

import config
import data_handler
import indicators


# (delta, theta_per_day, vega_per_volpt) per share — the one BSM implementation
# lives in indicators.call_greeks_full; this delegates so the theta formula has
# a single source of truth (also used by the LEAP juice-vs-burn math).
_call_greeks_full = indicators.call_greeks_full


def _leg_greeks(S, strike, dte, mark_per_share, q: float = 0.0):
    """Greeks per share for a call leg, implying vol from its stored mark.

    q is the underlying's continuous dividend yield (decimal). It lowers a
    dividend payer's call delta (delta = e^(-qT)·N(d1)) — most on the long-dated
    LEAP — so the book's net delta / beta-adjusted leverage warning is computed
    on honest greeks rather than q=0. [R3(c)]"""
    T = (dte or 0) / 365.0
    if not (S and strike and T > 0):
        return None, None, None
    sigma = indicators.implied_vol_call(mark_per_share, S, strike, T, config.RISK_FREE_RATE, q)
    if not sigma:
        return None, None, None
    return _call_greeks_full(S, strike, T, config.RISK_FREE_RATE, sigma, q)


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

    # Dividend-adjusted greeks: q lowers call delta most on the long-dated LEAP,
    # so the book delta / beta-adjusted leverage warning must not run at q=0. [R3(c)]
    import dividends
    q, q_src = dividends.q_with_source(ticker)

    leap = p.get("leap") or {}
    contracts = int(leap.get("contracts") or 0)
    if leap and contracts:
        mark = (float(leap["current_bid"]) / (contracts * 100)
                if leap.get("current_bid") is not None else None)
        d, th, v = _leg_greeks(price, leap.get("strike"), leap.get("dte"), mark, q)
        if d is None:
            greeks_complete = False
        else:
            share_equiv += d * contracts * 100
            theta_day += th * contracts * 100
            vega_total += v * contracts * 100

    for sc in p.get("short_calls", []):
        n = int(sc.get("contracts") or 0)
        d, th, v = _leg_greeks(price, sc.get("strike"), sc.get("dte"), sc.get("current_bid"), q)
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
        "q": round(q, 4),
        "q_source": q_src,
    }


def _returns(df, lookback: int):
    return df["Close"].astype(float).pct_change().dropna().tail(lookback)


def correlation(df_a, df_b, lookback: int = config.CORRELATION_LOOKBACK) -> float | None:
    """Pearson correlation of two tickers' daily returns over `lookback`
    sessions, on their overlapping dates. None when either frame is missing or
    the overlap is too short / degenerate."""
    if df_a is None or df_b is None:
        return None
    a, b = _returns(df_a, lookback), _returns(df_b, lookback)
    joined = a.to_frame("a").join(b.to_frame("b"), how="inner").dropna()
    if len(joined) < 30 or joined["a"].std() == 0 or joined["b"].std() == 0:
        return None
    return float(joined["a"].corr(joined["b"]))


def concentration(state: dict, rows: list[dict] | None = None) -> dict:
    """Cross-position concentration the 1/sector rule can't see: pairwise trailing
    correlation of the open underlyings, plus the book's net SPY-beta-adjusted
    delta as a multiple of deployed capital. Warns when two names are too
    correlated to count as diversified, or when the beta-adjusted book delta says
    the 'spread' is really one directional bet. Only meaningful with ≥2 open
    positions. Pure (cached data only) — no cash/Schwab side effects, so the alert
    engine can call it directly."""
    open_pos = [p for p in state.get("positions", []) if p.get("status") != "closed"]
    tickers = [p.get("ticker", "") for p in open_pos]
    applicable = len(open_pos) >= 2

    dfs = {t: data_handler.get_daily(t) for t in tickers}
    pairs, max_corr = [], None
    if applicable:
        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                ta, tb = tickers[i], tickers[j]
                c = correlation(dfs.get(ta), dfs.get(tb))
                if c is None:
                    continue
                pairs.append({"a": ta, "b": tb, "correlation": round(c, 2),
                              "high": c >= config.CORRELATION_WARN_THRESHOLD})
                max_corr = c if max_corr is None else max(max_corr, c)

    if rows is None:
        spy_df = data_handler.get_daily(config.BENCHMARK)
        rows = []
        for p in open_pos:
            try:
                r = position_risk(p, spy_df)
            except Exception:  # noqa: BLE001 — one bad position must not blank the check
                r = None
            if r:
                rows.append(r)
    beta_adj = [r["delta_dollars_spy_adj"] for r in rows
                if r.get("delta_dollars_spy_adj") is not None]
    net_beta_adj = round(sum(beta_adj), 2) if beta_adj else None

    import position_manager
    deployed = position_manager.deployed_capital(state)
    leverage = (round(abs(net_beta_adj) / deployed, 2)
                if net_beta_adj is not None and deployed else None)

    warnings, high_pairs = [], [p for p in pairs if p["high"]]
    if applicable:
        for hp in high_pairs:
            warnings.append(
                f"{hp['a']} and {hp['b']} are {hp['correlation']:.2f} correlated "
                f"(≥ {config.CORRELATION_WARN_THRESHOLD:.2f}) — one shock hits both, "
                f"despite satisfying the 1-per-sector rule.")
        if leverage is not None and leverage >= config.BETA_ADJ_LEVERAGE_WARN:
            warnings.append(
                f"Beta-adjusted book delta ${net_beta_adj:,.0f} is {leverage:g}× deployed "
                f"capital (≥ {config.BETA_ADJ_LEVERAGE_WARN:g}×) — the book is effectively "
                f"one directional bet.")
    return {
        "applicable": applicable,
        "pairs": pairs,
        "high_correlation_pairs": high_pairs,
        "max_correlation": round(max_corr, 2) if max_corr is not None else None,
        "correlation_threshold": config.CORRELATION_WARN_THRESHOLD,
        "correlation_lookback": config.CORRELATION_LOOKBACK,
        "net_beta_adj_delta_dollars": net_beta_adj,
        "beta_adj_leverage": leverage,
        "beta_adj_leverage_threshold": config.BETA_ADJ_LEVERAGE_WARN,
        "warnings": warnings,
        "warn": bool(warnings),
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

    import position_manager
    deployed = position_manager.deployed_capital(state)

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
        # Cross-position correlation / beta-adjusted concentration the 1/sector
        # rule can't see. Reuses the rows already priced above.
        "concentration": concentration(state, rows=rows),
    }
