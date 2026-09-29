"""Where the money is made and lost: underlying (shares) vs short covered calls.

Read-only, per ticker and book-wide. Four buckets, all cash P/L in dollars:

* shares realized   — ``realized_pnl`` on sell_shares / close_shares_assigned
                      (cost basis is the history-derived one).
* shares unrealized — (spot - cost basis) x shares still held.
* calls realized    — premium sold minus cash paid to buy back. An assignment or
                      expiry buys back for nothing here: the intrinsic the call
                      gave away is already inside the shares' sale at the strike.
* calls unrealized  — open shorts: premium collected minus the current mark.
* dividends         — held-share cash dividends (their own income event).

Each row also carries the ``story`` inputs behind those buckets (cost in/out of
the shares, premium split into intrinsic/extrinsic, buybacks, open-call mark) so
the UI can walk from original cost to position P/L.

``uncovered_gap`` (from ``coverage_gaps``) is an "of which" line INSIDE the
shares P/L — the stock move while owned lots had no call — never added on top.
No authority: nothing here feeds a gate or verdict.
"""
from __future__ import annotations

import coverage_gaps
import logging_handler as log

_KEYS = ("shares_realized", "shares_unrealized", "calls_realized",
         "calls_unrealized", "dividends")
# Story inputs: additive dollar figures that explain the buckets above.
_STORY = ("shares_bought_cost", "shares_sold_cost", "shares_sale_proceeds",
          "shares_held_cost", "shares_held_value", "premium_sold",
          "premium_extrinsic", "buyback_paid", "buyback_extrinsic",
          "open_call_mark", "open_call_extrinsic")


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _blank(ticker: str) -> dict:
    return {"ticker": ticker, "open": False, "spot": None,
            **{k: 0.0 for k in _KEYS}, **{k: 0.0 for k in _STORY},
            "uncovered_gap": 0.0,
            "uncovered_gap_open": False}


def _finish(row: dict) -> dict:
    for k in _KEYS + _STORY + ("uncovered_gap",):
        row[k] = round(row[k], 2)
    row["buyback_intrinsic"] = round(row["buyback_paid"] - row["buyback_extrinsic"], 2)
    row["open_call_intrinsic"] = round(row["open_call_mark"] - row["open_call_extrinsic"], 2)
    row["premium_intrinsic"] = round(row["premium_sold"] - row["premium_extrinsic"], 2)
    row["shares_total"] = round(row["shares_realized"] + row["shares_unrealized"], 2)
    row["calls_total"] = round(row["calls_realized"] + row["calls_unrealized"], 2)
    row["realized"] = round(row["shares_realized"] + row["calls_realized"]
                            + row["dividends"], 2)
    row["unrealized"] = round(row["shares_unrealized"] + row["calls_unrealized"], 2)
    row["total"] = round(row["shares_total"] + row["calls_total"] + row["dividends"], 2)
    # Shares P/L with the uncovered stretches taken out: what the stock did
    # while it was actually wearing a call.
    row["shares_while_covered"] = round(row["shares_total"] - row["uncovered_gap"], 2)
    return row


