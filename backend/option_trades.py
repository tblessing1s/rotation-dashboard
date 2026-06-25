"""Execute a CFM option trade *from the dashboard* and snapshot its theta entry.

This is the one place in the app that can place a REAL order against the live
Schwab account, so it is built defensively:

  * ``preview_option`` is always safe — it only dry-runs against Schwab's
    previewOrder endpoint and never fills.
  * ``place_option`` actually transmits. It is gated behind a hard kill-switch
    env flag (``SCHWAB_LIVE_TRADING_ENABLED``); without it set, the function
    refuses *before* contacting Schwab's order endpoint, so a stray button click
    or a test can never put on a real position.

The reason placement lives here (rather than just logging a fill after the fact)
is timing: the instant the option fills, we hit Schwab's quotes endpoint for the
underlying, so the premium and the stock price are captured together on the same
feed. That pairing is what makes the intrinsic/extrinsic split exact
(options_math.decompose) — the entry row of the theta ledger.
"""
from __future__ import annotations

import os
import time

import db
import schwab_orders
from options_math import decompose
from providers.base import ProviderError
from providers.schwab import SchwabProvider

# How long to wait for a fill before reporting the order as still working. A
# market order fills well inside this; a resting limit may not, and that is fine
# — we report it WORKING and capture nothing rather than guess a fill.
DEFAULT_MAX_POLLS = 8
DEFAULT_POLL_DELAY = 1.0

_LIVE_FLAG = "SCHWAB_LIVE_TRADING_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def available() -> bool:
    return SchwabProvider.configured()


def live_trading_enabled() -> bool:
    """The kill-switch: real orders only transmit when this env flag is truthy."""
    return str(os.environ.get(_LIVE_FLAG, "")).strip().lower() in _TRUTHY


def _mask(account_number) -> str:
    s = str(account_number or "")
    return f"****{s[-4:]}" if len(s) >= 4 else (s or "account")


def _resolve_account(provider: SchwabProvider, account_hash: str | None) -> tuple[str, str | None]:
    """Resolve (account_hash, masked_label), defaulting to the first account."""
    if account_hash:
        return account_hash, None
    numbers = provider.account_numbers()
    if not numbers:
        raise ProviderError("No Schwab accounts available for this login.")
    entry = numbers[0]
    resolved = entry.get("hashValue")
    if not resolved:
        raise ProviderError("Could not resolve a Schwab account hash.")
    return resolved, _mask(entry.get("accountNumber"))


def _stock_price_from_quote(quote: dict) -> float | None:
    """Pick the best single stock price from a Schwab quote: last, else mark,
    else the bid/ask midpoint."""
    if not quote:
        return None
    for key in ("last", "mark"):
        v = quote.get(key)
        if v:
            return float(v)
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid and ask:
        return round((float(bid) + float(ask)) / 2, 4)
    return None


def _leg_info(order_like: dict) -> tuple[str | None, str, object]:
    """Pull (osi_symbol, side, quantity) from any order-shaped dict.

    Works for both an order we built and the order Schwab echoes back on
    get_order, so fill capture never depends on the original spec. Side
    includes "to_close" suffix when applicable (e.g., "sell_to_close").
    """
    leg = ((order_like or {}).get("orderLegCollection") or [{}])[0]
    inst = leg.get("instrument") or {}
    instruction = str(leg.get("instruction") or "").upper()
    # Preserve the to_close/to_open distinction in the side field.
    if "SELL_TO_CLOSE" in instruction:
        side = "sell_to_close"
    elif "BUY_TO_CLOSE" in instruction:
        side = "buy_to_close"
    elif instruction.startswith("SELL"):
        side = "sell"
    else:
        side = "buy"
    return inst.get("symbol"), side, leg.get("quantity")


def _poll_for_fill(provider, account_hash, order_id, max_polls, poll_delay, sleep_fn):
    """Poll get_order until a fill price appears or the order reaches a terminal
    state. Returns (status, fill, last_order_payload)."""
    status, fill, last = "WORKING", None, {}
    for attempt in range(max(1, int(max_polls))):
        try:
            last = provider.get_order(account_hash, order_id)
        except ProviderError:
            last = {}
        fill = schwab_orders.parse_fill(last)
        status = fill.get("status") or status
        if fill.get("fillPrice") is not None or status in schwab_orders.TERMINAL_ORDER_STATES:
            break
        if attempt < max_polls - 1:
            sleep_fn(poll_delay)
    return status, fill, last


