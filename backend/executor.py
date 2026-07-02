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

# Why a roll happened — the whipsaw ledger key. Unrecognized values fall back to
# "scheduled" so the ledger enum stays clean for later calibration.
ROLL_REASONS = {"scheduled", "75%-rule", "defend", "earnings", "kill-switch-exit"}

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

    # Level 5 gate (Account & Juice) — entry only. A blocking failure stops the
    # buy_leap unless the payload carries an explicit override_reason, which is
    # recorded on the immutable execution (see _buy_leap).
    if action == "buy_leap":
        _enforce_account_gate(payload, ticker, contracts)

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


def _enforce_account_gate(payload, ticker, contracts):
    """Run the Level 5 gate for an entry. Blocking failures raise ValueError
    (HTTP 400) unless override_reason is supplied; the gate result is stashed on
    the payload so _buy_leap can log the override + failed checks."""
    import account_gate
    leap_cost_ps = None
    if payload.get("execution_price"):  # per-contract dollars -> per-share
        leap_cost_ps = float(payload["execution_price"]) / 100.0
    gate = account_gate.evaluate(
        ticker, contracts=contracts,
        leap_cost_per_share=leap_cost_ps,
        weekly_extrinsic_per_share=payload.get("weekly_extrinsic_per_share"),
    )
    payload["_account_gate"] = gate
    if gate["pass"]:
        return
    reason = (payload.get("override_reason") or "").strip()
    if not reason:
        failed = ", ".join(gate["blocking_failures"])
        details = "; ".join(
            f"{c['id']}: {c['label']}" for c in gate["checks"]
            if c["blocking"] and not c["pass"])
        raise ValueError(
            f"Level 5 gate blocked entry ({failed}) — {details}. "
            "Pass override_reason to enter anyway (logged).")


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


def _commit_roll_from_pending(rec: dict, order: dict):
    """Commit a filled two-leg roll: overlay the actual per-leg fill prices onto
    the payload (falling back to the staged estimates) and record both legs."""
    payload = dict(rec.get("payload") or {})
    close_px, open_px = _roll_leg_fills(order, rec.get("close_option_symbol", ""),
                                        rec.get("open_option_symbol", ""))
    if close_px is not None:
        payload["close_price_per_share"] = close_px
    if open_px is not None:
        payload["premium_per_share"] = open_px
    return _commit_roll(payload, rec["ticker"], int(rec["contracts"]),
                        rec.get("stock_price"), "live", rec.get("price_source", "schwab"))


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
        if rec.get("kind") == "roll_short":
            result = _commit_roll_from_pending(rec, order)
        else:
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

    # Level-5 gate context: log any override (with what it overrode) on the
    # immutable record, and resolve the circuit breaker + dividend to store.
    gate = payload.get("_account_gate") or {}
    if payload.get("override_reason"):
        execution["override"] = {
            "reason": str(payload["override_reason"]).strip(),
            "failed_checks": gate.get("blocking_failures", []),
        }

    # Entry REQUIRES a line-in-the-sand: operator's price, else the suggested
    # default max(MA50, entry - 2xATR) — the entry always stores one.
    cb_price = payload.get("circuit_breaker_price")
    cb_source = "operator"
    if cb_price is None:
        cb_price = (gate.get("suggested_circuit_breaker") or {}).get("price")
        cb_source = "default"
        if cb_price is None:
            import account_gate
            cb_price = account_gate.suggested_circuit_breaker(ticker).get("price")
    circuit_breaker = ({"price": round(float(cb_price), 2), "source": cb_source,
                        "set_at": log.utcnow()[:10]} if cb_price is not None else None)
    execution["circuit_breaker_price"] = circuit_breaker["price"] if circuit_breaker else None

    dividend = gate.get("dividend")
    if dividend is None:
        import dividends
        try:
            dividend = dividends.next_dividend(ticker)
        except Exception:  # noqa: BLE001 — dividend data must never block an entry
            dividend = {"ex_date": None, "amount": None, "source": "error"}

    def apply(position):
        position["circuit_breaker"] = circuit_breaker
        position["dividend"] = dividend
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


