"""Pull live positions and trade history straight from a Schwab brokerage
account, so positions no longer have to be hand-imported via CSV.

Unlike market-data ingestion (which writes to the datastore on a schedule),
this is contacted on-demand when the user taps "Sync from Schwab" on the
Positions tab — it returns a current-holdings snapshot plus trade activity
normalized into the exact row shape the frontend's Schwab-only transaction
ledger already consumes (date / symbol / action / leg / qty / price / amount /
flowType …), so synced fills merge and de-duplicate without requiring CSV data.

Requires the Schwab app to be approved for "Accounts and Trading Production"
(a separate product from the market-data feed); without it the account calls
return HTTP 401/403 and that reason is surfaced to the UI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from providers.base import ProviderError
from providers.schwab import SchwabProvider

# Schwab caps the transactions window at one year per request.
MAX_SYNC_DAYS = 365


def available() -> bool:
    return SchwabProvider.configured()


def _iso(dt: datetime) -> str:
    # Schwab wants ISO-8601 with milliseconds and a Z suffix.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _mask(account_number: str | None) -> str:
    s = str(account_number or "")
    return f"****{s[-4:]}" if len(s) >= 4 else (s or "account")


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# -- normalization -----------------------------------------------------------
def normalize_account(account: dict) -> dict:
    """Schwab account payload -> a compact snapshot of holdings + balances."""
    sec = account.get("securitiesAccount", account) or {}
    balances = sec.get("currentBalances") or {}
    positions = []
    for pos in sec.get("positions", []) or []:
        instrument = pos.get("instrument") or {}
        symbol = instrument.get("symbol") or instrument.get("underlyingSymbol")
        if not symbol:
            continue
        long_qty = _num(pos.get("longQuantity"), 0) or 0
        short_qty = _num(pos.get("shortQuantity"), 0) or 0
        positions.append({
            "symbol": str(symbol).upper(),
            "assetType": instrument.get("assetType"),
            "description": instrument.get("description"),
            "longQty": long_qty,
            "shortQty": short_qty,
            "netQty": long_qty - short_qty,
            "averagePrice": _num(pos.get("averagePrice")),
            "marketValue": _num(pos.get("marketValue")),
            "dayPL": _num(pos.get("currentDayProfitLoss")),
            "openPL": _num(pos.get("longOpenProfitLoss")) or _num(pos.get("shortOpenProfitLoss")),
        })
    positions.sort(key=lambda p: p["symbol"])
    return {
        "account": _mask(sec.get("accountNumber")),
        "type": sec.get("type"),
        "liquidationValue": _num(balances.get("liquidationValue")),
        "cashBalance": _num(balances.get("cashBalance")),
        "buyingPower": _num(balances.get("buyingPower")),
        "positions": positions,
    }


def normalize_trade(txn: dict) -> list[dict]:
    """One Schwab TRADE transaction -> one ledger row per traded leg.

    Fee/commission and cash legs are skipped. Sign and open/close are derived
    from each leg's signed quantity and positionEffect so equity buys/sells and
    multi-leg option fills net consistently in the frontend ledger.
    """
    when = txn.get("tradeDate") or txn.get("time") or ""
    date = str(when)[:10]
    order_id = str(txn.get("orderId") or txn.get("activityId") or "")
    description = txn.get("description") or ""
    rows = []
    for item in txn.get("transferItems", []) or []:
        instrument = item.get("instrument") or {}
        symbol = instrument.get("symbol") or instrument.get("underlyingSymbol")
        amount = _num(item.get("amount"))
        asset_type = (instrument.get("assetType") or "").upper()
        # Skip fee rows (no instrument / zero qty) and cash settlement legs.
        if not symbol or not amount or asset_type in ("CURRENCY", "CASH_EQUIVALENT"):
            continue
        bought = amount > 0
        qty = abs(amount)
        price = _num(item.get("price")) or 0
        cost = _num(item.get("cost"))
        # Cash flow: buys are a debit (negative), sells a credit (positive).
        # Prefer Schwab's signed cost; fall back to price * signed qty.
        cash = cost if cost is not None else (-(amount * price) if price else 0)
        effect = (item.get("positionEffect") or "").upper()
        if effect == "OPENING":
            flow, leg = "open", ("long" if bought else "short")
        elif effect == "CLOSING":
            flow, leg = "close", ("short" if bought else "long")
        else:
            # No explicit effect (e.g. plain equity fill): treat a buy as
            # opening/long and a sell as closing/long so it nets out.
            flow, leg = ("open", "long") if bought else ("close", "long")
        action = ("BUY" if bought else "SELL") + (" TO OPEN" if flow == "open" else " TO CLOSE")
        rows.append({
            "date": date,
            "symbol": str(symbol).upper(),
            "positionId": order_id,
            "strategy": "SCHWAB",
            "source": "schwab",
            "action": action,
            "flowType": flow,
            "leg": leg,
            "qty": qty,
            "price": price,
            "amount": round(cash, 2) if cash is not None else 0,
            "note": description,
        })
    return rows


# -- orchestration -----------------------------------------------------------
def sync(days: int = MAX_SYNC_DAYS, end: datetime | None = None) -> dict:
    """Fetch the current-holdings snapshot and recent trade history.

    Returns {configured, accounts[], transactions[], errors{}, asOf, range}.
    Partial failures (e.g. positions ok but one account's transactions 403) are
    reported per-source in `errors` rather than failing the whole sync.
    """
    if not SchwabProvider.configured():
        return {
            "configured": False,
            "error": "Schwab credentials are not set (SCHWAB_APP_KEY / SECRET / REFRESH_TOKEN).",
            "accounts": [],
            "transactions": [],
        }

    days = max(1, min(int(days), MAX_SYNC_DAYS))
    end_dt = end or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    provider = SchwabProvider()
    errors: dict[str, str] = {}

    try:
        numbers = provider.account_numbers()
    except ProviderError as e:
        db.kv_set("schwab_account_error", {"at": db.utcnow(), "error": str(e)})
        return {"configured": True, "error": str(e), "accounts": [], "transactions": []}

    accounts: list[dict] = []
    try:
        accounts = [normalize_account(a) for a in provider.get_accounts(positions=True)]
    except ProviderError as e:
        errors["positions"] = str(e)

    transactions: list[dict] = []
    for entry in numbers:
        account_hash = entry.get("hashValue")
        label = _mask(entry.get("accountNumber"))
        if not account_hash:
            continue
        try:
            for txn in provider.get_transactions(account_hash, _iso(start_dt), _iso(end_dt)):
                if (txn.get("type") or "").upper() == "TRADE":
                    transactions.extend(normalize_trade(txn))
        except ProviderError as e:
            errors[f"transactions:{label}"] = str(e)

    transactions.sort(key=lambda r: (r.get("date", ""), r.get("symbol", "")))
    db.kv_set("schwab_account_error", None if not errors else {"at": db.utcnow(), "errors": errors})

    return {
        "configured": True,
        "accounts": accounts,
        "transactions": transactions,
        "errors": errors,
        "asOf": db.utcnow(),
        "range": {"start": start_dt.date().isoformat(), "end": end_dt.date().isoformat(), "days": days},
    }