def _capture_fill(provider, base: dict, order_like: dict, order_id: str, fill: dict) -> dict:
    """Snapshot a confirmed fill into the theta ledger and attach it to `base`.

    Idempotent: if this order id was already captured, the stored row is
    returned instead of re-quoting. The underlying price is the live quote taken
    *now* — tight when called right at the fill (the poll path), an approximation
    when re-checking a fill that happened earlier (the status path). Mutates and
    returns `base`.
    """
    fill_price = fill.get("fillPrice")
    filled_qty = fill.get("filledQuantity")

    existing = db.get_option_fill_by_order(order_id)
    if existing:
        base["ok"] = True
        base["fill"] = existing
        base["split"] = existing.get("payload", {}).get("split")
        base["ledger"] = existing
        base["alreadyCaptured"] = True
        return base

    osi, side, leg_qty = _leg_info(order_like)
    try:
        parsed = schwab_orders.parse_osi_symbol(osi)
    except (ValueError, TypeError) as e:
        base["ok"] = True
        base["fill"] = {"fillPrice": fill_price, "filledQuantity": filled_qty}
        base["warning"] = f"Filled at {fill_price}, but the option symbol could not be parsed ({e})."
        return base

    try:
        quote = provider.get_quote(parsed["underlying"])
    except ProviderError as e:
        base["ok"] = True
        base["fill"] = {"fillPrice": fill_price, "filledQuantity": filled_qty}
        base["warning"] = f"Filled at {fill_price}, but the stock quote failed ({e}); theta snapshot skipped."
        return base

    stock_price = _stock_price_from_quote(quote)
    if stock_price is None:
        base["ok"] = True
        base["fill"] = {"fillPrice": fill_price, "filledQuantity": filled_qty}
        base["warning"] = "Filled, but the stock quote had no usable price; theta snapshot skipped."
        return base

    split = decompose(parsed["option_type"], parsed["strike"], stock_price, fill_price)
    record = {
        "order_id": order_id,
        "underlying": parsed["underlying"],
        "osi_symbol": osi,
        "option_type": split["type"],
        "strike": parsed["strike"],
        "expiry": parsed["expiry"],
        "side": side,
        "quantity": int(filled_qty or leg_qty or 0),
        "premium": fill_price,
        "stock_price": stock_price,
        "intrinsic": split["intrinsic"],
        "extrinsic": split["extrinsic"],
        "quote_time": quote.get("quoteTimeMs"),
        "filled_at": db.utcnow(),
        "split": split,
    }
    base["ok"] = True
    base["fill"] = record
    base["split"] = split
    base["ledger"] = db.record_option_fill(record)
    return base


def _finalize(provider, base: dict, order_like: dict, order_id: str, fill: dict) -> dict:
    """Either capture a fill into `base`, or mark the order still working."""
    if not fill or fill.get("fillPrice") is None:
        base["ok"] = True
        base["fill"] = None
        base["note"] = ("Order is working; not filled yet. Re-check status, cancel, "
                        "or re-price (replace) to chase the fill.")
        return base
    return _capture_fill(provider, base, order_like, order_id, fill)


def preview_option(spec: dict, *, account_hash: str | None = None) -> dict:
    """Build the option order for ``spec`` and dry-run it via Schwab previewOrder.

    Always safe — nothing is ever placed. Returns ``{ok, mode:"PREVIEW", order,
    preview, account}`` or ``{ok: False, error}``.
    """
    if not SchwabProvider.configured():
        return {"ok": False, "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN)."}
    try:
        order = schwab_orders.build_option_order(spec or {})
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": f"Invalid option spec: {e}"}

    provider = SchwabProvider()
    try:
        resolved_hash, label = _resolve_account(provider, account_hash)
        raw = provider.preview_order(resolved_hash, order)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "mode": "PREVIEW",
        "account": label,
        "order": order,
        "preview": schwab_orders.normalize_preview(raw),
    }


def place_option(
    spec: dict,
    *,
    account_hash: str | None = None,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_delay: float = DEFAULT_POLL_DELAY,
    sleep_fn=time.sleep,
) -> dict:
    """Transmit a REAL option order, then snapshot intrinsic/extrinsic at fill.

    Flow: validate -> (kill-switch) -> place -> poll until terminal/filled ->
    capture the underlying quote -> decompose the premium -> store the ledger
    row. Returns ``{ok, mode:"LIVE", orderId, status, fill, ledger}`` on a fill,
    ``{ok: True, status:"WORKING", orderId, ...}`` when it hasn't filled yet, or
    ``{ok: False, error}`` on any failure (including the kill-switch being off).
    """
    if not SchwabProvider.configured():
        return {"ok": False, "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN)."}
    if not live_trading_enabled():
        return {
            "ok": False,
            "error": (
                "Live option trading is disabled. Set the "
                f"{_LIVE_FLAG}=true server secret to arm real order placement. "
                "Use preview to validate the order safely in the meantime."
            ),
            "liveDisabled": True,
        }
    try:
        order = schwab_orders.build_option_order(spec or {})
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": f"Invalid option spec: {e}"}

    provider = SchwabProvider()
    try:
        resolved_hash, label = _resolve_account(provider, account_hash)
        placed = provider.place_order(resolved_hash, order)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}

    order_id = placed.get("orderId")
    if not order_id:
        return {"ok": False, "error": "Schwab accepted the order but returned no order id.", "order": order}

    status, fill, _ = _poll_for_fill(provider, resolved_hash, order_id, max_polls, poll_delay, sleep_fn)
    base = {"mode": "LIVE", "orderId": order_id, "account": label, "order": order, "status": status}
    return _finalize(provider, base, order, order_id, fill)


