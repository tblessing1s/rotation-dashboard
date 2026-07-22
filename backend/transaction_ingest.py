"""Execution ingestion from the Schwab transactions endpoint (spec §4).

Broker transaction records are the ONLY source for ingested execution economics
(``INGESTION_IS_GROUND_TRUTH``): fills, prices, fees, and timestamps are copied
verbatim from the broker, never hand-entered or synthesized. This module turns a
Schwab transactions feed into a structured ingestion report:

  * **Dedupe by Schwab transaction id** — persisted in ``state["ingested_transactions"]``
    so re-running reconciliation is always safe and idempotent.
  * **Matching** — each broker execution is matched to an app order by Schwab
    ``orderId`` when possible. A matched execution CONFIRMS the app's own record
    (source ``app``); the app already booked it at fill time, so ingestion records
    the transaction→order linkage rather than double-booking.
  * **Out-of-band detection** — a broker execution with no matching app order
    (e.g. the incident's manual ToS roll) is surfaced as a PROPOSED adoption
    tagged ``source: broker_manual``, with every economic field taken from the
    broker record. Per ``NO_AUTO_REMEDIATION`` the app never auto-applies it; the
    operator adopts it with one click (executor.adopt_broker_trade), which appends
    the execution through the same tested builders the app uses. Multi-leg
    out-of-band orders sharing one ``orderId`` are linked into a single logical
    action (so a manual roll ingests as a roll, not two unrelated trades).

The core (``build_report``) is a pure function over a parsed feed + the current
state, mirroring reconcile.py's offline-testable pattern. The thin wrapper
(``run_ingestion``) pulls the live feed, isolates fetch failures, and persists the
dedupe ledger + the surfaced proposals. No derived value is ever patched directly:
adoption appends executions and the normal recompute rebuilds ledgers/positions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
import reconcile

logger = logging.getLogger("cfm.alerts")

# ---- Sources (execution provenance) ----------------------------------------
SOURCE_APP = "app"                     # broker fill matched an app-submitted order
SOURCE_BROKER_MANUAL = "broker_manual"  # out-of-band trade with no app order

# ---- Logical actions inferred from a group's legs --------------------------
ACT_ROLL = "roll_short"
ACT_SELL_SHORT = "sell_short"
ACT_CLOSE_SHORT = "close_short"
ACT_BUY_LEAP = "buy_leap"
ACT_CLOSE_LEAP = "close_leap"
ACT_UNKNOWN = "unknown"

# Schwab instruction -> position effect we care about. LIVE_VERIFY: confirm the
# exact instruction strings Schwab echoes on option TRADE transferItems.
_OPENING = {"BUY_TO_OPEN", "SELL_TO_OPEN"}
_CLOSING = {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Parse — one Schwab transaction -> a normalized record (or an error string)
# ---------------------------------------------------------------------------
def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _leg_from_transfer_item(item: dict) -> dict:
    """Normalize one Schwab ``transferItem`` into a canonical execution leg.

    LIVE_VERIFY: field names (``instrument.symbol``/``assetType``/``putCall``/
    ``underlyingSymbol``/``strikePrice``/``expirationDate``, ``amount``, ``price``,
    ``cost``, ``positionEffect``, ``feeType``) are assumptions; a missing/renamed
    field degrades to None and the group is flagged for review, never guessed.
    """
    inst = item.get("instrument") or {}
    asset = (inst.get("assetType") or "").upper()
    symbol = (inst.get("symbol") or "").strip()
    amount = _num(item.get("amount"), 0.0) or 0.0           # signed contract/share count
    price = _num(item.get("price"))                          # per-share/contract fill price
    cost = _num(item.get("cost"))                            # signed net cash for the leg
    pos_effect = (item.get("positionEffect") or "").upper()  # OPENING / CLOSING
    fee_type = (item.get("feeType") or "").upper()

    leg = {
        "symbol": symbol,
        "asset_type": asset,
        "amount": amount,
        "price": price,
        "cost": cost,
        "position_effect": pos_effect,
        "fee_type": fee_type or None,
        "underlying": (inst.get("underlyingSymbol") or "").upper() or None,
        "put_call": None,
        "strike": _num(inst.get("strikePrice")),
        "expiry": None,
    }
    if asset == "OPTION" and symbol:
        # Prefer the OCC symbol (authoritative) over the loose instrument fields.
        try:
            parsed = reconcile.parse_option_symbol(symbol)
            leg["underlying"] = parsed["underlying"]
            leg["put_call"] = parsed["put_call"]
            leg["strike"] = parsed["strike"]
            leg["expiry"] = parsed["expiry"]
        except reconcile.OptionSymbolParseError:
            # Fall back to the instrument's own fields; if those are absent too the
            # group is flagged (a broker option we can't understand is never
            # silently ingested).
            pc = (inst.get("putCall") or "").upper()
            leg["put_call"] = reconcile.CALL if pc == "CALL" else reconcile.PUT if pc == "PUT" else None
            exp = inst.get("expirationDate")
            leg["expiry"] = str(exp)[:10] if exp else None
    return leg


def parse_transaction(txn: dict) -> tuple[dict | None, str | None]:
    """Normalize one Schwab transaction. Returns ``(record, None)`` on success or
    ``(None, reason)`` when it is not an ingestable trade / cannot be understood.

    A record is ``{transaction_id, order_id, time, type, net_amount, fees,
    legs: [...]}``. LIVE_VERIFY: ``activityId`` as the stable transaction id and
    ``orderId`` as the app-order link are the two load-bearing assumptions.
    """
    if not isinstance(txn, dict):
        return None, "transaction is not an object"
    ttype = (txn.get("type") or "").upper()
    if ttype and ttype != "TRADE":
        # Non-trade activity (dividends, transfers, fees) — not an execution.
        return None, None
    txn_id = txn.get("activityId") or txn.get("transactionId") or txn.get("id")
    if txn_id is None:
        return None, "transaction has no activityId/transactionId (cannot dedupe)"
    order_id = txn.get("orderId")
    items = txn.get("transferItems") or txn.get("transactionItems") or []
    legs = [_leg_from_transfer_item(it) for it in items
            if (it.get("instrument") or {}).get("assetType", "").upper() in ("OPTION", "EQUITY")]
    if not legs:
        return None, None  # a TRADE with no option/equity leg (e.g. a pure fee row)
    fees = sum(_num(it.get("cost"), 0.0) or 0.0 for it in items
               if (it.get("feeType") or "").upper() and not (it.get("instrument") or {}).get("assetType"))
    return {
        "transaction_id": str(txn_id),
        "order_id": str(order_id) if order_id is not None else None,
        "time": txn.get("time") or txn.get("tradeDate") or txn.get("settlementDate"),
        "type": ttype or "TRADE",
        "net_amount": _num(txn.get("netAmount")),
        "fees": round(fees, 2) if fees else 0.0,
        "legs": legs,
    }, None


# LIVE_VERIFY — the exact Schwab transaction ``type`` for a cash dividend is
# UNCONFIRMED against a live feed; these are the believed candidates (the audit
# names DIVIDEND_OR_INTEREST / RECEIVE_AND_DELIVER). Confirm the real type values
# Schwab sends for a cash dividend before trusting this recognition path.
DIVIDEND_TYPES = {"DIVIDEND_OR_INTEREST", "RECEIVE_AND_DELIVER", "CASH_DIVIDEND",
                  "DIVIDEND", "ORDINARY_DIVIDEND", "QUALIFIED_DIVIDEND"}


def parse_dividend(txn: dict) -> dict | None:
    """Recognize a cash-dividend transaction and normalize it to a dividend income
    record {transaction_id, ticker, amount, time, type}. Returns None for anything
    that isn't a recognizable dividend. Cash dividends were previously DROPPED
    (parse_transaction returns (None, None) for non-TRADE rows), so held-share
    dividend income was silently discarded. LIVE_VERIFY — see DIVIDEND_TYPES."""
    if not isinstance(txn, dict):
        return None
    if (txn.get("type") or "").upper() not in DIVIDEND_TYPES:
        return None
    txn_id = txn.get("activityId") or txn.get("transactionId") or txn.get("id")
    if txn_id is None:
        return None
    amount = _num(txn.get("netAmount"))
    if not amount:
        return None
    ticker = None
    for it in (txn.get("transferItems") or txn.get("transactionItems") or []):
        inst = it.get("instrument") or {}
        sym = inst.get("symbol") or inst.get("underlyingSymbol")
        if sym:
            ticker = str(sym).upper()
            break
    return {"transaction_id": str(txn_id), "ticker": ticker,
            "amount": round(float(amount), 2),
            "time": txn.get("time") or txn.get("tradeDate") or txn.get("settlementDate"),
            "type": (txn.get("type") or "").upper()}


def dividend_proposals(feed: list, already: set) -> list[dict]:
    """Not-yet-ingested cash-dividend rows from a feed, as one-click
    dividend_income proposals (the app never auto-books — NO_AUTO_REMEDIATION)."""
    out: list[dict] = []
    for txn in feed or []:
        d = parse_dividend(txn)
        if d and d["transaction_id"] not in already:
            out.append(dict(d, proposal_id=f"div_{d['transaction_id']}",
                            action="dividend_income",
                            summary=(f"cash dividend ${d['amount']:.2f} on "
                                     f"{d['ticker'] or '?'} — adopt to book as dividend income")))
    return out


def parse_feed(feed: list) -> tuple[list[dict], list[str]]:
    """Parse a whole transactions feed. Returns ``(records, errors)``."""
    records: list[dict] = []
    errors: list[str] = []
    for txn in feed or []:
        rec, err = parse_transaction(txn)
        if rec is not None:
            records.append(rec)
        elif err:
            errors.append(err)
    return records, errors


# ---------------------------------------------------------------------------
# Group by orderId — link multi-leg executions into one logical action
# ---------------------------------------------------------------------------
def group_by_order(records: list[dict]) -> list[dict]:
    """Group parsed transactions sharing a Schwab ``orderId`` into one logical
    action. Transactions with no orderId each stand alone (keyed by their own
    transaction id) — they can still be matched/adopted as single-leg trades."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for rec in records:
        key = rec["order_id"] or f"txn:{rec['transaction_id']}"
        g = groups.get(key)
        if g is None:
            g = {"order_id": rec["order_id"], "group_key": key,
                 "transaction_ids": [], "legs": [], "time": rec.get("time"),
                 "fees": 0.0}
            groups[key] = g
            order.append(key)
        g["transaction_ids"].append(rec["transaction_id"])
        for leg in rec["legs"]:
            g["legs"].append(dict(leg, transaction_id=rec["transaction_id"]))
        g["fees"] = round((g["fees"] or 0.0) + (rec.get("fees") or 0.0), 2)
        # Keep the earliest timestamp for the logical action.
        if rec.get("time") and (not g.get("time") or str(rec["time"]) < str(g["time"])):
            g["time"] = rec["time"]
    return [groups[k] for k in order]