def _roll_reason(payload) -> str:
    reason = (payload.get("roll_reason") or "").strip()
    return reason if reason in ROLL_REASONS else "scheduled"


def _next_roll_id(state) -> str:
    n = sum(1 for e in state.get("executions", [])
            if e.get("roll_id") and e.get("action") == "close_short")
    return f"roll_{n + 1:03d}"


def _roll_short(payload, ticker, contracts, stock_price, mode, price_source):
    """Roll an open short in one operation: buy to close the existing leg, then
    sell a new one. The caller chooses the new week (``to_expiration``/``to_dte``)
    and strike (``to_strike``) independently — same week / different week and same
    strike / different strike are all just different values here.

    Paper (logged) mode records both legs immediately at the supplied/midpoint
    prices. LIVE mode transmits ONE two-leg net-credit/debit ticket (no legging
    risk) and commits both legs only when the ticket fills, via the same
    pending -> poll -> commit/auto-cancel lifecycle as single-leg orders."""
    from_strike = payload.get("from_strike", payload.get("strike"))
    to_strike = payload.get("to_strike")
    if from_strike is None or to_strike is None:
        raise ValueError("roll_short requires from_strike and to_strike")
    contracts = int(contracts or 0)
    if mode == "live" and schwab_api.configured():
        return _place_live_roll(payload, ticker, contracts, stock_price, price_source)
    return _commit_roll(payload, ticker, contracts, stock_price, mode, price_source)


def _place_live_roll(payload, ticker, contracts, stock_price, price_source):
    """Transmit the roll as a single two-leg NET order and park it as pending."""
    client = data_handler.client()
    account_hash = client.primary_account_hash()

    def _symbol(prefix):
        sym = payload.get(f"{prefix}_option_symbol")
        if sym:
            return sym
        expiration = payload.get(f"{prefix}_expiration")
        strike = payload.get(f"{prefix}_strike")
        if not expiration:
            raise ValueError(
                f"live roll needs {prefix}_option_symbol or {prefix}_expiration to build the contract")
        return schwab_api.occ_option_symbol(ticker, expiration, strike, call=True)

    close_symbol = _symbol("from")
    open_symbol = _symbol("to")
    buyback = float(payload.get("close_price_per_share") or 0)
    new_premium = float(payload.get("premium_per_share") or 0)
    net = round(new_premium - buyback, 2)
    order = schwab_api.build_roll_order(contracts, close_symbol, open_symbol, net)
    placed = client.place_order(account_hash, order)
    order_id = placed.get("orderId")
    if not order_id:
        raise schwab_api.SchwabError("Schwab accepted the roll but returned no order id")

    log.save_pending_order(order_id, {
        "kind": "roll_short",
        "payload": payload, "ticker": ticker, "action": "roll_short",
        "contracts": contracts, "stock_price": stock_price,
        "price_source": price_source, "account_hash": account_hash,
        "close_option_symbol": close_symbol, "open_option_symbol": open_symbol,
        "net_limit": net, "placed_at": log.utcnow(),
    })
    return {
        "success": True,
        "status": "working",
        "order_id": str(order_id),
        "mode": "live",
        "option_symbols": [close_symbol, open_symbol],
        "net_limit": net,
    }


def _roll_leg_fills(order: dict, close_symbol: str, open_symbol: str):
    """(close_fill, open_fill) per-share prices from a filled two-leg order's
    activity, matched by legId -> orderLegCollection symbol. None when absent."""
    leg_symbol = {}
    for i, leg in enumerate(order.get("orderLegCollection") or [], start=1):
        sym = ((leg.get("instrument") or {}).get("symbol") or "").strip()
        leg_symbol[leg.get("legId") or i] = sym
    close_px = open_px = None
    try:
        for act in order.get("orderActivityCollection", []) or []:
            for leg in act.get("executionLegs", []) or []:
                price = leg.get("price")
                if price is None:
                    continue
                sym = leg_symbol.get(leg.get("legId"))
                if sym == close_symbol.strip():
                    close_px = float(price)
                elif sym == open_symbol.strip():
                    open_px = float(price)
    except (TypeError, ValueError):
        pass
    return close_px, open_px


