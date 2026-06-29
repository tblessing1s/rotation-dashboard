"""Execute CFM actions (buy_leap / sell_short / close_short) and auto-log them.

Every execution captures the stock price + premium at the moment of execution
and appends an immutable record to state.json, from which the theta ledger and
extrinsic-payback meters are derived. Live order transmission to Schwab is
gated behind the CFM_LIVE_TRADING env flag; with it off (the default) the action
is captured and logged against live market prices but no order is sent — the
honest paper path. Position state updates identically either way.
"""
from __future__ import annotations

import os

import config
import data_handler
import indicators
import logging_handler as log
import schwab_api
import sector_data

VALID_ACTIONS = {"buy_leap", "sell_short", "close_short", "close_leap", "roll_short"}

# Schwab order instruction per single-leg CFM action (all legs are calls).
INSTRUCTION = {
    "buy_leap": "BUY_TO_OPEN",
    "sell_short": "SELL_TO_OPEN",
    "close_short": "BUY_TO_CLOSE",
    "close_leap": "SELL_TO_CLOSE",
}


def live_enabled() -> bool:
    return os.environ.get("CFM_LIVE_TRADING", "").strip() in ("1", "true", "yes")


def _capture_price(ticker: str, supplied: float | None) -> tuple[float | None, str]:
    if supplied is not None:
        return float(supplied), "supplied"
    q = data_handler.latest_quote(ticker)
    if q:
        return q["price"], q["source"]
    return None, "unavailable"


def _ensure_position(state: dict, ticker: str) -> dict:
    p = log.find_position(state, ticker)
    if p:
        return p
    p = {
        "ticker": ticker.upper(),
        "sector": sector_data.sector_for(ticker) or "",
        "entry_date": log.utcnow()[:10],
        "status": "active",
        "leap": None,
        "shares": {"count": 0, "cost_basis_per_share": None, "cap": config.SHARE_CAP, "pct_to_cap": 0},
        "short_calls": [],
        "kill_switch": {},
        "thesis": {"fundamentals": "", "intact": True},
    }
    state["positions"].append(p)
    return p


def execute(payload: dict) -> dict:
    action = (payload.get("action") or "").strip()
    ticker = (payload.get("ticker") or "").strip().upper()
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action '{action}' (expected one of {sorted(VALID_ACTIONS)})")
    if not ticker:
        raise ValueError("ticker is required")
    contracts = int(payload.get("contracts") or 0)
    strike = payload.get("strike")
    stock_price, price_source = _capture_price(ticker, payload.get("stock_price"))

    state = log.load_state()
    position = _ensure_position(state, ticker)
    log.save_state(state)  # persist the shell position before recording the fill

    mode = "live" if live_enabled() else "logged"

    if action == "roll_short":
        return _roll_short(payload, ticker, contracts, stock_price, mode, price_source)

    # Live single-leg orders go to the broker and resolve asynchronously (place ->
    # poll -> fill/cancel); they're committed to state only once they actually
    # fill. Everything else (paper, or live without Schwab configured) commits
    # immediately as the honest logged path.
    if mode == "live" and schwab_api.configured():
        return _place_live(payload, ticker, action, contracts, strike, stock_price, price_source)
    return _commit(payload, ticker, action, contracts, strike, stock_price, price_source, mode)


def _build_leg(payload, ticker, action, strike, contracts, stock_price):
    if action == "buy_leap":
        return _buy_leap(payload, ticker, strike, contracts, stock_price)
    if action == "sell_short":
        return _sell_short(payload, ticker, strike, contracts, stock_price)
    if action == "close_leap":
        return _close_leap(payload, ticker, strike, contracts, stock_price)
    return _close_short(payload, ticker, strike, contracts, stock_price)


def _commit(payload, ticker, action, contracts, strike, stock_price, price_source, mode):
    """Record one filled leg: append the immutable execution, apply the position
    mutation, and rebuild the derived ledgers. Shared by the paper path and the
    live fill-confirmation path."""
    execution, position_update = _build_leg(payload, ticker, action, strike, contracts, stock_price)
    execution["mode"] = mode
    execution["price_source"] = price_source
    stored = log.append_execution(execution)

    state = log.load_state()
    position = _ensure_position(state, ticker)
    position_update(position)
    log.recompute_derived(state)
    log.save_state(state)

    return {
        "success": True,
        "status": "filled",
        "execution_id": stored["id"],
        "timestamp": stored["date"],
        "mode": mode,
        "captured_price": stock_price,
        "execution": stored,
    }