def build(state: dict, position_views: list[dict] | None = None) -> dict:
    """``position_views`` = ``position_manager.positions_view(state)`` (spot and
    live short marks). Without them the unrealized buckets stay 0."""
    rows: dict[str, dict] = {}

    def row(t: str) -> dict:
        t = (t or "").upper()
        return rows.setdefault(t, _blank(t))

    for e in log.derived_executions(state):
        t = e.get("ticker")
        if not t:
            continue
        a = e.get("action")
        if a == "buy_shares":
            row(t)["shares_bought_cost"] += (
                _f(e.get("execution_total"))
                or _f(e.get("qty")) * _f(e.get("price_per_share")))
        elif a in ("sell_shares", "close_shares_assigned"):
            r = row(t)
            r["shares_realized"] += _f(e.get("realized_pnl"))
            r["shares_sold_cost"] += _f(e.get("cost_basis_per_share")) * _f(e.get("qty"))
            r["shares_sale_proceeds"] += (
                _f(e.get("execution_total")) or _f(e.get("proceeds")))
        elif a == "sell_short":
            r = row(t)
            prem = _f(e.get("premium_total"))
            r["calls_realized"] += prem
            r["premium_sold"] += prem
            # Extrinsic captured at the sale; the rest of the premium was intrinsic.
            r["premium_extrinsic"] += (_f(e.get("entry_extrinsic_per_share"))
                                       * _f(e.get("contracts")) * 100)
        elif a == "close_short":
            reason = e.get("reason") or e.get("exit_reason")
            free = e.get("assigned") or reason == "expired_worthless"
            paid = 0.0 if free else _f(e.get("close_total"))
            r = row(t)
            r["calls_realized"] -= paid
            r["buyback_paid"] += paid
            if paid:
                # Time value bought back; the remainder of the debit is intrinsic.
                _, ext_ps, _ = log.close_economics(e)
                r["buyback_extrinsic"] += min(
                    max(ext_ps, 0.0) * _f(e.get("contracts")) * 100, paid)
        elif a == "dividend_income":
            row(t)["dividends"] += _f(e.get("amount"))

    for v in position_views or []:
        if v.get("status") == "closed":
            continue
        r = row(v.get("ticker"))
        spot = v.get("stock_price") or v.get("price")
        sh = v.get("shares") or {}
        count, basis = _f(sh.get("count")), _f(sh.get("cost_basis_per_share"))
        r["open"] = bool(count or v.get("short_calls"))
        r["spot"] = spot
        r["shares_held_cost"] += basis * count
        if spot and count:
            r["shares_held_value"] += float(spot) * count
            r["shares_unrealized"] += (float(spot) - basis) * count
        for sc in v.get("short_calls") or []:
            n = _f(sc.get("contracts"))
            mark = sc.get("current_bid")
            if mark is None or not n:
                continue
            # The sell_short above already banked the premium as realized; the
            # open leg's unrealized side is the cost to close it, negative.
            r["calls_unrealized"] -= float(mark) * n * 100
            r["open_call_mark"] += float(mark) * n * 100
            strike = sc.get("strike")
            if spot and strike is not None:
                intr = max(float(spot) - float(strike), 0.0) * n * 100
            else:
                intr = 0.0
            r["open_call_extrinsic"] += max(float(mark) * n * 100 - intr, 0.0)

    # A short still open has had its premium counted in calls_realized; net it
    # out so the premium sits in unrealized (premium - mark) instead.
    for v in position_views or []:
        if v.get("status") == "closed":
            continue
        r = row(v.get("ticker"))
        for sc in v.get("short_calls") or []:
            if sc.get("current_bid") is None:
                continue
            prem = _f(sc.get("entry_premium_total"))
            r["calls_realized"] -= prem
            r["calls_unrealized"] += prem

    for t, r in rows.items():
        spot = r["spot"]
        view = next((v for v in position_views or []
                     if (v.get("ticker") or "").upper() == t
                     and v.get("status") != "closed"), None)
        g = coverage_gaps.for_ticker(state, t, live_price=spot)["summary"]
        r["uncovered_gap"] = _f(g.get("gap_pnl"))
        r["uncovered_gap_open"] = bool(g.get("open"))
        r["uncovered_count"] = g.get("count", 0)
        r["uncovered_by_kind"] = g.get("by_kind", {})
        _finish(r)
        r["entry_date"] = (view or {}).get("entry_date")

    tickers = sorted(rows.values(), key=lambda r: r["total"])  # worst first
    tot = _blank("ALL")
    for r in tickers:
        for k in _KEYS + _STORY + ("uncovered_gap",):
            tot[k] += r[k]
    return {"authority": "none", "totals": _finish(tot), "tickers": tickers}