def order_status(order_id: str, *, account_hash: str | None = None) -> dict:
    """Re-check a placed order and, if it has filled, capture its theta snapshot now.

    This is the answer to "did it fill yet?" after the place window closed — and
    it is idempotent, so polling a since-filled order repeatedly only stores the
    snapshot once. A read action (no kill-switch needed). Returns
    ``{ok, orderId, status, fill, ...}``.
    """
    if not SchwabProvider.configured():
        return {"ok": False, "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN)."}
    provider = SchwabProvider()
    try:
        resolved_hash, label = _resolve_account(provider, account_hash)
        payload = provider.get_order(resolved_hash, order_id)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}

    fill = schwab_orders.parse_fill(payload)
    base = {"orderId": str(order_id), "account": label, "status": fill.get("status") or "UNKNOWN"}
    if fill.get("fillPrice") is not None:
        return _capture_fill(provider, base, payload, str(order_id), fill)
    base["ok"] = True
    base["fill"] = None
    return base


def cancel_option(order_id: str, *, account_hash: str | None = None) -> dict:
    """Cancel a working order. Risk-reducing, so it is NOT behind the kill-switch
    — you can always pull a resting order even with live placement disabled."""
    if not SchwabProvider.configured():
        return {"ok": False, "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN)."}
    provider = SchwabProvider()
    try:
        resolved_hash, label = _resolve_account(provider, account_hash)
        provider.cancel_order(resolved_hash, order_id)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "orderId": str(order_id), "status": "CANCELED", "account": label}


def replace_option(
    order_id: str,
    spec: dict,
    *,
    account_hash: str | None = None,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_delay: float = DEFAULT_POLL_DELAY,
    sleep_fn=time.sleep,
) -> dict:
    """Re-price a working order: atomically cancel `order_id` and submit `spec`.

    This is the "work the order to get filled" path — bump the limit and replace
    rather than chase with a separate cancel + place. Schwab mints a NEW order id
    (the original is canceled); we poll the new id and snapshot on fill, exactly
    like place_option. Gated behind the kill-switch since it transmits an order.
    """
    if not SchwabProvider.configured():
        return {"ok": False, "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN)."}
    if not live_trading_enabled():
        return {
            "ok": False,
            "error": (
                "Live option trading is disabled. Set the "
                f"{_LIVE_FLAG}=true server secret to arm real order placement/replacement."
            ),
            "liveDisabled": True,
        }
    try:
        order = schwab_orders.build_option_order(spec or {})
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": f"Invalid option spec: {e}"}

    provider = SchwabProvider()
    try:
        resolved_hash, label = _resolve_account(provider, account_hash)
        replaced = provider.replace_order(resolved_hash, order_id, order)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}

    new_id = replaced.get("orderId")
    if not new_id:
        return {
            "ok": False,
            "error": "Schwab accepted the replacement but returned no new order id.",
            "replacedOrderId": str(order_id),
            "order": order,
        }

    status, fill, _ = _poll_for_fill(provider, resolved_hash, new_id, max_polls, poll_delay, sleep_fn)
    base = {
        "mode": "LIVE", "orderId": new_id, "replacedOrderId": str(order_id),
        "account": label, "order": order, "status": status,
    }
    return _finalize(provider, base, order, new_id, fill)