def _limit_price(action, payload):
    """Per-share LIMIT price for the order leg. buy_leap/close_leap carry
    per-contract dollars (÷100); the short legs are already per-share."""
    if action == "buy_leap":
        return float(payload.get("execution_price") or 0) / 100.0
    if action == "close_leap":
        return float(payload.get("close_price") or 0) / 100.0
    if action == "sell_short":
        return float(payload.get("premium_per_share") or 0)
    return float(payload.get("close_price_per_share") or 0)  # close_short


def _place_live(payload, ticker, action, contracts, strike, stock_price, price_source):
    """Transmit a real single-leg LIMIT order and park it as pending. The fill is
    confirmed (and committed) later via order_status; cancel_order drops it."""
    client = data_handler.client()
    account_hash = client.primary_account_hash()

    option_symbol = payload.get("option_symbol")
    if not option_symbol:
        expiration = payload.get("expiration")
        if not expiration:
            raise ValueError(f"{action} live order needs option_symbol or expiration to build the contract")
        option_symbol = schwab_api.occ_option_symbol(ticker, expiration, strike, call=True)

    limit = _limit_price(action, payload)
    order = schwab_api.build_single_leg_order(INSTRUCTION[action], contracts, option_symbol, limit)
    placed = client.place_order(account_hash, order)
    order_id = placed.get("orderId")
    if not order_id:
        raise schwab_api.SchwabError("Schwab accepted the order but returned no order id")

    log.save_pending_order(order_id, {
        "payload": payload, "ticker": ticker, "action": action, "contracts": contracts,
        "strike": strike, "stock_price": stock_price, "price_source": price_source,
        "account_hash": account_hash, "option_symbol": option_symbol,
        "limit_price": limit, "placed_at": log.utcnow(),
    })
    return {
        "success": True,
        "status": "working",
        "order_id": str(order_id),
        "mode": "live",
        "option_symbol": option_symbol,
        "limit_price": limit,
    }


def _fill_price(order: dict):
    """Best-effort average fill price from a Schwab order's activity legs."""
    try:
        for act in order.get("orderActivityCollection", []) or []:
            for leg in act.get("executionLegs", []) or []:
                if leg.get("price") is not None:
                    return float(leg["price"])
    except (TypeError, ValueError):
        pass
    return None


def _commit_from_pending(rec: dict, fill_price):
    """Commit a pending order once filled, overlaying the actual fill price onto
    the right payload field so the logged execution reflects the real fill."""
    payload = dict(rec.get("payload") or {})
    action = rec["action"]
    if fill_price is not None:
        if action == "buy_leap":
            payload["execution_price"] = fill_price * 100
        elif action == "close_leap":
            payload["close_price"] = fill_price * 100
        elif action == "sell_short":
            payload["premium_per_share"] = fill_price
        else:  # close_short
            payload["close_price_per_share"] = fill_price
    return _commit(payload, rec["ticker"], action, int(rec["contracts"]),
                   rec["strike"], rec["stock_price"], rec.get("price_source", "schwab"), "live")


def order_status(order_id: str) -> dict:
    """Poll a live order. On FILLED, commit it as an execution and clear the
    pending entry; on CANCELED/REJECTED/EXPIRED, clear it; otherwise it's still
    working."""
    rec = log.get_pending_order(order_id)
    if not rec:
        # Already resolved (committed or cleared) — nothing left to confirm.
        return {"order_id": order_id, "status": "unknown"}
    order = data_handler.client().get_order(rec["account_hash"], order_id)
    raw = (order.get("status") or "").upper()
    if raw == "FILLED":
        result = _commit_from_pending(rec, _fill_price(order))
        log.pop_pending_order(order_id)
        return {"order_id": order_id, "status": "filled", "raw_status": raw, **result}
    if raw in ("CANCELED", "REJECTED", "EXPIRED"):
        log.pop_pending_order(order_id)
        return {"order_id": order_id, "status": "rejected" if raw == "REJECTED" else "canceled",
                "raw_status": raw}
    return {"order_id": order_id, "status": "working", "raw_status": raw}


def cancel_order(order_id: str) -> dict:
    """Cancel a working order at the broker and drop the pending entry."""
    rec = log.get_pending_order(order_id)
    if rec:
        try:
            data_handler.client().cancel_order(rec["account_hash"], order_id)
        finally:
            log.pop_pending_order(order_id)
    return {"order_id": order_id, "status": "canceled"}


