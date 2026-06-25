"""Model-free intrinsic / extrinsic decomposition for an option fill.

The whole point of executing an option *from* the dashboard is that we can
capture two facts at the same instant: the premium actually paid (the option's
fill price per share) and the underlying's price right then (a Schwab quote).
With those, the split is pure arithmetic — no Black-Scholes, no implied vol, no
provider model:

    intrinsic = the cash an exercise would realize *now* (never negative)
    extrinsic = everything left in the premium (time value, vol, spread)

Tracking how `extrinsic` erodes over the hold is the theta ledger; this module
is just the deterministic decomposition at any (premium, stock_price) pair.
"""
from __future__ import annotations

CALL = "call"
PUT = "put"


def normalize_type(option_type: str) -> str:
    """Map any reasonable spelling to the canonical 'call' / 'put'."""
    t = str(option_type or "").strip().lower()
    if t in ("c", "call", "calls"):
        return CALL
    if t in ("p", "put", "puts"):
        return PUT
    raise ValueError(f"option_type must be call or put, got {option_type!r}")


def intrinsic_value(option_type: str, strike: float, stock_price: float) -> float:
    """Per-share intrinsic value — the in-the-money amount, floored at zero.

    Call: max(0, stock - strike). Put: max(0, strike - stock).
    """
    t = normalize_type(option_type)
    strike = float(strike)
    stock_price = float(stock_price)
    if t == CALL:
        return max(0.0, stock_price - strike)
    return max(0.0, strike - stock_price)


def decompose(option_type: str, strike: float, stock_price: float, premium: float) -> dict:
    """Split a per-share premium into intrinsic + extrinsic at a stock price.

    `premium` is the option's fill price per share (so a $2.50 contract is 2.50,
    i.e. $250 of premium per 100-share contract). Returns per-share figures plus
    the moneyness so the ledger can label the row without recomputing.

    Extrinsic can come back slightly negative if a deep-ITM option filled below
    parity (a real, if rare, microstructure event); we surface it as-is rather
    than clamping, so the number stays an honest reflection of the fill.
    """
    t = normalize_type(option_type)
    strike = float(strike)
    stock_price = float(stock_price)
    premium = float(premium)
    intrinsic = intrinsic_value(t, strike, stock_price)
    extrinsic = premium - intrinsic
    if intrinsic > 0:
        moneyness = "ITM"
    elif (t == CALL and stock_price >= strike) or (t == PUT and stock_price <= strike):
        # Exactly at the strike (intrinsic rounds to zero) reads as ATM.
        moneyness = "ATM"
    else:
        moneyness = "OTM"
    extrinsic_pct = round(extrinsic / premium * 100, 2) if premium else None
    return {
        "type": t,
        "strike": round(strike, 4),
        "stockPrice": round(stock_price, 4),
        "premium": round(premium, 4),
        "intrinsic": round(intrinsic, 4),
        "extrinsic": round(extrinsic, 4),
        "extrinsicPct": extrinsic_pct,
        "moneyness": moneyness,
    }