def _commit_roll(payload, ticker, contracts, stock_price, mode, price_source):
    from_strike = payload.get("from_strike", payload.get("strike"))
    to_strike = payload.get("to_strike")
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

    # Link the pair for the roll ledger (derived in recompute_derived).
    roll_id = _next_roll_id(log.load_state())
    reason = _roll_reason(payload)
    for leg_exec, leg in ((close_exec, "close"), (sell_exec, "open")):
        leg_exec["mode"] = mode
        leg_exec["price_source"] = price_source
        leg_exec["roll_leg"] = leg
        leg_exec["roll_id"] = roll_id
        leg_exec["roll_reason"] = reason

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


def defend_recommendation(ticker: str) -> dict:
    """Defensive roll-down for a breached short (underlying < short strike):
    new strike = price − 1.5×ATR (GREEN) / 2.0×ATR (YELLOW), same or next weekly
    expiry, with the estimated net credit/debit, the new short's extrinsic, and
    the effect on effective cost basis. Prices come from the stored short mark +
    a Black-Scholes estimate at trailing realized vol, so this works in demo /
    off-hours; the staged roll itself re-prices from the live chain."""
    import screening

    ticker = ticker.upper()
    state = log.load_state()
    pos = log.find_position(state, ticker)
    if not pos:
        return {"ticker": ticker, "error": "no position"}
    df = data_handler.get_daily(ticker)
    price = indicators.last(df)
    atr_val = indicators.atr(df) if df is not None else None
    hv = indicators.hist_vol(df) if df is not None else None
    if price is None or atr_val is None:
        return {"ticker": ticker, "error": "insufficient data"}

    breached = [sc for sc in pos.get("short_calls", [])
                if sc.get("strike") is not None and price < float(sc["strike"])]
    if not breached:
        return {"ticker": ticker, "breached": False, "stock_price": round(price, 2)}
    sc = min(breached, key=lambda s: s.get("dte") if s.get("dte") is not None else 1e9)

    regime = screening.regime().get("status", "yellow")
    from option_chain import REGIME_ATR_MULT
    atr_mult = REGIME_ATR_MULT.get(regime, REGIME_ATR_MULT["yellow"])
    new_strike = indicators.short_strike(price, atr_val, atr_mult)

    contracts = int(sc.get("contracts") or 0)
    dte = sc.get("dte")
    roll_dte = int(dte) if dte else 5  # same week when it has time, else next weekly
    buyback = sc.get("current_bid")
    new_premium = new_extrinsic = None
    if hv:
        bs = indicators._bs_call_price(price, new_strike, max(roll_dte, 1) / 365.0,
                                       config.RISK_FREE_RATE, hv / 100.0)
        new_premium = round(bs, 2)
        new_extrinsic = round(max(bs - max(price - new_strike, 0.0), 0.0), 2)
    net = (round((new_premium - float(buyback)) * contracts * 100, 2)
           if (new_premium is not None and buyback is not None) else None)
    return {
        "ticker": ticker,
        "breached": True,
        "stock_price": round(price, 2),
        "atr": round(atr_val, 2),
        "regime": regime,
        "atr_mult": atr_mult,
        "current_short": {"strike": sc.get("strike"), "contracts": contracts,
                          "dte": dte, "expiration": sc.get("expiration"),
                          "buyback_per_share": buyback},
        "recommended_strike": new_strike,
        "recommended_dte": roll_dte,
        "new_premium_per_share": new_premium,
        "new_extrinsic_per_share": new_extrinsic,
        "net_total": net,
        # A net credit lowers the effective LEAP cost basis; a debit raises it.
        "cost_basis_effect": -net if net is not None else None,
        "source": "estimate",
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