def _buy_leap(payload, ticker, strike, contracts, stock_price):
    # execution_price is per-contract total dollars; execution_total is the trade.
    price_per_contract = float(payload.get("execution_price") or 0)
    total = float(payload.get("execution_total") or price_per_contract * contracts)
    intrinsic_per_contract = max((stock_price or 0) - (strike or 0), 0) * 100
    extrinsic_at_entry = float(payload.get("extrinsic_captured")
                               or max(price_per_contract - intrinsic_per_contract, 0) * contracts)
    execution = {
        "ticker": ticker, "action": "buy_leap", "strike": strike, "contracts": contracts,
        "execution_price": price_per_contract, "execution_total": total,
        "extrinsic_captured": round(extrinsic_at_entry, 2), "stock_price": stock_price,
    }

    def apply(position):
        position["leap"] = {
            "strike": strike, "contracts": contracts, "cost_basis": total,
            "current_bid": total, "intrinsic": round(intrinsic_per_contract * contracts, 2),
            "extrinsic": round(extrinsic_at_entry, 2),
            "entry_date": log.utcnow()[:10], "dte": payload.get("dte", config.LEAP_TARGET_DTE),
            "expiration": payload.get("expiration"),
            "extrinsic_at_entry": round(extrinsic_at_entry, 2), "extrinsic_collected_to_date": 0,
        }
        position["status"] = "active"
    return execution, apply


def _close_leap(payload, ticker, strike, contracts, stock_price):
    """Sell the deep-ITM LEAP to close (exit or roll the long).

    close_price is per-contract total dollars (mirrors buy_leap's execution_price).
    Realized P&L is the sale proceeds minus the stored cost basis; the position's
    leap is cleared and the position is marked closed if no shares/shorts remain.
    """
    close_per_contract = float(payload.get("close_price") or 0)
    close_total = float(payload.get("close_total") or close_per_contract * contracts)
    intrinsic_per_contract = max((stock_price or 0) - (strike or 0), 0) * 100
    extrinsic_remaining = max(close_per_contract - intrinsic_per_contract, 0) * contracts

    # Cost basis from the stored LEAP (caller may override).
    state = log.load_state()
    position = log.find_position(state, ticker)
    leap = (position or {}).get("leap") or {}
    cost_basis = payload.get("cost_basis")
    cost_basis = float(cost_basis if cost_basis is not None else leap.get("cost_basis") or 0)
    realized_pnl = round(close_total - cost_basis, 2)

    execution = {
        "ticker": ticker, "action": "close_leap", "strike": strike, "contracts": contracts,
        "close_price": close_per_contract, "close_total": close_total, "stock_price": stock_price,
        "cost_basis": round(cost_basis, 2), "realized_pnl": realized_pnl,
        "extrinsic_remaining": round(extrinsic_remaining, 2),
    }

    def apply(position):
        position["leap"] = None
        shares = position.get("shares") or {}
        if not position.get("short_calls") and int(shares.get("count") or 0) == 0:
            position["status"] = "closed"
    return execution, apply


def _sell_short(payload, ticker, strike, contracts, stock_price):
    premium_per_share = float(payload.get("premium_per_share") or 0)
    premium_total = float(payload.get("premium_total") or premium_per_share * contracts * 100)
    intrinsic_per_share = max((stock_price or 0) - (strike or 0), 0)
    entry_extrinsic_per_share = round(max(premium_per_share - intrinsic_per_share, 0), 4)
    execution = {
        "ticker": ticker, "action": "sell_short", "strike": strike, "contracts": contracts,
        "premium_per_share": premium_per_share, "premium_total": premium_total,
        "stock_price": stock_price, "entry_extrinsic_per_share": entry_extrinsic_per_share,
    }

    def apply(position):
        position.setdefault("short_calls", []).append({
            "strike": strike, "contracts": contracts, "open_date": log.utcnow()[:10],
            "expiration": payload.get("expiration"),
            "dte": payload.get("dte", 5), "entry_extrinsic_per_share": entry_extrinsic_per_share,
            "entry_premium_total": premium_total, "current_bid": premium_per_share,
            "current_cost": premium_total,
        })
    return execution, apply


