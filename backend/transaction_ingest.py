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
ACT_BUY_SHARES = "buy_shares"
ACT_SELL_SHARES = "sell_shares"
ACT_UNKNOWN = "unknown"

# Schwab instruction -> position effect we care about. LIVE_VERIFY: confirm the
# exact instruction strings Schwab echoes on option TRADE transferItems.
_OPENING = {"BUY_TO_OPEN", "SELL_TO_OPEN"}
_CLOSING = {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}

# assetType values that trade like a plain share (buy N, sell N, no strike or
# expiry) and are normalized to "EQUITY" everywhere past this parse boundary.
# CONFIRMED LIVE (2026-09-24): Schwab classifies an ETF's own transactionItems
# under "COLLECTIVE_INVESTMENT", not "EQUITY" — an individual stock is
# "EQUITY". Before this was found, an ETF buy/sell (e.g. IBIT) fell outside
# the old OPTION/EQUITY filter entirely and was silently dropped with no
# trace by the "pure fee row" no-op path, exactly like the once-undetected
# DIVIDEND_TYPES gap. Extend this set (never widen the bare "EQUITY" check
# elsewhere) if another such type is confirmed.
_EQUITY_LIKE = {"EQUITY", "COLLECTIVE_INVESTMENT"}


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
    if asset in _EQUITY_LIKE:
        asset = "EQUITY"  # normalize the ETF/fund synonym so every downstream
                          # check (asset_type == "EQUITY") needs to know only one spelling
    symbol = (inst.get("symbol") or "").strip()
    amount = _num(item.get("amount"), 0.0) or 0.0           # signed contract/share count
    price = _num(item.get("price"))                          # per-share/contract fill price
    cost = _num(item.get("cost"))                            # signed net cash for the leg
    pos_effect = (item.get("positionEffect") or "").upper()  # OPENING / CLOSING
    fee_type = (item.get("feeType") or "").upper()

    # An option leg's "underlying" is its underlyingSymbol (a different ticker
    # from the option's own OCC symbol); a plain share leg (EQUITY, including
    # a normalized ETF) has no underlyingSymbol field at all — ITS ticker IS
    # its own symbol. Falling back here, once, fixes ticker resolution for
    # every equity-only group at the source instead of patching each of
    # _underlying()/_summ_leg() separately.
    underlying = (inst.get("underlyingSymbol") or "").upper() or None
    if underlying is None and asset == "EQUITY" and symbol:
        underlying = symbol.upper()
    leg = {
        "symbol": symbol,
        "asset_type": asset,
        "amount": amount,
        "price": price,
        "cost": cost,
        "position_effect": pos_effect,
        "fee_type": fee_type or None,
        "underlying": underlying,
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
        # NOTE the reason is None, so `parse_transactions` drops the row with no
        # error recorded. That silence is correct for a fee row and WRONG for an
        # assignment; `assignment_proposals` below is the recognizer, and
        # `unrecognized_on_open_puts` is the loud backstop for anything it misses.
        return None, None
    txn_id = txn.get("activityId") or txn.get("transactionId") or txn.get("id")
    if txn_id is None:
        return None, "transaction has no activityId/transactionId (cannot dedupe)"
    order_id = txn.get("orderId")
    items = txn.get("transferItems") or txn.get("transactionItems") or []
    legs = [_leg_from_transfer_item(it) for it in items
            if (it.get("instrument") or {}).get("assetType", "").upper() in ({"OPTION"} | _EQUITY_LIKE)]
    if not legs:
        # A TRADE row where no item carries an instrument at all (a pure fee/
        # interest line, or a CURRENCY settlement leg alongside a recognized
        # one — see the filter above) is a legitimate silent no-op. But an
        # item that DOES carry an instrument + assetType Schwab actually sent
        # — just one this code doesn't recognize (fixed income, an option on
        # futures, etc.) — must never vanish the same way: that is exactly
        # how the DIVIDEND_TYPES gap went undetected until it was found by
        # hand. Surface it as a loud parse issue instead of a silent drop.
        unrecognized = sorted({
            (it.get("instrument") or {}).get("assetType", "").upper()
            for it in items if (it.get("instrument") or {}).get("assetType")
        } - {"OPTION"} - _EQUITY_LIKE)
        if unrecognized:
            return None, (f"transaction {txn_id} has an unrecognized instrument type "
                          f"({', '.join(unrecognized)}) — not ingested; needs a code fix")
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


# ---------------------------------------------------------------------------
# Option assignment / exercise (schema v22)
#
# LIVE_VERIFY — THESE STRINGS ARE UNCONFIRMED AGAINST A LIVE FEED.
#
# The exact Schwab transaction ``type`` for an option assignment is not documented
# in a form worth trusting, and this codebase has been burned by exactly that once
# already: DIVIDEND_TYPES above is itself an unverified candidate set, added after
# cash dividends were found to be silently discarded by the non-TRADE drop.
#
# So the recognizer is deliberately NOT the safety mechanism. The safety mechanism
# is `unrecognized_on_open_puts`, which fires on ANY non-TRADE row touching a
# symbol that holds an open put — INCLUDING a type absent from this set. Getting
# these strings wrong costs a manual classification step; it does not cost a
# missed assignment. Confirm them against one real assignment and narrow the set.
# ---------------------------------------------------------------------------
ASSIGNMENT_TYPES = {"RECEIVE_AND_DELIVER", "ASSIGNMENT", "OPTION_ASSIGNMENT",
                    "EXERCISE", "OPTION_EXERCISE", "OPTION_EXPIRATION",
                    "EXPIRATION"}


def parse_assignment(txn: dict) -> dict | None:
    """Recognize an option assignment/exercise and normalize it. None otherwise.

    Early and expiry assignment look IDENTICAL here, deliberately — the broker
    reports the same event shape and the app treats them the same way. Early
    assignment on a short put is more likely than intuition suggests when short
    rates are elevated (the holder earns interest on the strike proceeds against
    the put's remaining extrinsic), so a recognizer that keyed off the expiry date
    would miss the surprising half of the cases."""
    if not isinstance(txn, dict):
        return None
    if (txn.get("type") or "").upper() not in ASSIGNMENT_TYPES:
        return None
    txn_id = txn.get("activityId") or txn.get("transactionId") or txn.get("id")
    if txn_id is None:
        return None
    ticker = strike = expiration = put_call = None
    shares = 0
    for it in (txn.get("transferItems") or txn.get("transactionItems") or []):
        inst = it.get("instrument") or {}
        asset = (inst.get("assetType") or "").upper()
        if asset == "OPTION":
            sym = inst.get("symbol")
            parsed = _parse_occ(sym) if sym else None
            if parsed:
                ticker = ticker or parsed["underlying"]
                strike, expiration = parsed["strike"], parsed["expiry"]
                put_call = parsed["put_call"]
        elif asset == "EQUITY":
            ticker = ticker or (inst.get("symbol") or "").upper() or None
            shares = int(abs(_num(it.get("amount")) or 0))
    if not ticker:
        return None
    return {"transaction_id": str(txn_id), "ticker": str(ticker).upper(),
            "strike": strike, "expiration": expiration, "put_call": put_call,
            "shares": shares,
            "time": txn.get("time") or txn.get("tradeDate") or txn.get("settlementDate"),
            "type": (txn.get("type") or "").upper()}


def _parse_occ(symbol: str) -> dict | None:
    """Underlying / expiry / side / strike from an OCC option symbol.

    Reuses ``reconcile``'s parser rather than restating the 21-char layout — the
    broker view already had to read these symbols to compare holdings, and two
    parsers for one format is two places to get the strike scale wrong."""
    try:
        import reconcile
        parsed = reconcile.parse_option_symbol(symbol)
    except Exception:  # noqa: BLE001 — an unreadable symbol is not an assignment
        return None
    if not parsed:
        return None
    return {"underlying": parsed.get("underlying"), "strike": parsed.get("strike"),
            "expiry": parsed.get("expiry"), "put_call": parsed.get("put_call")}


def assignment_proposals(feed: list, already: set, open_puts: dict) -> list[dict]:
    """Not-yet-ingested assignment rows on symbols holding an open put, as
    one-click ``put_assigned`` proposals.

    The app NEVER auto-books one [NO_AUTO_REMEDIATION]: an assignment converts
    collateral into shares and retags the position, which is exactly the class of
    change that must be a human's decision even when the evidence is unambiguous.
    """
    out: list[dict] = []
    for txn in feed or []:
        a = parse_assignment(txn)
        if not a or a["transaction_id"] in already:
            continue
        leg = (open_puts or {}).get(a["ticker"])
        if not leg:
            continue
        out.append({
            **a, "proposal_id": f"assign_{a['transaction_id']}",
            "action": "put_assigned",
            "strike": a.get("strike") or leg.get("strike"),
            "expiration": a.get("expiration") or leg.get("expiration"),
            "contracts": leg.get("contracts"),
            "source": "broker_assignment",
        })
    return out


def unrecognized_on_open_puts(feed: list, open_puts: dict) -> list[dict]:
    """THE LOUD BACKSTOP, and the point of this whole section.

    Any non-TRADE transaction touching a symbol that holds an open put, whose type
    ``parse_assignment`` did NOT recognize. Those rows are otherwise dropped with
    no error by ``parse_transaction`` — the same silent discard that swallowed cash
    dividends until ``parse_dividend`` was added.

    The failure this prevents is specific and expensive: if an assignment is not
    detected, the application believes it holds cash and collateral while the
    account actually holds 100 shares and no put. The covered-call machinery never
    engages, the shares sit uncovered, and nothing on the screen says so.

    Correctness here does NOT depend on ``ASSIGNMENT_TYPES`` being right. A type
    absent from that set lands in this list instead, and a reconciliation
    discrepancy is a worse outcome than a clean proposal but an incomparably
    better one than silence.
    """
    out: list[dict] = []
    for txn in feed or []:
        if not isinstance(txn, dict):
            continue
        ttype = (txn.get("type") or "").upper()
        if not ttype or ttype == "TRADE" or parse_assignment(txn) is not None:
            continue
        for it in (txn.get("transferItems") or txn.get("transactionItems") or []):
            inst = it.get("instrument") or {}
            sym = (inst.get("underlyingSymbol") or inst.get("symbol") or "")
            base = str(sym).split()[0].upper() if sym else ""
            if base and base in (open_puts or {}):
                out.append({
                    "transaction_id": str(txn.get("activityId")
                                          or txn.get("transactionId") or ""),
                    "ticker": base, "type": ttype,
                    "time": txn.get("time") or txn.get("tradeDate"),
                    "summary": (f"Unrecognized '{ttype}' activity on {base}, which holds "
                                f"an open short put — could be an assignment. Classify "
                                f"it manually; do NOT assume it is benign."),
                })
                break
    return out


def open_put_legs(state: dict) -> dict:
    """{ticker: the first open short-put leg} across open positions — the lookup
    both recognizers above key off."""
    out: dict = {}
    for p in (state or {}).get("positions", []):
        if p.get("status") == "closed":
            continue
        legs = p.get("short_puts") or []
        if legs:
            out[(p.get("ticker") or "").upper()] = legs[0]
    return out


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
    roll. A lone equity leg is the shares-primary base leg (buy_shares/
    sell_shares) — CONFIRMED LIVE: before this, a plain share sale fell through
    to ACT_UNKNOWN and the generic "SHORT STOCK appeared out-of-band —
    assignment likely" warning, alarming language meant for actual unexplained
    short stock, not a legitimate closing sale of a real long position.
    LIVE_VERIFY: distinguishing a LEAP long-call open from a covered-call
    short-call open relies on position effect + instruction, which is why adoption
    still routes through the operator (who confirms the action)."""
    opts = [l for l in legs if l["asset_type"] == "OPTION"]
    if not opts:
        equity = [l for l in legs if l["asset_type"] == "EQUITY"]
        if len(equity) == 1:
            return ACT_BUY_SHARES if (equity[0]["amount"] or 0) > 0 else ACT_SELL_SHARES
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
    if leg.get("asset_type") == "EQUITY":
        buying = (leg.get("amount") or 0) > 0
        return "buy_shares" if buying else "sell_shares"
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
    """A multiset of (ticker, action, strike, expiry, contracts-or-qty) keys the
    app has ALREADY booked as executions. A broker leg whose key is present here
    is a fill the app already has — it must be CONFIRMED, never surfaced for
    adoption (that was the duplicate-leg defect). Count-valued so N identical
    legs match N booked executions, not one. Covers both the option actions and
    the shares-primary base-leg actions (buy_shares/sell_shares), keyed by qty
    with no strike/expiry — a plain share fill needs the same real-booking
    verification an option leg gets, not just a known Schwab order id (an order
    the app placed and tracked is not proof its fill was ever turned into an
    execution — see the ``is_app`` note in ``build_report``)."""
    keys: dict[tuple, int] = {}
    for e in state.get("executions") or []:
        action = e.get("action")
        if action in ("sell_short", "close_short", "buy_leap", "close_leap"):
            k = _exec_key(e.get("ticker"), action, e.get("strike"),
                          e.get("expiration"), e.get("contracts"))
        elif action in ("buy_shares", "sell_shares"):
            k = _exec_key(e.get("ticker"), action, None, None, e.get("qty"))
        else:
            continue
        keys[k] = keys.get(k, 0) + 1
    return keys


def _group_already_booked(legs: list[dict], exec_keys: dict[tuple, int]) -> bool:
    """True when EVERY option or plain-share leg of a group corresponds to an
    execution the app already holds (consuming counts so a genuinely-new second
    identical leg is not swallowed by one booked leg). A leg this function
    can't classify (assignments never reach here — see build_report's TRADE-
    only gate) makes the whole group unverified, never counted as booked."""
    relevant = [l for l in legs if l["asset_type"] in ("OPTION", "EQUITY")]
    if not relevant:
        return False
    remaining = dict(exec_keys)
    ticker = _underlying(legs)
    for leg in relevant:
        action = _leg_action(leg)
        if action is None:
            return False
        if leg["asset_type"] == "OPTION":
            k = _exec_key(ticker, action, leg.get("strike"), leg.get("expiry"),
                          abs(leg.get("amount") or 0))
        else:
            k = _exec_key(ticker, action, None, None, abs(leg.get("amount") or 0))
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
    if action == ACT_BUY_SHARES:
        return "shares were bought out-of-band — the base lot for this book's engine"
    if action == ACT_SELL_SHARES:
        return "shares were sold out-of-band — closing or trimming the base lot"
    # Reached only when a group's action genuinely couldn't be classified (not
    # the normal single-equity-leg case above, which is now buy_shares/
    # sell_shares) — an actual unexplained short stock appearance is rare and
    # does warrant this loud a warning.
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
       skipped_duplicates:[txn_id...], skipped_detail:[{transaction_id, ticker,
       source, order_id, proposal_id, ingested_at}...], errors:[...]}

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
    # WHY each duplicate was already ingested — a group that never became a
    # matched/proposed row is otherwise invisible: it just silently disappears
    # on every re-run, indistinguishable from "there was nothing here" without
    # this. Ledger detail included so a transaction wrongly marked "matched"
    # from a broken order-link (the app's own order never got its fill
    # recorded) is diagnosable from the ingestion report itself, not a guess.
    already_ledger = state.get("ingested_transactions") or {}
    skipped_detail: list[dict] = []

    for g in groups:
        fresh_txn_ids = [t for t in g["transaction_ids"] if t not in already]
        dup_txn_ids = [t for t in g["transaction_ids"] if t in already]
        skipped.extend(dup_txn_ids)
        for tid in dup_txn_ids:
            rec = already_ledger.get(str(tid)) or {}
            skipped_detail.append({
                "transaction_id": tid, "ticker": _underlying(g["legs"]),
                "source": rec.get("source"), "order_id": rec.get("order_id"),
                "proposal_id": rec.get("proposal_id"), "ingested_at": rec.get("ingested_at"),
            })
        if not fresh_txn_ids:
            continue  # every leg of this group already ingested — idempotent no-op

        action = infer_action(g["legs"])
        # A broker fill is "already ours" ONLY when every leg corresponds to an
        # execution the app already booked (content-verified via exec_keys) —
        # NEVER on a known Schwab orderId alone. A known orderId means the app
        # PLACED the order; it is not proof the fill ever got turned into an
        # execution (order polling can die between submission and fill, same
        # failure _enrich_proposals_from_journal already recovers for). Trusting
        # the orderId alone silently marked a real, unbooked broker fill "matched"
        # — permanently buried in the dedupe ledger with no execution behind it,
        # since a matched fill books nothing further by design. This was a real,
        # confirmed incident, not a hypothetical: see the session notes.
        app_order_known = g["order_id"] is not None and str(g["order_id"]) in known_orders
        is_app = _group_already_booked(g["legs"], exec_keys)
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
            by = f"app order {g['order_id']}" if app_order_known else "an execution the app already booked"
            matched.append(dict(common, source=SOURCE_APP,
                                summary=f"broker fill confirms {by}"))
        else:
            pid = f"adopt_{g['group_key']}".replace(":", "_")
            # A known app order that still isn't content-verified is exactly the
            # lost-fill case: say so, so adopting it doesn't read as "why is my
            # own order showing up as out-of-band."
            lost_fill_note = (" — this order was placed from the app, but its fill "
                              "was never recorded here; adopting will book it now"
                              if app_order_known else "")
            proposals.append(dict(
                common, source=SOURCE_BROKER_MANUAL, proposal_id=pid,
                exposure=_exposure(action, g["legs"]),
                summary=(f"out-of-band {action} on {_underlying(g['legs']) or '?'} "
                         f"(broker order {g['order_id'] or 'n/a'}) — adopt to book it"
                         f"{lost_fill_note}")))

    _open_puts = open_put_legs(state)
    return {
        "as_of": as_of,
        "fetched": len(feed or []),
        "parsed": len(records),
        "matched": matched,
        "proposals": proposals,
        # Cash dividends on held shares (schema v20) — previously DROPPED. Surfaced
        # as one-click dividend_income proposals; never auto-booked (NO_AUTO_REMEDIATION).
        "dividend_proposals": dividend_proposals(feed, already),
        # Option assignment/exercise on a symbol holding an open put (schema v22).
        # Surfaced as one-click put_assigned proposals; never auto-booked.
        "assignment_proposals": assignment_proposals(feed, already, _open_puts),
        # THE LOUD BACKSTOP. Non-TRADE activity on a put-holding symbol that the
        # recognizer did not classify — otherwise dropped with no error at all.
        # These are discrepancies, not proposals: they require a human to look.
        "unrecognized_put_activity": unrecognized_on_open_puts(feed, _open_puts),
        "skipped_duplicates": skipped,
        "skipped_detail": skipped_detail,
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


def release_ingested(state: dict, transaction_ids: list[str]) -> list[str]:
    """Remove transaction ids from the dedupe ledger so the NEXT ingestion run
    re-classifies them from scratch, instead of skipping them as duplicates
    forever. For recovering a transaction that was wrongly marked ``matched``
    by the pre-fix ``is_app`` (a known Schwab order id alone, with no execution
    ever actually booked) — that transaction is otherwise buried permanently,
    since a matched fill books nothing further by design.

    Safe either way: releasing a transaction that WAS genuinely already booked
    just makes the next ingest re-verify it via ``_group_already_booked``
    (content-based, not order-id-based) and it re-matches as before; nothing is
    deleted from ``state["executions"]`` — only the dedupe marker. Returns the
    ids actually found and removed."""
    ledger = state.get("ingested_transactions") or {}
    removed = [tid for tid in transaction_ids if str(tid) in ledger]
    for tid in removed:
        del ledger[str(tid)]
    return removed


def _persist_report(state: dict, report: dict) -> None:
    """Store the last ingestion report + surface open proposals, and record every
    MATCHED transaction id into the dedupe ledger (matched fills need no operator
    action — the app already booked them). Out-of-band proposals are NOT recorded
    as ingested until the operator adopts them, so they keep surfacing until acted
    on."""
    ing = state.setdefault("ingestion", {"last": None, "proposals": []})
    ing["last"] = {k: report[k] for k in ("as_of", "fetched", "parsed",
                                          "skipped_duplicates", "skipped_detail", "errors")}
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
# The widest a one-off deeper pull may reach back — well past
# INGESTION_LOOKBACK_DAYS for the rare case of recovering a trade placed
# out-of-band long enough ago the normal daily window never saw it (e.g. one
# filled through the broker's own platform instead of this app). Dedupe by
# transaction id makes an overlapping wider pull always idempotent, so this is
# safe to use ad hoc — it is not a change to the standing daily window.
INGESTION_MAX_LOOKBACK_DAYS = 90


def _start_end_window(lookback_days: int | None = None) -> tuple[str, str]:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    days = int(config.INGESTION_LOOKBACK_DAYS) if lookback_days is None else int(lookback_days)
    days = max(1, min(days, INGESTION_MAX_LOOKBACK_DAYS))
    start = now - timedelta(days=days)
    return (start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"))


def fetch_transactions(lookback_days: int | None = None) -> list:
    """Live Schwab transactions call, isolated so tests monkeypatch it."""
    import accounts
    import data_handler
    client = data_handler.broker_client()
    # Ingest the transactions of THIS book's brokerage account only — pulling a
    # sibling account's fills would propose them for adoption into the wrong book.
    account_hash = accounts.broker_hash(client)
    start, end = _start_end_window(lookback_days)
    return client.get_transactions(account_hash, start_date=start, end_date=end)


def _enrich_proposals_from_journal(report: dict) -> None:
    """A proposal whose broker order id the app's order journal knows was placed
    FROM the app — its fill just never made it into (or was lost from) the store.
    Carry the journal's stock price onto the proposal so adoption books the same
    extrinsic split the original fill would have, without the operator retyping
    a number the app already captured."""
    import logging_handler as log
    for p in report.get("proposals") or []:
        oid = p.get("order_id")
        if not oid:
            continue
        try:
            journal = log.order_journal_lookup(str(oid))
        except Exception as e:  # noqa: BLE001 — enrichment is best-effort
            logger.warning("order journal lookup failed for %s: %s", oid, e)
            journal = None
        if not journal:
            continue
        p["app_order"] = True
        p["app_placed_at"] = journal.get("placed_at") or journal.get("at")
        if journal.get("stock_price") is not None:
            p["app_stock_price"] = journal["stock_price"]
            p["app_stock_price_source"] = journal.get("stock_price_source")
            p["summary"] = (f"{p.get('summary')} — placed from this app "
                            f"(stock {journal['stock_price']} captured at "
                            f"{journal.get('stock_price_source') or 'order'})")


def run_ingestion(state: dict | None = None, persist: bool = True,
                  feed: list | None = None, lookback_days: int | None = None) -> dict:
    """Pull the Schwab transactions feed, classify it, and (by default) persist
    the dedupe ledger + surfaced proposals. Idempotent: re-running skips already
    ingested transaction ids. A fetch failure returns a report with the error and
    touches nothing (like reconcile's failure report). ``lookback_days`` overrides
    the standing INGESTION_LOOKBACK_DAYS window for this one run only (capped at
    INGESTION_MAX_LOOKBACK_DAYS) — for recovering a trade placed out-of-band far
    enough back the normal daily window never saw it."""
    import logging_handler as log

    owns_state = state is None
    as_of = _utcnow()

    if feed is None:
        try:
            # Keep the common (no-override) path a zero-arg call — existing test
            # doubles for fetch_transactions take no parameters.
            feed = fetch_transactions() if lookback_days is None else fetch_transactions(lookback_days)
        except Exception as e:  # noqa: BLE001 — isolate the fetch failure
            report = {"as_of": as_of, "fetched": 0, "parsed": 0, "matched": [],
                      "proposals": [], "skipped_duplicates": [], "skipped_detail": [],
                      "errors": [f"transactions fetch failed: {e}"], "broker_ok": False}
            logger.warning("transaction ingestion fetch failed: %s", e)
            return report

    # The state is read AFTER the (slow) transactions fetch, and when this run
    # owns it the report is classified and persisted against a FRESH copy under
    # the store lock. Loading before the fetch and saving that copy afterwards
    # overwrote any fill the order poll committed in between — see
    # logging_handler.mutate_state.
    if not owns_state:
        report = build_report(feed, state, as_of)
        report["broker_ok"] = True
        _enrich_proposals_from_journal(report)
        if persist:
            _persist_report(state, report)
        return report

    def _classify_and_persist(fresh: dict) -> dict:
        rep = build_report(feed, fresh, as_of)
        rep["broker_ok"] = True
        _enrich_proposals_from_journal(rep)
        if persist:
            _persist_report(fresh, rep)
        return rep

    if persist:
        return log.mutate_state(_classify_and_persist)
    return _classify_and_persist(log.load_state())