def infer_action(legs: list[dict]) -> str:
    """Infer the logical action a group of legs represents. Deep-ITM long calls
    (buy/sell to open/close with a positive/negative amount) map to leap legs; the
    short-call legs map to sell/close short; a close-call + open-call pair is a
    roll. LIVE_VERIFY: distinguishing a LEAP long-call open from a covered-call
    short-call open relies on position effect + instruction, which is why adoption
    still routes through the operator (who confirms the action)."""
    opts = [l for l in legs if l["asset_type"] == "OPTION"]
    if not opts:
        return ACT_UNKNOWN
    opens = [l for l in opts if l["position_effect"] == "OPENING"
             or _instruction_of(l) in _OPENING]
    closes = [l for l in opts if l["position_effect"] == "CLOSING"
              or _instruction_of(l) in _CLOSING]
    # A roll: one closing + one opening call leg.
    if len(closes) == 1 and len(opens) == 1:
        return ACT_ROLL
    if len(opts) == 1:
        leg = opts[0]
        buying = (leg["amount"] or 0) > 0
        opening = leg["position_effect"] == "OPENING" or _instruction_of(leg) in _OPENING
        if opening:
            return ACT_BUY_LEAP if buying else ACT_SELL_SHORT
        return ACT_CLOSE_SHORT if buying else ACT_CLOSE_LEAP
    return ACT_UNKNOWN


