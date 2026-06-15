"""Build and *preview* (dry-run) Schwab bracket orders from executor signals.

Schwab's Trader API has no paper-trading environment — its order endpoints act
on the real, live brokerage account. So instead of placing an order, this module
submits the bracket to Schwab's ``previewOrder`` endpoint, which validates the
order against the live account (buying power, pricing, tradeability) and returns
the projected cost / commission / fees plus any rejects — without ever filling.

This is the safe bridge between detection and live trading: it proves the order
maps correctly and would be accepted, while guaranteeing nothing executes. A real
fill would call a ``place_order`` (POST .../orders) path, which is intentionally
NOT wired here.
"""
from __future__ import annotations

from providers.base import ProviderError
from providers.schwab import SchwabProvider

EQUITY = "EQUITY"


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------
def _leg(instruction: str, quantity: int, symbol: str) -> dict:
    return {
        "instruction": instruction,
        "quantity": int(quantity),
        "instrument": {"symbol": str(symbol).upper(), "assetType": EQUITY},
    }


def build_bracket_order(signal: dict) -> dict:
    """Signal -> a Schwab one-triggers-OCO bracket order (entry + target + stop).

    Long:  BUY entry, then OCO of SELL limit @target / SELL stop @stop.
    Short: SELL_SHORT entry, then OCO of BUY_TO_COVER limit @target / @stop.

    The entry leg follows the signal's ``order_type`` (default LIMIT @entry_price;
    MARKET drops the price). Both protective exits are DAY orders for the full
    position. Raises ValueError/KeyError on a malformed signal.
    """
    direction = str(signal.get("direction") or "").lower()
    if direction not in ("long", "short"):
        raise ValueError("signal.direction must be Long or Short")

    symbol = str(signal["ticker"]).upper()
    qty = int(signal["position_size"])
    if qty <= 0:
        raise ValueError("position_size must be greater than 0")

    entry = round(float(signal["entry_price"]), 2)
    target = round(float(signal["target_price"]), 2)
    stop = round(float(signal["stop_price"]), 2)
    if entry <= 0 or target <= 0 or stop <= 0:
        raise ValueError("entry, target, and stop prices must be greater than 0")

    if direction == "long":
        entry_instr, exit_instr = "BUY", "SELL"
    else:
        entry_instr, exit_instr = "SELL_SHORT", "BUY_TO_COVER"

    take_profit = {
        "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY",
        "price": f"{target:.2f}", "orderStrategyType": "SINGLE",
        "orderLegCollection": [_leg(exit_instr, qty, symbol)],
    }
    stop_loss = {
        "orderType": "STOP", "session": "NORMAL", "duration": "DAY",
        "stopPrice": f"{stop:.2f}", "orderStrategyType": "SINGLE",
        "orderLegCollection": [_leg(exit_instr, qty, symbol)],
    }

    entry_type = str(signal.get("order_type") or "LIMIT").upper()
    order = {
        "orderType": entry_type,
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "TRIGGER",
        "orderLegCollection": [_leg(entry_instr, qty, symbol)],
        "childOrderStrategies": [
            {"orderStrategyType": "OCO", "childOrderStrategies": [take_profit, stop_loss]}
        ],
    }
    if entry_type != "MARKET":
        order["price"] = f"{entry:.2f}"
    return order


# ---------------------------------------------------------------------------
# Preview response normalization
# ---------------------------------------------------------------------------
def _messages(items) -> list[str]:
    """Pull human-readable strings out of Schwab validation/alert lists."""
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
            continue
        msg = it.get("message") or it.get("text") or it.get("code") if isinstance(it, dict) else None
        if msg:
            out.append(str(msg))
    return out


def _sum_values(node) -> float:
    """Recursively sum every numeric ``value`` leaf under a fee/commission node.

    Schwab's commissionAndFee block nests per-leg values a few levels deep and the
    exact shape varies; summing the ``value`` leaves is a shape-tolerant way to get
    a best-effort total cost without hard-coding the schema.
    """
    total = 0.0
    if isinstance(node, dict):
        if "value" in node:
            try:
                total += float(node["value"])
            except (TypeError, ValueError):
                pass
        for v in node.values():
            total += _sum_values(v)
    elif isinstance(node, list):
        for v in node:
            total += _sum_values(v)
    return total


def normalize_preview(payload: dict) -> dict:
    """Schwab previewOrder response -> a compact, UI-friendly summary.

    Defensive about the (loosely documented, evolving) preview schema: surfaces
    the order value, a best-effort commission/fee total, validation status, and
    any reject/alert messages, while passing the raw payload through for detail.
    """
    payload = payload or {}
    strategy = payload.get("orderStrategy") or {}
    validation = payload.get("orderValidationResult") or {}
    fees = payload.get("commissionAndFee") or {}

    rejects = _messages(validation.get("rejects"))
    alerts = _messages(validation.get("alerts"))
    if rejects:
        status = "REJECTED"
    elif alerts:
        status = "WARNING"
    else:
        status = "OK"

    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return {
        "status": status,
        "orderValue": _num(strategy.get("orderValue")),
        "quantity": _num(strategy.get("quantity")),
        "price": _num(strategy.get("price")),
        "estimatedCost": round(_sum_values(fees), 2) if fees else None,
        "rejects": rejects,
        "alerts": alerts,
        "raw": payload,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def preview_bracket(signal: dict, *, account_hash: str | None = None) -> dict:
    """Build the bracket for ``signal`` and dry-run it via Schwab previewOrder.

    Returns ``{ok, mode:"PREVIEW", order, preview, account}`` on success, or
    ``{ok: False, error}`` when credentials are missing, the signal is malformed,
    or Schwab rejects the request. NOTHING is ever placed.
    """
    if not SchwabProvider.configured():
        return {
            "ok": False,
            "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN).",
        }

    try:
        order = build_bracket_order(signal or {})
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": f"Invalid signal for order: {e}"}

    provider = SchwabProvider()
    account_label = None
    try:
        if not account_hash:
            numbers = provider.account_numbers()
            if not numbers:
                return {"ok": False, "error": "No Schwab accounts available for this login."}
            entry = numbers[0]
            account_hash = entry.get("hashValue")
            num = str(entry.get("accountNumber") or "")
            account_label = f"****{num[-4:]}" if len(num) >= 4 else (num or "account")
        if not account_hash:
            return {"ok": False, "error": "Could not resolve a Schwab account hash."}
        raw = provider.preview_order(account_hash, order)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "mode": "PREVIEW",
        "account": account_label,
        "order": order,
        "preview": normalize_preview(raw),
    }
