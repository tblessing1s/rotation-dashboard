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

    # Poll until the order resolves or we observe a fill price.
    status, fill = "WORKING", None
    for attempt in range(max(1, int(max_polls))):
        try:
            order_status = provider.get_order(resolved_hash, order_id)
        except ProviderError:
            order_status = {}
        fill = schwab_orders.parse_fill(order_status)
        status = fill.get("status") or status
        if fill.get("fillPrice") is not None or status in schwab_orders.TERMINAL_ORDER_STATES:
            break
        if attempt < max_polls - 1:
            sleep_fn(poll_delay)

    base = {"mode": "LIVE", "orderId": order_id, "account": label, "order": order, "status": status}

    fill_price = fill.get("fillPrice") if fill else None
    if fill_price is None:
        # Placed, but no fill observed in the poll window (e.g. a resting limit).
        # Nothing to decompose yet; report it so the UI can show a working order.
        base["ok"] = True
        base["fill"] = None
        base["note"] = "Order placed but not filled within the poll window; no theta snapshot taken."
        return base

    # Filled — capture the underlying price right now and split the premium.
    underlying = str(spec["underlying"]).upper()
    try:
        quote = provider.get_quote(underlying)
    except ProviderError as e:
        base["ok"] = True
        base["fill"] = {"fillPrice": fill_price, "filledQuantity": fill.get("filledQuantity")}
        base["warning"] = f"Filled at {fill_price}, but the stock quote failed ({e}); theta snapshot skipped."
        return base

    stock_price = _stock_price_from_quote(quote)
    if stock_price is None:
        base["ok"] = True
        base["fill"] = {"fillPrice": fill_price, "filledQuantity": fill.get("filledQuantity")}
        base["warning"] = "Filled, but the stock quote had no usable price; theta snapshot skipped."
        return base

    split = decompose(spec["option_type"], spec["strike"], stock_price, fill_price)
    osi = order["orderLegCollection"][0]["instrument"]["symbol"]
    fill_record = {
        "order_id": order_id,
        "underlying": underlying,
        "osi_symbol": osi,
        "option_type": split["type"],
        "strike": float(spec["strike"]),
        "expiry": str(spec["expiry"])[:10],
        "side": str(spec.get("side") or "buy").lower(),
        "quantity": int(fill.get("filledQuantity") or spec["quantity"]),
        "premium": fill_price,
        "stock_price": stock_price,
        "intrinsic": split["intrinsic"],
        "extrinsic": split["extrinsic"],
        "quote_time": quote.get("quoteTimeMs"),
        "filled_at": db.utcnow(),
    }
    fill_record["split"] = split
    ledger_row = db.record_option_fill(fill_record)

    base["ok"] = True
    base["fill"] = fill_record
    base["split"] = split
    base["ledger"] = ledger_row
    return base