def _instruction_of(leg: dict) -> str:
    """Reconstruct a BUY/SELL_TO_OPEN/CLOSE label from amount sign + position
    effect when Schwab didn't echo an explicit instruction on the transferItem."""
    amt = leg.get("amount") or 0
    eff = leg.get("position_effect")
    side = "BUY" if amt > 0 else "SELL"
    if eff == "OPENING":
        return f"{side}_TO_OPEN"
    if eff == "CLOSING":
        return f"{side}_TO_CLOSE"
    return ""


# ---------------------------------------------------------------------------
# App-order index — which Schwab orderIds does the app already know about?
# ---------------------------------------------------------------------------
def app_order_ids(state: dict) -> set[str]:
    """Every Schwab orderId the app has a record of, across all order stores:
    pending_orders (keys), order_events, order_locks, order_submissions, and
    order_receipts. Used to tell a matched fill (source: app) from an out-of-band
    trade (source: broker_manual)."""
    ids: set[str] = set()
    ids.update(str(k) for k in (state.get("pending_orders") or {}).keys())
    for ev in state.get("order_events") or []:
        if ev.get("order_id") is not None:
            ids.add(str(ev["order_id"]))
    for lock in (state.get("order_locks") or {}).values():
        if lock.get("order_id") is not None:
            ids.add(str(lock["order_id"]))
    for sub in (state.get("order_submissions") or {}).values():
        if sub.get("order_id") is not None:
            ids.add(str(sub["order_id"]))
    for r in state.get("order_receipts") or []:
        if r.get("order_id") is not None:
            ids.add(str(r["order_id"]))
    return ids