def _close_short(payload, ticker, strike, contracts, stock_price):
    close_per_share = float(payload.get("close_price_per_share") or 0)
    close_total = float(payload.get("close_total") or close_per_share * contracts * 100)
    intrinsic_per_share = max((stock_price or 0) - (strike or 0), 0)
    extrinsic_paid_back = round(max(close_per_share - intrinsic_per_share, 0), 4)

    # Pull the matching open short to recover what extrinsic we originally sold.
    state = log.load_state()
    position = log.find_position(state, ticker)
    extrinsic_sold = payload.get("extrinsic_sold")
    if extrinsic_sold is None and position:
        for sc in position.get("short_calls", []):
            if sc.get("strike") == strike:
                extrinsic_sold = sc.get("entry_extrinsic_per_share")
                break
    extrinsic_sold = round(float(extrinsic_sold or 0), 4)
    net_juice = round(extrinsic_sold - extrinsic_paid_back, 4)
    net_juice_total = round(net_juice * contracts * 100, 2)
    execution = {
        "ticker": ticker, "action": "close_short", "strike": strike, "contracts": contracts,
        "close_price_per_share": close_per_share, "close_total": close_total,
        "stock_price": stock_price, "extrinsic_sold": extrinsic_sold,
        "extrinsic_paid_back": extrinsic_paid_back, "net_juice": net_juice,
        "net_juice_total": net_juice_total,
    }

    def apply(position):
        position["short_calls"] = [sc for sc in position.get("short_calls", [])
                                   if sc.get("strike") != strike]
    return execution, apply


def _roll_short(payload, ticker, contracts, stock_price, mode, price_source):
    """Roll an open short in one operation: buy to close the existing leg, then
    sell a new one. The caller chooses the new week (``to_expiration``/``to_dte``)
    and strike (``to_strike``) independently — same week / different week and same
    strike / different strike are all just different values here. Two immutable
    executions are recorded (a close_short then a sell_short) so the theta ledger
    and extrinsic-payback meters derive exactly as they do for a manual two-step
    roll; only the position mutation is applied once, atomically, at the end."""
    from_strike = payload.get("from_strike", payload.get("strike"))
    to_strike = payload.get("to_strike")
    if from_strike is None or to_strike is None:
        raise ValueError("roll_short requires from_strike and to_strike")
    contracts = int(contracts or 0)

    close_payload = {
        "ticker": ticker, "strike": from_strike, "contracts": contracts,
        "close_price_per_share": payload.get("close_price_per_share"),
        "close_total": payload.get("close_total"),
        "stock_price": stock_price,
        "extrinsic_sold": payload.get("extrinsic_sold"),
    }
    close_exec, close_apply = _close_short(close_payload, ticker, from_strike, contracts, stock_price)

    sell_payload = {
        "ticker": ticker, "strike": to_strike, "contracts": contracts,
        "premium_per_share": payload.get("premium_per_share"),
        "premium_total": payload.get("premium_total"),
        "stock_price": stock_price,
        "expiration": payload.get("to_expiration"),
        "dte": payload.get("to_dte", payload.get("dte", 5)),
    }
    sell_exec, sell_apply = _sell_short(sell_payload, ticker, to_strike, contracts, stock_price)

    for leg_exec, leg in ((close_exec, "close"), (sell_exec, "open")):
        leg_exec["mode"] = mode
        leg_exec["price_source"] = price_source
        leg_exec["roll_leg"] = leg

    stored_close = log.append_execution(close_exec)
    stored_sell = log.append_execution(sell_exec)

    # Apply both position mutations onto the freshly written state, once.
    state = log.load_state()
    position = _ensure_position(state, ticker)
    close_apply(position)
    sell_apply(position)
    log.recompute_derived(state)
    log.save_state(state)

    close_total = float(stored_close.get("close_total") or 0)
    new_total = float(stored_sell.get("premium_total") or 0)
    return {
        "success": True,
        "status": "filled",
        "execution_id": stored_sell["id"],
        "close_execution_id": stored_close["id"],
        "timestamp": stored_sell["date"],
        "mode": mode,
        "captured_price": stock_price,
        "net_credit": round(new_total - close_total, 2),
        "executions": [stored_close, stored_sell],
    }


def roll_suggestion(ticker: str) -> dict:
    """Next weekly short strike = stock - 1.5*ATR (rounded to 0.5)."""
    df = data_handler.get_daily(ticker)
    price = indicators.last(df)
    atr_val = indicators.atr(df)
    if price is None or atr_val is None:
        return {"ticker": ticker, "error": "insufficient data"}
    return {
        "ticker": ticker,
        "stock_price": round(price, 2),
        "atr": round(atr_val, 2),
        "atr_mult": config.SHORT_ATR_MULT,
        "suggested_strike": indicators.short_strike(price, atr_val),
    }
