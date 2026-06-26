"""Position-derived math: LEAP intrinsic/extrinsic, share-cap progress, and the
portfolio-level capital + milestone summary. Pure functions over a state dict.
"""
from __future__ import annotations

import config
import data_handler


def _stock_price(ticker: str) -> float | None:
    q = data_handler.latest_quote(ticker)
    return q["price"] if q else None


def enrich_leap(leap: dict, stock_price: float | None) -> dict:
    """Re-split a LEAP's current value into intrinsic/extrinsic.

    intrinsic = max(stock - strike, 0) * contracts * 100
    extrinsic = current option value - intrinsic
    Uses the stored current_bid (per-contract total) when present; otherwise
    leaves the stored values untouched.
    """
    out = dict(leap)
    strike = leap.get("strike")
    contracts = int(leap.get("contracts") or 0)
    if strike is not None and stock_price is not None and contracts:
        intrinsic = max(stock_price - strike, 0.0) * contracts * 100
        out["intrinsic"] = round(intrinsic, 2)
        current = leap.get("current_bid")
        if current is not None:
            out["extrinsic"] = round(float(current) - intrinsic, 2)
    return out


def enrich_position(position: dict) -> dict:
    out = dict(position)
    ticker = position.get("ticker", "")
    price = _stock_price(ticker)
    out["stock_price"] = price
    if position.get("leap"):
        out["leap"] = enrich_leap(position["leap"], price)
    shares = dict(position.get("shares") or {})
    count = int(shares.get("count") or 0)
    cap = int(shares.get("cap") or config.SHARE_CAP)
    shares["cap"] = cap
    shares["pct_to_cap"] = round(count / cap * 100, 1) if cap else 0
    shares["locked"] = count >= cap
    out["shares"] = shares
    return out


def positions_view(state: dict) -> list[dict]:
    return [enrich_position(p) for p in state.get("positions", [])]


def capital_summary(state: dict) -> dict:
    meta = state.get("metadata", {})
    deployed = float(meta.get("capital_deployed") or 0)
    reserve = float(meta.get("reserve_required") or config.RESERVE_REQUIRED)
    operating = float(meta.get("operating_cash") or 0)
    ytd = float(state.get("theta_ledger", {}).get("totals", {}).get("ytd") or 0)
    monthly = float(state.get("theta_ledger", {}).get("totals", {}).get("this_month") or 0)
    return {
        "capital_deployed": deployed,
        "reserve_required": reserve,
        "operating_cash": operating,
        "reserve_ok": operating >= reserve or reserve == 0,
        "milestones": {
            "half_nut": {
                "target": config.MILESTONE_HALF_NUT,
                "current": monthly,
                "pct": round(monthly / config.MILESTONE_HALF_NUT * 100, 1) if config.MILESTONE_HALF_NUT else 0,
            },
            "quit_safe": {
                "target": config.MILESTONE_QUIT_SAFE,
                "current": monthly,
                "pct": round(monthly / config.MILESTONE_QUIT_SAFE * 100, 1) if config.MILESTONE_QUIT_SAFE else 0,
            },
        },
        "juice_ytd": ytd,
    }


def can_add_shares(state: dict, ticker: str) -> bool:
    """A position can accumulate more shares only until it hits the 500 cap."""
    from logging_handler import find_position
    p = find_position(state, ticker)
    if not p:
        return True
    shares = p.get("shares") or {}
    return int(shares.get("count") or 0) < int(shares.get("cap") or config.SHARE_CAP)