def ingested_ids(state: dict) -> set[str]:
    """Transaction ids already ingested (the dedupe set)."""
    return set((state.get("ingested_transactions") or {}).keys())


# Map a broker leg to the app execution ACTION it would book, keyed by
# instruction. Mirrors executor.INSTRUCTION inverted; used to dedupe a broker
# fill against an execution the app ALREADY holds.
def _leg_action(leg: dict) -> str | None:
    if leg.get("asset_type") != "OPTION":
        return None
    buying = (leg.get("amount") or 0) > 0
    closing = leg.get("position_effect") == "CLOSING" or _instruction_of(leg) in _CLOSING
    if closing:
        return "close_short" if buying else "close_leap"
    return "buy_leap" if buying else "sell_short"


def _exec_key(ticker, action, strike, expiry, contracts) -> tuple:
    def _r(v):
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None
    return ((ticker or "").upper(), action, _r(strike),
            str(expiry)[:10] if expiry else None, int(contracts or 0))


def existing_execution_keys(state: dict) -> dict[tuple, int]:
    """A multiset of (ticker, action, strike, expiry, contracts) keys the app has
    ALREADY booked as executions. A broker leg whose key is present here is a fill
    the app already has — it must be CONFIRMED, never surfaced for adoption (that
    was the duplicate-leg defect). Count-valued so N identical legs match N booked
    executions, not one."""
    keys: dict[tuple, int] = {}
    for e in state.get("executions") or []:
        action = e.get("action")
        if action not in ("sell_short", "close_short", "buy_leap", "close_leap"):
            continue
        k = _exec_key(e.get("ticker"), action, e.get("strike"),
                      e.get("expiration"), e.get("contracts"))
        keys[k] = keys.get(k, 0) + 1
    return keys


def _group_already_booked(legs: list[dict], exec_keys: dict[tuple, int]) -> bool:
    """True when EVERY option leg of a group corresponds to an execution the app
    already holds (consuming counts so a genuinely-new second identical leg is not
    swallowed by one booked leg). Equity legs (assignments) never count as booked —
    those are always surfaced."""
    opt_legs = [l for l in legs if l["asset_type"] == "OPTION"]
    if not opt_legs:
        return False
    remaining = dict(exec_keys)
    ticker = _underlying(legs)
    for leg in opt_legs:
        action = _leg_action(leg)
        k = _exec_key(ticker, action, leg.get("strike"), leg.get("expiry"),
                      abs(leg.get("amount") or 0))
        if remaining.get(k, 0) <= 0:
            return False
        remaining[k] -= 1
    return True