def close_option(
    fill_id: int,
    *,
    account_hash: str | None = None,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_delay: float = DEFAULT_POLL_DELAY,
    sleep_fn=time.sleep,
) -> dict:
    """Close an open option position by selling/buying to close.

    Takes a fill_id (the open position to close) and optionally a limit_price.
    Builds a close order (opposite direction: buy to close if originally sold,
    sell to close if originally bought), places it, polls for fill, and snapshots
    the close. Gated behind the kill-switch since it transmits an order.
    Returns ``{ok, mode:"LIVE", orderId, status, fill, ledger}`` on success.
    """
    if not SchwabProvider.configured():
        return {"ok": False, "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN)."}
    if not live_trading_enabled():
        return {
            "ok": False,
            "error": (
                "Live option trading is disabled. Set the "
                f"{_LIVE_FLAG}=true server secret to arm real order placement."
            ),
            "liveDisabled": True,
        }

    fill = db.get_option_fill(fill_id)
    if not fill:
        return {"ok": False, "error": f"No open position found with fill_id={fill_id}"}

    # Build close order: opposite direction of the opening side.
    # If originally bought, we sell to close (side="buy" -> instruction="SELL_TO_CLOSE").
    # If originally sold, we buy to close (side="sell" -> instruction="BUY_TO_CLOSE").
    opposite_side = "sell" if fill.get("side") == "buy" else "buy"
    close_spec = {
        "underlying": fill.get("underlying"),
        "expiry": fill.get("expiry"),
        "option_type": fill.get("option_type"),
        "strike": fill.get("strike"),
        "quantity": fill.get("quantity"),
        "side": opposite_side,
        "order_type": order_type,
    }
    if limit_price is not None:
        close_spec["limit_price"] = limit_price

    try:
        order = schwab_orders.build_option_close_order(close_spec)
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": f"Invalid close spec: {e}"}

    provider = SchwabProvider()
    try:
        resolved_hash, label = _resolve_account(provider, account_hash)
        placed = provider.place_order(resolved_hash, order)
    except ProviderError as e:
        return {"ok": False, "error": str(e)}

    order_id = placed.get("orderId")
    if not order_id:
        return {"ok": False, "error": "Schwab accepted the order but returned no order id.", "order": order}

    status, close_fill, _ = _poll_for_fill(provider, resolved_hash, order_id, max_polls, poll_delay, sleep_fn)
    base = {
        "mode": "LIVE", "orderId": order_id, "fillId": fill_id, "account": label,
        "order": order, "status": status, "closedPosition": fill,
    }
    return _finalize(provider, base, order, order_id, close_fill)


def batch_close_options(
    fill_ids: list[int],
    *,
    account_hash: str | None = None,
    order_type: str = "LIMIT",
    limit_price: float | None = None,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_delay: float = DEFAULT_POLL_DELAY,
    sleep_fn=time.sleep,
) -> dict:
    """Close multiple open positions simultaneously.

    Submits one close order per fill_id and collects results. Returns
    ``{ok, closed, failed, errors}``.
    """
    if not fill_ids:
        return {"ok": True, "closed": [], "failed": [], "errors": []}

    closed = []
    failed = []
    errors = []

    for fid in fill_ids:
        result = close_option(
            fid, account_hash=account_hash, order_type=order_type,
            limit_price=limit_price, max_polls=max_polls, poll_delay=poll_delay,
            sleep_fn=sleep_fn,
        )
        if result.get("ok"):
            closed.append({"fillId": fid, "orderId": result.get("orderId"), "status": result.get("status")})
        else:
            failed.append(fid)
            errors.append({"fillId": fid, "error": result.get("error")})

    return {
        "ok": True,
        "closed": closed,
        "failed": failed,
        "errors": errors if errors else None,
    }


def roll_option(
    fill_id: int,
    new_strike: float,
    new_expiry: str,
    *,
    account_hash: str | None = None,
    close_order_type: str = "MARKET",
    open_order_type: str = "LIMIT",
    open_limit_price: float | None = None,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_delay: float = DEFAULT_POLL_DELAY,
    sleep_fn=time.sleep,
) -> dict:
    """Roll a position: close current option, open a new one with different strike/expiry.

    Closes the position at market (or at a limit), then opens a new position with
    the new strike and expiry at a specified limit price. Executes as two separate
    orders (not atomic), so fills are independent. Gated behind the kill-switch.
    Returns ``{ok, closed, opened}`` on success, with details of both orders.
    """
    close_result = close_option(
        fill_id, account_hash=account_hash, order_type=close_order_type,
        max_polls=max_polls, poll_delay=poll_delay, sleep_fn=sleep_fn,
    )
    if not close_result.get("ok"):
        return {"ok": False, "error": "Failed to close position", "closeResult": close_result}

    # Get the closed position to extract details for the new open.
    fill = db.get_option_fill(fill_id)
    if not fill:
        return {
            "ok": False,
            "error": "Could not reload position after close",
            "closeResult": close_result,
        }

    # Open the new position with the same side and direction, but new strike/expiry.
    open_spec = {
        "underlying": fill.get("underlying"),
        "expiry": new_expiry,
        "option_type": fill.get("option_type"),
        "strike": new_strike,
        "quantity": fill.get("quantity"),
        "side": fill.get("side"),
        "order_type": open_order_type,
    }
    if open_limit_price is not None:
        open_spec["limit_price"] = open_limit_price

    open_result = place_option(
        open_spec, account_hash=account_hash,
        max_polls=max_polls, poll_delay=poll_delay, sleep_fn=sleep_fn,
    )
    if not open_result.get("ok"):
        return {
            "ok": False,
            "error": "Failed to open new position (close succeeded, but new open failed)",
            "closeResult": close_result,
            "openResult": open_result,
        }

    return {
        "ok": True,
        "closed": close_result,
        "opened": open_result,
    }
