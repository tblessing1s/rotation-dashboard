"""Live-order verification — diff what we recorded against Schwab's own record.

The live execution path (place → poll → commit) had only ever run against mocks.
This is the harness for deliberately exercising it: after a real live roll or
exit, re-fetch each order from Schwab and confirm three things independently:

  1. the broker says the order actually FILLED,
  2. the per-leg fill price Schwab reports matches the price we logged
     (to the cent), and
  3. the position still reconciles against the broker account.

Inputs come from the ``order_receipts`` written at fill time (order id + the
committed execution ids). Read-only: it fetches and compares, never writes to
state (reconcile runs with persist=False). Returns a structured GREEN / diffs
report for the API + UI.
"""
from __future__ import annotations

import logging_handler as log
import reconcile
import schwab_api
from executor import INSTRUCTION

logger = log.logger

PRICE_TOLERANCE = 0.01  # per-share $; a fill matches if within a cent


def _broker_legs(order: dict) -> list[dict]:
    """Per-leg (symbol, instruction, quantity, fill_price) from a Schwab order,
    matching executionLegs -> orderLegCollection by legId (same shape the
    executor's fill parser uses)."""
    legs: dict = {}
    for i, leg in enumerate(order.get("orderLegCollection") or [], start=1):
        leg_id = leg.get("legId") or i
        legs[leg_id] = {
            "symbol": ((leg.get("instrument") or {}).get("symbol") or "").strip(),
            "instruction": (leg.get("instruction") or "").upper(),
            "quantity": leg.get("quantity"),
            "fill_price": None,
        }
    for act in order.get("orderActivityCollection") or []:
        for ex in act.get("executionLegs") or []:
            leg = legs.get(ex.get("legId"))
            if leg is not None and ex.get("price") is not None:
                try:
                    leg["fill_price"] = float(ex["price"])
                except (TypeError, ValueError):
                    pass
    return list(legs.values())


def _recorded_per_share(execution: dict) -> float | None:
    """The per-share option price we logged for an execution, normalized across
    the leap (per-contract dollars) and short (per-share) storage conventions."""
    action = execution.get("action")
    try:
        if action == "buy_leap":
            return float(execution.get("execution_price") or 0) / 100.0
        if action == "close_leap":
            return float(execution.get("close_price") or 0) / 100.0
        if action == "sell_short":
            return float(execution.get("premium_per_share") or 0)
        if action == "close_short":
            return float(execution.get("close_price_per_share") or 0)
    except (TypeError, ValueError):
        return None
    return None


def _match_leg(execution: dict, broker_legs: list[dict]) -> dict | None:
    """Pair a committed execution to its broker leg by instruction + strike."""
    want_instr = INSTRUCTION.get(execution.get("action"))
    strike = execution.get("strike")
    for leg in broker_legs:
        if leg["instruction"] != want_instr:
            continue
        try:
            parsed = reconcile.parse_option_symbol(leg["symbol"])
        except Exception:  # noqa: BLE001 — a malformed broker symbol just won't match
            continue
        if strike is not None and abs(float(parsed.get("strike")) - float(strike)) < 1e-6:
            return leg
    return None


def _verify_receipt(receipt: dict, executions_by_id: dict, client) -> dict:
    """Verify one filled order against Schwab. Returns a per-order verdict."""
    order_id = receipt.get("order_id")
    out = {"order_id": order_id, "ticker": receipt.get("ticker"),
           "kind": receipt.get("kind"), "captured_at": receipt.get("captured_at"),
           "legs": [], "issues": [], "ok": False}

    try:
        order = client.get_order(receipt.get("account_hash"), order_id)
    except Exception as e:  # noqa: BLE001 — a fetch failure is a reportable issue
        out["issues"].append(f"could not fetch order from Schwab: {e}")
        return out

    status = (order.get("status") or "").upper()
    out["broker_status"] = status
    if status != "FILLED":
        out["issues"].append(f"broker status is {status or 'unknown'}, not FILLED")

    broker_legs = _broker_legs(order)
    for exec_id in receipt.get("execution_ids") or []:
        execution = executions_by_id.get(exec_id)
        if execution is None:
            out["issues"].append(f"committed execution {exec_id} not found in the log")
            continue
        recorded = _recorded_per_share(execution)
        leg = _match_leg(execution, broker_legs)
        broker_price = leg.get("fill_price") if leg else None
        drift = (abs(broker_price - recorded)
                 if broker_price is not None and recorded is not None else None)
        leg_ok = drift is not None and drift <= PRICE_TOLERANCE
        out["legs"].append({
            "execution_id": exec_id, "action": execution.get("action"),
            "strike": execution.get("strike"),
            "recorded_price": recorded, "broker_price": broker_price,
            "drift": round(drift, 4) if drift is not None else None,
            "ok": leg_ok,
        })
        if leg is None:
            out["issues"].append(f"{execution.get('action')} {execution.get('strike')}: "
                                 "no matching broker leg")
        elif not leg_ok:
            out["issues"].append(
                f"{execution.get('action')} {execution.get('strike')}: recorded "
                f"{recorded} vs broker {broker_price} (drift {drift})")

    out["ok"] = status == "FILLED" and bool(out["legs"]) and not out["issues"]
    return out


def verify_live_fills(limit: int = 20) -> dict:
    """Verify the most recent live fills against Schwab + reconcile the book.

    Read-only. When Schwab isn't connected (or in demo mode) it still returns the
    receipts and the reconcile summary, flagging that broker verification was
    skipped rather than passing vacuously.
    """
    state = log.load_state()
    receipts = list(reversed(state.get("order_receipts") or []))[:limit]
    executions_by_id = {e.get("id"): e for e in state.get("executions") or []}

    connected = schwab_api.configured()
    orders: list[dict] = []
    if connected:
        import data_handler
        client = data_handler.client()
        for r in receipts:
            orders.append(_verify_receipt(r, executions_by_id, client))
    else:
        orders = [{"order_id": r.get("order_id"), "ticker": r.get("ticker"),
                   "kind": r.get("kind"), "captured_at": r.get("captured_at"),
                   "ok": None, "issues": ["Schwab not connected — broker check skipped"],
                   "legs": []} for r in receipts]

    try:
        recon = reconcile.run_reconciliation(persist=False)
        recon_summary = {"status": recon.get("status"),
                         "broker_ok": recon.get("broker_ok"),
                         "open_diffs": len([d for d in recon.get("diffs", [])
                                            if reconcile._diff_open(d)])}
    except Exception as e:  # noqa: BLE001 — reconcile failure is reported, not fatal
        recon_summary = {"status": "error", "error": str(e)}

    verified = [o for o in orders if o.get("ok") is not None]
    all_ok = connected and all(o["ok"] for o in verified) if verified else None
    return {
        "schwab_connected": connected,
        "checked": len(orders),
        "all_ok": all_ok,
        "orders": orders,
        "reconcile": recon_summary,
    }