# ---------------------------------------------------------------------------
# Exposure description (spec §6) for an out-of-band / unbalanced group
# ---------------------------------------------------------------------------
def _exposure(action: str, legs: list[dict]) -> str:
    opts = [l for l in legs if l["asset_type"] == "OPTION"]
    if action == ACT_ROLL:
        return "covered roll: short call bought to close and a new short call sold to open"
    if action == ACT_SELL_SHORT:
        return "a new SHORT CALL was opened out-of-band — confirm it is covered by the LEAP"
    if action == ACT_CLOSE_SHORT:
        return "a short call was bought to close out-of-band — the leg may now be uncovered/removed"
    if action == ACT_BUY_LEAP:
        return "a long call (LEAP) was opened out-of-band"
    if action == ACT_CLOSE_LEAP:
        return ("a long call (LEAP) was closed out-of-band — any remaining short call may be "
                "UNCOVERED (naked). Review immediately.")
    if any(l["asset_type"] == "EQUITY" and (l["amount"] or 0) < 0 for l in legs):
        return "SHORT STOCK appeared out-of-band — assignment likely; review immediately"
    return f"out-of-band trade with {len(opts)} option leg(s) — review"


def _summ_leg(leg: dict) -> str:
    if leg["asset_type"] == "OPTION":
        cp = "call" if leg["put_call"] == reconcile.CALL else "put"
        return (f"{_instruction_of(leg) or '?'} {abs(leg['amount'] or 0):g} "
                f"{leg['underlying']} {leg['strike']} {cp} @ {leg['price']}")
    return f"{'BUY' if (leg['amount'] or 0) > 0 else 'SELL'} {abs(leg['amount'] or 0):g} {leg['underlying']} shares"


def _underlying(legs: list[dict]) -> str | None:
    for l in legs:
        if l.get("underlying"):
            return l["underlying"]
    return None


