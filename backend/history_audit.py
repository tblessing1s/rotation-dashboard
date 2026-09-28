"""Trade-history audit — the book's FILLS vs Schwab's own transactions.

Reconciliation (reconcile.py) compares what is HELD right now; ingestion
(transaction_ingest.py) surfaces broker fills the book has not booked. Neither
catches the reverse, and neither checks prices. Every live incident that let a
book drift was one of these, and each would have shown up here the next
morning:

  * PHANTOM — a fill the book holds that Schwab never made. CONFIRMED LIVE: a
    roll Schwab CANCELED was booked as filled at $0/$0 (SPCX order
    1008063520870), and one book carried another account's whole SPCX history.
  * MISSING — a fill Schwab made that the book does not hold (a trade placed at
    the broker, or a fill the order poll never committed). CONFIRMED LIVE:
    a book was missing its own 9/1-9/16 SPCX fills.
  * PRICE / QUANTITY — a fill booked at a price (or size) other than Schwab's.

Findings carry NO authority over trading — this is a report plus an alert. A
finding the operator has explained (e.g. history from before this book used
the app) can be acknowledged with a typed reason; it then stops counting.

The core (``audit``) is PURE over (feed, state, window). ``run_history_audit``
fetches, audits and persists ``state["history_audit"]``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
import logging_handler as log
import reconcile
import transaction_ingest as ingest

logger = log.logger

PHANTOM = "PHANTOM_IN_BOOK"
MISSING = "MISSING_IN_BOOK"
PRICE = "PRICE_MISMATCH"
QUANTITY = "QUANTITY_MISMATCH"

# Executions that are broker FILLS (and so must appear in Schwab's TRADE feed).
_AUDITED = ("sell_short", "close_short", "buy_shares", "sell_shares",
            "buy_leap", "close_leap")
PRICE_TOLERANCE = 0.01      # per share / per contract-share, like fill_verify
DATE_SLACK_DAYS = 3         # booked date vs Schwab's trade date (key match only)
FETCH_BUFFER_DAYS = 3       # fetch a little wider than audited, so an edge fill matches


def _day(v) -> str:
    return str(v or "")[:10]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _same_strike(a, b) -> bool:
    fa, fb = _f(a), _f(b)
    if fa is None and fb is None:
        return True
    return fa is not None and fb is not None and abs(fa - fb) < 0.005


def _book_price(e: dict):
    """Per-share price a booked fill carries (LEAP executions store per contract)."""
    a = e.get("action")
    if a == "sell_short":
        return _f(e.get("premium_per_share"))
    if a == "close_short":
        return _f(e.get("close_price_per_share"))
    if a in ("buy_shares", "sell_shares"):
        return _f(e.get("price_per_share"))
    px = _f(e.get("execution_price"))
    return round(px / 100.0, 4) if px is not None else None


def _receipt_orders(state: dict) -> dict:
    """execution id -> Schwab order id, from the fill receipts."""
    out = {}
    for r in state.get("order_receipts") or []:
        for eid in r.get("execution_ids") or []:
            if r.get("order_id") is not None:
                out[str(eid)] = str(r["order_id"])
    return out


def book_fills(state: dict, since: str, until: str) -> list[dict]:
    """The book's live broker fills dated in [since, until): the corrected view
    (voided / reversed executions excluded, History corrections overlaid).
    Paper bookings never reach the broker and assignments are not TRADE rows,
    so neither is audited."""
    by_receipt = _receipt_orders(state)
    out = []
    for e in log.derived_executions(state):
        if e.get("action") not in _AUDITED:
            continue
        if e.get("live_transmitted") is False or e.get("mode") == "logged":
            continue
        if e.get("assigned"):
            continue
        d = _day(e.get("date"))
        if not d or d < since or d >= until:
            continue
        shares = e["action"] in ("buy_shares", "sell_shares")
        out.append({
            "execution_id": str(e.get("id")),
            "ticker": (e.get("ticker") or "").upper(),
            "action": e["action"],
            "strike": None if shares else e.get("strike"),
            "expiration": None if shares else (_day(e.get("expiration")) or None),
            "qty": int(round(_f(e.get("qty") if shares else e.get("contracts")) or 0)),
            "price": _book_price(e),
            "date": d,
            "transaction_id": str(e["transaction_id"]) if e.get("transaction_id") else None,
            "order_id": (str(e.get("broker_order_id")) if e.get("broker_order_id")
                         else by_receipt.get(str(e.get("id")))),
        })
    return out


def broker_fills(feed: list, since: str, until: str) -> tuple[list[dict], list[str]]:
    """Schwab's TRADE legs dated in [since, until), one row per (order, leg
    instrument): partial fills of one order are summed (qty-weighted price) so
    a single booked leg matches them."""
    records, errors = ingest.parse_feed(feed)
    rows: dict[tuple, dict] = {}
    for rec in records:
        d = _day(rec.get("time"))
        if not d or d < since or d >= until:
            continue
        for leg in rec["legs"]:
            action = ingest._leg_action(leg)
            if action is None:
                continue
            if leg.get("asset_type") == "OPTION" and leg.get("put_call") == reconcile.PUT:
                continue  # puts are booked by the CSP lifecycle, not audited here
            qty = abs(_f(leg.get("amount")) or 0)
            if qty <= 0:
                continue
            opt = leg.get("asset_type") == "OPTION"
            key = (rec.get("order_id") or f"txn:{rec['transaction_id']}",
                   (leg.get("underlying") or "").upper(), action,
                   round(_f(leg.get("strike")) or 0, 2) if opt else None,
                   _day(leg.get("expiry")) or None if opt else None)
            row = rows.get(key)
            if row is None:
                row = rows[key] = {
                    "order_id": rec.get("order_id"), "transaction_ids": [],
                    "ticker": key[1], "action": action,
                    "strike": _f(leg.get("strike")) if opt else None,
                    "expiration": key[4], "qty": 0.0, "_notional": 0.0,
                    "_priced": 0.0, "date": d,
                }
            row["transaction_ids"].append(str(rec["transaction_id"]))
            row["qty"] += qty
            px = _f(leg.get("price"))
            if px is not None:
                row["_notional"] += px * qty
                row["_priced"] += qty
            row["date"] = min(row["date"], d)
    out = []
    for row in rows.values():
        row["price"] = round(row["_notional"] / row["_priced"], 4) if row["_priced"] else None
        row["qty"] = int(round(row["qty"]))
        del row["_notional"], row["_priced"]
        out.append(row)
    return out, errors


def _compatible(b: dict, s: dict) -> bool:
    if b["ticker"] != s["ticker"] or b["action"] != s["action"]:
        return False
    if not _same_strike(b["strike"], s["strike"]):
        return False
    # The book often never recorded an option's expiry; only a known one must agree.
    if b["expiration"] and s["expiration"] and b["expiration"] != s["expiration"]:
        return False
    return True


def _days_apart(a: str, b: str) -> int:
    try:
        return abs((datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days)
    except ValueError:
        return 10 ** 6


def match(book: list[dict], broker: list[dict]) -> tuple[list[tuple], list[dict], list[dict]]:
    """Pair booked fills with Schwab fills. Strongest evidence first:
    1) a Schwab transaction id the execution was booked from, 2) the Schwab
    order id, 3) same ticker/action/strike(/expiry)/qty within a few days.
    Returns (pairs, unmatched_book, unmatched_broker)."""
    free = list(broker)
    pairs: list[tuple] = []
    rest: list[dict] = []

    def take(pred):
        for s in free:
            if pred(s):
                free.remove(s)
                return s
        return None

    for b in book:
        s = None
        if b["transaction_id"]:
            s = take(lambda s: b["transaction_id"] in s["transaction_ids"])
        if s is None and b["order_id"]:
            s = take(lambda s: s["order_id"] == b["order_id"] and _compatible(b, s))
        if s is None:
            rest.append(b)
        else:
            pairs.append((b, s))

    unmatched = []
    for b in rest:
        cands = [s for s in free if _compatible(b, s) and s["qty"] == b["qty"]
                 and _days_apart(b["date"], s["date"]) <= DATE_SLACK_DAYS]
        if not cands:
            unmatched.append(b)
            continue
        s = min(cands, key=lambda s: _days_apart(b["date"], s["date"]))
        free.remove(s)
        pairs.append((b, s))
    return pairs, unmatched, free


def _describe(f: dict) -> str:
    what = f["action"].replace("_", " ")
    if f.get("strike") is not None:
        what += f" {f['strike']:g}C" if isinstance(f["strike"], (int, float)) else f" {f['strike']}C"
    if f.get("expiration"):
        what += f" exp {f['expiration']}"
    return f"{f['ticker']} {what} x{f['qty']} on {f['date']}"


def audit(feed: list, state: dict, since: str, until: str,
          as_of: str | None = None) -> dict:
    """PURE: compare the book's fills in [since, until) with Schwab's."""
    as_of = as_of or log.utcnow()
    book = book_fills(state, since, until)
    broker, errors = broker_fills(feed, since, until)
    pairs, phantom, missing = match(book, broker)

    findings: list[dict] = []
    for b in phantom:
        findings.append({
            "id": f"{PHANTOM}:{b['execution_id']}", "kind": PHANTOM,
            "ticker": b["ticker"], "execution_id": b["execution_id"],
            "order_id": b["order_id"], "book": b,
            "summary": (f"{_describe(b)} is in the book ({b['execution_id']}) but Schwab "
                        "shows no such fill — a booked trade that never happened, or "
                        "another account's trade."),
        })
    for s in missing:
        findings.append({
            "id": f"{MISSING}:{s['order_id'] or s['transaction_ids'][0]}:{s['action']}:{s['strike']}",
            "kind": MISSING, "ticker": s["ticker"], "order_id": s["order_id"],
            "broker": s,
            "summary": (f"Schwab filled {_describe(s)} @ {s['price']} (order "
                        f"{s['order_id'] or 'n/a'}) but the book has no such fill — adopt it "
                        "from Broker execution ingestion."),
        })
    for b, s in pairs:
        if b["qty"] != s["qty"]:
            findings.append({
                "id": f"{QUANTITY}:{b['execution_id']}", "kind": QUANTITY,
                "ticker": b["ticker"], "execution_id": b["execution_id"],
                "order_id": s["order_id"], "book": b, "broker": s,
                "summary": (f"{_describe(b)}: the book has {b['qty']}, Schwab filled "
                            f"{s['qty']} (order {s['order_id'] or 'n/a'})."),
            })
        elif (b["price"] is not None and s["price"] is not None
              and abs(b["price"] - s["price"]) > PRICE_TOLERANCE):
            findings.append({
                "id": f"{PRICE}:{b['execution_id']}", "kind": PRICE,
                "ticker": b["ticker"], "execution_id": b["execution_id"],
                "order_id": s["order_id"], "book": b, "broker": s,
                "summary": (f"{_describe(b)} booked @ {b['price']:g}, Schwab filled @ "
                            f"{s['price']:g} (order {s['order_id'] or 'n/a'}) — correct the "
                            "price in History."),
            })

    acks = ((state.get("history_audit") or {}).get("acks") or {})
    for f in findings:
        if f["id"] in acks:
            f["ack"] = acks[f["id"]]
    open_n = sum(1 for f in findings if not f.get("ack"))
    counts: dict[str, int] = {}
    for f in findings:
        if not f.get("ack"):
            counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {
        "as_of": as_of, "since": since, "until": until,
        "status": "CLEAN" if open_n == 0 else "DIRTY", "broker_ok": True,
        "book_fills": len(book), "broker_fills": len(broker), "matched": len(pairs),
        "counts": counts, "open": open_n, "findings": findings, "errors": errors,
    }


def _window(lookback_days: int | None, now: datetime | None = None) -> tuple[str, str]:
    """Audit [today - lookback, today). Today is left out: a fill from the last
    few hours may not be in Schwab's transaction feed yet."""
    now = now or datetime.now(timezone.utc)
    days = int(lookback_days or config.HISTORY_AUDIT_LOOKBACK_DAYS)
    days = max(1, min(days, ingest.INGESTION_MAX_LOOKBACK_DAYS - FETCH_BUFFER_DAYS))
    until = now.strftime("%Y-%m-%d")
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    return since, until, days


