"""Position-derived math: LEAP intrinsic/extrinsic, share-cap progress, and the
portfolio-level capital + milestone summary. Pure functions over a state dict.
"""
from __future__ import annotations

import config
import data_handler
import earnings


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


def enrich_short(sc: dict, stock_price: float | None, dividend: dict | None) -> dict:
    """Per-short management signals, all derived from stored execution data:

    - decay_pct + roll_now: the 75% buyback rule (HARD_CFM_RULE — when the short
      has surrendered >=75% of its sale premium with >2 DTE, roll early).
    - below_strike: the DEFEND trigger (stock closed under the short strike).
    - assignment_risk: extrinsic below the coming dividend before ex-div. The
      short is covered by a LEAP, NOT stock — assignment creates SHORT STOCK
      that owes the dividend, so the standard play is to roll before ex-div.
    """
    out = dict(sc)
    contracts = int(sc.get("contracts") or 0)
    sold = (float(sc["entry_premium_total"]) / (contracts * 100)
            if sc.get("entry_premium_total") and contracts else None)
    current = sc.get("current_bid")
    out["sold_per_share"] = round(sold, 2) if sold else None
    decay = (1 - float(current) / sold) if (sold and current is not None) else None
    out["decay_pct"] = round(decay * 100, 1) if decay is not None else None
    dte = sc.get("dte")
    out["roll_now"] = bool(decay is not None and decay >= config.BUYBACK_DECAY_PCT
                           and dte is not None and dte > config.BUYBACK_MIN_DTE)
    strike = sc.get("strike")
    out["below_strike"] = bool(stock_price is not None and strike is not None
                               and stock_price < float(strike))

    out["assignment_risk"] = None
    ex_date, amount = (dividend or {}).get("ex_date"), (dividend or {}).get("amount")
    if ex_date and amount and current is not None and strike is not None and stock_price is not None:
        from datetime import date, datetime, timedelta
        try:
            ex = datetime.strptime(str(ex_date)[:10], "%Y-%m-%d").date()
            expiry = (datetime.strptime(str(sc["expiration"])[:10], "%Y-%m-%d").date()
                      if sc.get("expiration")
                      else date.today() + timedelta(days=int(dte)) if dte is not None else None)
            extrinsic = max(float(current) - max(stock_price - float(strike), 0.0), 0.0)
            if expiry and date.today() <= ex <= expiry and extrinsic < float(amount):
                out["assignment_risk"] = {
                    "extrinsic": round(extrinsic, 2), "dividend": float(amount),
                    "ex_date": ex_date,
                    "note": ("Extrinsic below the dividend before ex-div — early assignment "
                             "likely. The short is covered by a LEAP, not stock: assignment "
                             "creates SHORT STOCK that owes the dividend. Roll before ex-div "
                             "(or accept the assignment mechanics deliberately)."),
                }
        except (TypeError, ValueError):
            pass
    return out


def enrich_position(position: dict, roll_summary: dict | None = None) -> dict:
    out = dict(position)
    ticker = position.get("ticker", "")
    price = _stock_price(ticker)
    out["stock_price"] = price
    if position.get("leap"):
        out["leap"] = enrich_leap(position["leap"], price)
    dividend = position.get("dividend")
    out["short_calls"] = [enrich_short(sc, price, dividend)
                          for sc in position.get("short_calls", [])]
    out["defend"] = any(sc["below_strike"] for sc in out["short_calls"])
    out["roll_summary"] = roll_summary or {"count": 0, "net_total": 0.0, "drag_total": 0.0}
    shares = dict(position.get("shares") or {})
    count = int(shares.get("count") or 0)
    cap = int(shares.get("cap") or config.SHARE_CAP)
    shares["cap"] = cap
    shares["pct_to_cap"] = round(count / cap * 100, 1) if cap else 0
    shares["locked"] = count >= cap
    # Accumulation-vs-kill-switch guard (config flag; see can_add_shares).
    if config.BLOCK_ACCUMULATION_ON_RS_DETERIORATION:
        blocked, why = _accumulation_block(ticker)
        shares["accumulation_blocked"] = blocked
        shares["accumulation_block_reason"] = why
    out["shares"] = shares
    try:
        out["earnings"] = earnings.next_earnings(ticker)
    except Exception:  # noqa: BLE001 — earnings is informational, never block positions
        out["earnings"] = {"ticker": ticker, "date": None, "days_until": None,
                           "warning": False, "source": "error"}
    return out


