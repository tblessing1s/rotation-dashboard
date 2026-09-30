"""Exports of the two History-tab record sets: the Transactions table and the raw
execution log. Pure over a loaded state dict.

* ``transactions_csv`` — what the Transactions table shows, one row per fill, oldest
  first, from the CORRECTED view (``logging_handler.derived_executions``: saved
  corrections overlaid, voided/reversed fills dropped, inherited extrinsic / cost
  basis re-derived) — the same numbers the ledgers and Payouts use.
* ``executions_csv`` / ``executions_json`` — the append-only log EXACTLY as stored:
  every record (corrections, reversals, voided fills, markers included), uncapped,
  oldest first, no overlay. JSON is the lossless form; the CSV flattens nested values
  into JSON strings.
"""
from __future__ import annotations

import csv
import io
import json

import logging_handler as log

FILL_ACTIONS = ("sell_short", "close_short", "buy_shares", "sell_shares",
                "close_shares_assigned")
_SHARE_ACTIONS = {"buy_shares", "sell_shares", "close_shares_assigned"}
_SHARE_EXIT = {"sell_shares", "close_shares_assigned"}

TRANSACTION_COLS = ["date", "ticker", "action", "strike", "qty", "expiration", "price",
                    "entry_stock_price", "stock_source", "stock_at_placement",
                    "stock_at_fill", "fill_time", "extrinsic_or_basis", "roll_group_id",
                    "source", "id"]


def _cell(v):
    """A CSV-safe cell: nested values become JSON; text that a spreadsheet would
    evaluate as a formula is prefixed so it stays text."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, sort_keys=True, default=str)
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _txn_price(e: dict):
    if e.get("action") == "close_shares_assigned":
        return e.get("strike")
    if e.get("action") in _SHARE_ACTIONS:
        return e.get("price_per_share")
    return e.get("premium_per_share") if e.get("action") == "sell_short" else e.get("close_price_per_share")


def _txn_extrinsic(e: dict):
    if e.get("action") == "sell_short":
        return e.get("entry_extrinsic_per_share")
    if e.get("action") == "close_short":
        return e.get("extrinsic_sold")
    if e.get("action") in _SHARE_EXIT:
        return e.get("cost_basis_per_share")
    return None


def transaction_rows(state: dict) -> list[dict]:
    rows = []
    for e in log.derived_executions(state):
        if e.get("action") not in FILL_ACTIONS:
            continue
        share = e.get("action") in _SHARE_ACTIONS
        rows.append({
            "date": (e.get("date") or "")[:10],
            "ticker": e.get("ticker"),
            "action": e.get("action"),
            "strike": None if share else e.get("strike"),
            "qty": e.get("qty") if share else e.get("contracts"),
            "expiration": None if share else e.get("expiration"),
            "price": _txn_price(e),
            "entry_stock_price": (e.get("price_per_share")
                                  if share and e.get("action") != "close_shares_assigned"
                                  else e.get("stock_price")),
            "stock_source": e.get("stock_price_source"),
            "stock_at_placement": e.get("stock_price_at_placement"),
            "stock_at_fill": e.get("stock_price_at_fill"),
            "fill_time": e.get("fill_time"),
            "extrinsic_or_basis": _txn_extrinsic(e),
            "roll_group_id": e.get("roll_group_id"),
            "source": e.get("source"),
            "id": e.get("id"),
        })
    rows.sort(key=lambda r: (r["date"], str(r["fill_time"] or ""), str(r["id"] or "")))
    return rows


def _write(cols: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return buf.getvalue()


def transactions_csv(state: dict) -> str:
    return _write(TRANSACTION_COLS, transaction_rows(state))


_LEAD = ["id", "date", "ticker", "action"]


def raw_executions(state: dict) -> list[dict]:
    return list(state.get("executions", []))


def executions_csv(state: dict) -> str:
    rows = raw_executions(state)
    keys = {k for r in rows for k in r}
    cols = [c for c in _LEAD if c in keys] + sorted(keys - set(_LEAD))
    return _write(cols, rows)


def executions_json(state: dict) -> str:
    return json.dumps(raw_executions(state), indent=2, sort_keys=True, default=str)