def run_history_audit(persist: bool = True, feed: list | None = None,
                      lookback_days: int | None = None) -> dict:
    """Fetch this book's Schwab transactions and audit its fills against them.
    A fetch failure yields a FAILED report and never overwrites the last good
    verdict with an empty one (an empty feed would read as "everything phantom")."""
    since, until, days = _window(lookback_days)
    as_of = log.utcnow()
    if feed is None:
        try:
            feed = ingest.fetch_transactions(days + FETCH_BUFFER_DAYS)
        except Exception as e:  # noqa: BLE001 — a failed fetch is reported, not fatal
            logger.warning("history audit fetch failed: %s", e)
            report = {"as_of": as_of, "since": since, "until": until, "status": "FAILED",
                      "broker_ok": False, "error": str(e), "findings": [], "open": 0,
                      "counts": {}}
            if persist:
                log.mutate_state(lambda s: s.setdefault("history_audit", {}).update(
                    {"last_failed": report}))
            return report

    def _apply(state: dict) -> dict:
        rep = audit(feed, state, since, until, as_of)
        if persist:
            ha = state.setdefault("history_audit", {})
            ha["last"] = rep
            ha.pop("last_failed", None)
        return rep

    if persist:
        return log.mutate_state(_apply)
    return _apply(log.load_state())