def positions_view(state: dict) -> list[dict]:
    by_ticker = (state.get("roll_ledger") or {}).get("by_ticker", {})
    out = [enrich_position(p, by_ticker.get(p.get("ticker", "")))
           for p in state.get("positions", [])]
    # Wash-sale visibility on OPEN positions: the cycle derivation marks a
    # loss-closing cycle "flagged" when the underlying is re-entered inside the
    # window — carry that onto the currently open position for the same name.
    flagged: dict[str, dict] = {}
    for c in state.get("cycles", []):
        ws = c.get("wash_sale")
        if ws and ws.get("status") == "flagged":
            flagged[c["ticker"]] = {"loss_exit_date": c.get("exit_date"),
                                    "loss": ws.get("loss"),
                                    "note": "Re-entry within 30 days of a loss exit "
                                            "— wash-sale rules likely defer the loss."}
    for p in out:
        p["wash_sale_flag"] = (flagged.get(p.get("ticker", ""))
                               if p.get("status") != "closed" else None)
    return out


def capital_summary(state: dict) -> dict:
    meta = state.get("metadata", {})
    deployed = float(meta.get("capital_deployed") or 0)
    reserve = float(meta.get("reserve_required") or config.RESERVE_REQUIRED)
    # Live Schwab balance when connected (also persists back to state.metadata
    # so this stays the single source other readers agree on); manual entry
    # is the fallback in demo mode, when Schwab isn't connected, or on error.
    import account_gate
    cash_info = account_gate.resolve_operating_cash(state)
    operating = cash_info["amount"]
    ytd = float(state.get("theta_ledger", {}).get("totals", {}).get("ytd") or 0)
    monthly = float(state.get("theta_ledger", {}).get("totals", {}).get("this_month") or 0)
    return {
        "capital_deployed": deployed,
        "reserve_required": reserve,
        "operating_cash": operating,
        "operating_cash_source": cash_info["source"],
        "operating_cash_error": cash_info["error"],
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


def _accumulation_block(ticker: str) -> tuple[bool, str | None]:
    """Kill-switch / RS3M-deterioration guard for share accumulation. Returns
    (blocked, reason). Any non-green kill-switch read blocks: red is an exit in
    progress; yellow (CAUTION) means RS is thinning toward the kill line — the
    pullback-accumulation play must not add to a name the strategy is about to
    leave."""
    try:
        import kill_switch
        ev = kill_switch.evaluate(ticker)
    except Exception:  # noqa: BLE001 — no data, no verdict: don't block on error
        return False, None
    if ev.get("status") in ("red", "yellow"):
        return True, (f"kill-switch {ev['status'].upper()} — RS3M vs SPY "
                      f"{ev.get('rs3m_vs_spy')}, vs Sector {ev.get('rs3m_vs_sector')}")
    return False, None


def can_add_shares(state: dict, ticker: str) -> bool:
    """A position can accumulate more shares only until it hits the 500 cap —
    and, when BLOCK_ACCUMULATION_ON_RS_DETERIORATION is on, only while the
    kill switch reads green for the name."""
    from logging_handler import find_position
    if config.BLOCK_ACCUMULATION_ON_RS_DETERIORATION and _accumulation_block(ticker)[0]:
        return False
    p = find_position(state, ticker)
    if not p:
        return True
    shares = p.get("shares") or {}
    return int(shares.get("count") or 0) < int(shares.get("cap") or config.SHARE_CAP)