# ---------------------------------------------------------------------------
# Core — pure over (feed, state)
# ---------------------------------------------------------------------------
def build_report(feed: list, state: dict, as_of: str | None = None) -> dict:
    """Classify a transactions feed against current state. PURE — no I/O, no
    mutation of ``state``. Returns the ingestion report:

      {as_of, fetched, parsed, matched:[...], proposals:[...],
       skipped_duplicates:[txn_id...], errors:[...]}

    ``matched``   — groups whose orderId the app already knows (source: app):
                    the fill confirms an existing app order; ingestion records the
                    transaction→order linkage (no execution is created here — the
                    app booked it at fill time).
    ``proposals`` — out-of-band groups (source: broker_manual) surfaced for
                    one-click operator adoption; every economic field is from the
                    broker record. NOT auto-applied (NO_AUTO_REMEDIATION).
    """
    as_of = as_of or _utcnow()
    records, errors = parse_feed(feed)
    already = ingested_ids(state)
    known_orders = app_order_ids(state)
    exec_keys = existing_execution_keys(state)

    groups = group_by_order(records)
    matched: list[dict] = []
    proposals: list[dict] = []
    skipped: list[str] = []

    for g in groups:
        fresh_txn_ids = [t for t in g["transaction_ids"] if t not in already]
        dup_txn_ids = [t for t in g["transaction_ids"] if t in already]
        skipped.extend(dup_txn_ids)
        if not fresh_txn_ids:
            continue  # every leg of this group already ingested — idempotent no-op

        action = infer_action(g["legs"])
        # A broker fill is "already ours" when EITHER its Schwab orderId is a known
        # app order OR every leg corresponds to an execution the app already booked
        # (the latter guards the duplicate-leg defect: an app fill whose orderId
        # didn't link must be CONFIRMED, never offered for adoption).
        is_app = ((g["order_id"] is not None and str(g["order_id"]) in known_orders)
                  or _group_already_booked(g["legs"], exec_keys))
        common = {
            "order_id": g["order_id"],
            "group_key": g["group_key"],
            "transaction_ids": fresh_txn_ids,
            "time": g.get("time"),
            "fees": g.get("fees") or 0.0,
            "ticker": _underlying(g["legs"]),
            "action": action,
            "legs": g["legs"],
            "leg_summaries": [_summ_leg(l) for l in g["legs"]],
        }
        if is_app:
            by = (f"app order {g['order_id']}" if g["order_id"] and str(g["order_id"]) in known_orders
                  else "an execution the app already booked")
            matched.append(dict(common, source=SOURCE_APP,
                                summary=f"broker fill confirms {by}"))
        else:
            pid = f"adopt_{g['group_key']}".replace(":", "_")
            proposals.append(dict(
                common, source=SOURCE_BROKER_MANUAL, proposal_id=pid,
                exposure=_exposure(action, g["legs"]),
                summary=(f"out-of-band {action} on {_underlying(g['legs']) or '?'} "
                         f"(broker order {g['order_id'] or 'n/a'}) — adopt to book it")))

    return {
        "as_of": as_of,
        "fetched": len(feed or []),
        "parsed": len(records),
        "matched": matched,
        "proposals": proposals,
        # Cash dividends on held shares (schema v20) — previously DROPPED. Surfaced
        # as one-click dividend_income proposals; never auto-booked (NO_AUTO_REMEDIATION).
        "dividend_proposals": dividend_proposals(feed, already),
        "skipped_duplicates": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Dedupe-ledger persistence (append-only; keyed by transaction id)
# ---------------------------------------------------------------------------
def record_ingested(state: dict, transaction_id: str, *, source: str,
                    order_id: str | None = None, execution_ids: list | None = None,
                    proposal_id: str | None = None) -> None:
    """Mark a transaction id ingested so re-runs skip it. Append-only into
    ``state["ingested_transactions"]``; never overwrites an existing entry."""
    ledger = state.setdefault("ingested_transactions", {})
    if str(transaction_id) in ledger:
        return
    ledger[str(transaction_id)] = {
        "source": source,
        "order_id": order_id,
        "execution_ids": execution_ids or [],
        "proposal_id": proposal_id,
        "ingested_at": _utcnow(),
    }


def _persist_report(state: dict, report: dict) -> None:
    """Store the last ingestion report + surface open proposals, and record every
    MATCHED transaction id into the dedupe ledger (matched fills need no operator
    action — the app already booked them). Out-of-band proposals are NOT recorded
    as ingested until the operator adopts them, so they keep surfacing until acted
    on."""
    ing = state.setdefault("ingestion", {"last": None, "proposals": []})
    ing["last"] = {k: report[k] for k in ("as_of", "fetched", "parsed",
                                          "skipped_duplicates", "errors")}
    ing["last"]["matched"] = len(report["matched"])
    ing["last"]["proposals"] = len(report["proposals"])
    if report.get("errors"):
        ing["last_success"] = ing.get("last_success")
    else:
        ing["last_success"] = report["as_of"]

    for m in report["matched"]:
        for tid in m["transaction_ids"]:
            record_ingested(state, tid, source=SOURCE_APP, order_id=m["order_id"])

    # Merge open proposals: keep any not-yet-adopted proposal, refresh with the
    # latest surfacing. A proposal whose transaction ids have since been ingested
    # (adopted) drops off.
    already = ingested_ids(state)
    open_props = [p for p in report["proposals"]
                  if not all(t in already for t in p["transaction_ids"])]
    ing["proposals"] = open_props


# ---------------------------------------------------------------------------
# Fetch wrapper
# ---------------------------------------------------------------------------
def _start_end_window() -> tuple[str, str]:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=int(config.INGESTION_LOOKBACK_DAYS))
    return (start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"))


def fetch_transactions() -> list:
    """Live Schwab transactions call, isolated so tests monkeypatch it."""
    import data_handler
    client = data_handler.client()
    account_hash = client.primary_account_hash()
    start, end = _start_end_window()
    return client.get_transactions(account_hash, start_date=start, end_date=end)


def run_ingestion(state: dict | None = None, persist: bool = True,
                  feed: list | None = None) -> dict:
    """Pull the Schwab transactions feed, classify it, and (by default) persist
    the dedupe ledger + surfaced proposals. Idempotent: re-running skips already
    ingested transaction ids. A fetch failure returns a report with the error and
    touches nothing (like reconcile's failure report)."""
    import logging_handler as log

    owns_state = state is None
    state = state if state is not None else log.load_state()
    as_of = _utcnow()

    if feed is None:
        try:
            feed = fetch_transactions()
        except Exception as e:  # noqa: BLE001 — isolate the fetch failure
            report = {"as_of": as_of, "fetched": 0, "parsed": 0, "matched": [],
                      "proposals": [], "skipped_duplicates": [],
                      "errors": [f"transactions fetch failed: {e}"], "broker_ok": False}
            logger.warning("transaction ingestion fetch failed: %s", e)
            return report

    report = build_report(feed, state, as_of)
    report["broker_ok"] = True
    if persist:
        _persist_report(state, report)
        if owns_state:
            log.save_state(state)
    return report