def last_report(state: dict) -> dict | None:
    return (state.get("history_audit") or {}).get("last")


def acknowledge(finding_id: str, reason: str) -> dict:
    """Mark one finding explained (typed reason, logged). It stays listed but
    no longer counts toward DIRTY or the alert."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("acknowledging a finding requires a typed reason")

    def _ack(state: dict) -> dict:
        ha = state.setdefault("history_audit", {})
        rec = {"reason": reason, "at": log.utcnow()}
        ha.setdefault("acks", {})[str(finding_id)] = rec
        last = ha.get("last")
        if last:
            for f in last.get("findings") or []:
                if f["id"] == finding_id:
                    f["ack"] = rec
            last["open"] = sum(1 for f in last.get("findings") or [] if not f.get("ack"))
            counts: dict[str, int] = {}
            for f in last.get("findings") or []:
                if not f.get("ack"):
                    counts[f["kind"]] = counts.get(f["kind"], 0) + 1
            last["counts"] = counts
            last["status"] = "CLEAN" if last["open"] == 0 else "DIRTY"
        return rec
    return log.mutate_state(_ack)


def open_findings(state: dict) -> list[dict]:
    rep = last_report(state) or {}
    if not rep.get("broker_ok"):
        return []
    return [f for f in rep.get("findings") or [] if not f.get("ack")]
