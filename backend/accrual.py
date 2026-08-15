"""Per-position accrual ledger + the 100-share lot builder (schema v21).

PROVENANCE — ``TRAVIS_EXTENSION``. Compounding realized income into further share
lots is not a CFM rule; it is what the shares-primary model makes possible.

WHAT ACCRUES (the whitelist — this is the whole safety story)

Exactly two sources may ever credit the ledger:

  * ``REALIZED_EXTRINSIC`` — the net extrinsic realized when a short-call CYCLE
    closes (bought back or expired worthless). Derived through
    ``logging_handler.close_economics``, which nets the extrinsic paid back out of
    the extrinsic sold, so INTRINSIC is excluded by construction: it passes through
    and nets to zero, and is never income [HARD_CFM_RULE].
  * ``DIVIDEND`` — a booked ``dividend_income`` event on held shares.

WHAT IS STRUCTURALLY EXCLUDED, and why it matters

  * **Roll-down credits.** A defensive roll takes in a credit today in exchange for
    a DEFERRED INTRINSIC OBLIGATION — the position now owes more if the stock
    recovers. Crediting that to a compounding ledger is the Martingale trap:
    every defensive roll would fund a bigger position, so the book scales UP
    precisely as the thesis deteriorates. The exclusion is enforced by
    construction in ``credit_for`` (a ``close_short`` carrying a ``roll_id`` is a
    roll leg and is rejected), not by a downstream filter that could be forgotten.
  * **Unrealized juice.** An open short's mark is not income until the cycle closes.
  * **Any intrinsic component.** See above.

DERIVED, NOT APPENDED. The credits are recomputed from the immutable execution log
by ``derive`` (like the theta / payback / dividend / roll ledgers) rather than
written as separate ``ACCRUAL_CREDIT`` executions. Two reasons: a credit carries no
information the source execution does not already carry, so appending one is
duplication that can drift; and a whitelist enforced at DERIVE time cannot be
bypassed by any future writer, whereas an append-time whitelist can. Each derived
record still names its ``source`` and its ``execution_id``, so the provenance the
event type was for is fully preserved.

``LOT_ADD_RECOMMENDED`` — the fact that the app recommended an add at a moment in
time — IS appended, because it is not derivable from trade history. An EXECUTED add
is deliberately not its own action: it is a ``buy_shares`` carrying a ``lot_add``
stamp, so it traverses the SAME freeze / Level-5 / execution-window / spread-quality
path as any other new risk.

ACCRUED CASH IS NOT EXPOSURE. Nothing here ever touches ``shares.count``,
``covered_lots`` or ``position_capital``. Accrued cash changes covered-call math at
exactly one moment: when a real 100-share lot is actually bought. There is no
fractional exposure and no partial coverage [HARD_CFM_RULE].

PURE over state — no clock, no provider calls, no I/O.
"""
from __future__ import annotations

import config
import logging_handler as log

SOURCE_REALIZED_EXTRINSIC = "REALIZED_EXTRINSIC"
SOURCE_DIVIDEND = "DIVIDEND"

# The ONLY sources that may ever credit the ledger.
ACCEPTED_SOURCES = frozenset({SOURCE_REALIZED_EXTRINSIC, SOURCE_DIVIDEND})

# Appended typed events (see the module docstring for why ACCRUAL_CREDIT is not
# among them).
LOT_ADD_RECOMMENDED = "lot_add_recommended"


def credit_for(execution: dict) -> dict | None:
    """The SINGLE place an execution may become an accrual credit, or None.

    This is the whitelist, enforced by construction. Anything not explicitly
    recognized below contributes nothing — a new execution action added later
    accrues nothing until someone deliberately adds it here.

    A ``close_short`` carrying a ``roll_id`` is one leg of a roll, NOT a cycle
    close: its credit is the near-side of a deferred intrinsic obligation, so it is
    rejected outright (the Martingale guard). A cycle close — a buyback or an
    expiry — carries no ``roll_id``.
    """
    if not isinstance(execution, dict):
        return None
    action = execution.get("action")

    if action == "close_short":
        # Martingale guard — a roll leg can never produce a credit.
        if execution.get("roll_id"):
            return None
        # Re-derived from the close's stored facts (never the stored net field), so
        # a stale figure can't leak in and so the intrinsic component is netted out
        # rather than trusted.
        _sold_ps, _paid_ps, net = log.close_economics(execution)
        if not net:
            return None
        return {
            "execution_id": execution.get("id"),
            "ticker": execution.get("ticker", ""),
            "source": SOURCE_REALIZED_EXTRINSIC,
            "amount": round(float(net), 2),
            "date": execution.get("date"),
            "detail": {"strike": execution.get("strike"),
                       "contracts": execution.get("contracts")},
        }

    if action == "dividend_income":
        amount = float(execution.get("amount") or 0)
        if not amount:
            return None
        return {
            "execution_id": execution.get("id"),
            "ticker": execution.get("ticker", ""),
            "source": SOURCE_DIVIDEND,
            "amount": round(amount, 2),
            "date": execution.get("pay_date") or execution.get("date"),
            "detail": {"per_share": execution.get("per_share"),
                       "shares": execution.get("shares"),
                       "ex_date": execution.get("ex_date")},
        }

    return None


def derive(state: dict, execs: list[dict]) -> dict:
    """Rebuild ``state['accrual_ledger']`` from the immutable execution log.

    ``execs`` is the DERIVED execution list (corrections already overlaid), so an
    appended ``txn_correction`` that fixes a close price flows straight through
    into the accrual balance with no separate reconciliation path.
    """
    records: list[dict] = []
    recommendations: list[dict] = []
    by_ticker: dict[str, dict] = {}

    def agg_for(ticker: str) -> dict:
        return by_ticker.setdefault(ticker, {
            "ticker": ticker, "credited": 0.0,
            "by_source": {src: 0.0 for src in sorted(ACCEPTED_SOURCES)},
            "spent_on_lots": 0.0, "accrued_cash": 0.0, "credits": 0})

    # ONE pass: credits, the lot-adds that spend them, and the recommendation log.
    for e in execs:
        if e.get("action") == LOT_ADD_RECOMMENDED:
            recommendations.append(
                {"execution_id": e.get("id"), "ticker": e.get("ticker", ""),
                 "date": e.get("date"), "accrued_cash": e.get("accrued_cash"),
                 "lot_cost": e.get("lot_cost"), "blocked": e.get("blocked"),
                 "blocked_reason": e.get("blocked_reason")})
            continue
        if e.get("action") == "buy_shares" and e.get("lot_add"):
            # A lot add SPENDS the balance it was funded by — without this the same
            # dollars would fund an unbounded series of adds.
            spent = agg_for(e.get("ticker", ""))
            spent["spent_on_lots"] = round(
                spent["spent_on_lots"] + float(e.get("execution_total") or 0), 2)
            continue
        credit = credit_for(e)
        if credit is None or credit["source"] not in ACCEPTED_SOURCES:
            continue
        records.append(credit)
        agg = agg_for(credit["ticker"])
        agg["credited"] = round(agg["credited"] + credit["amount"], 2)
        agg["by_source"][credit["source"]] = round(
            agg["by_source"][credit["source"]] + credit["amount"], 2)
        agg["credits"] += 1

    for agg in by_ticker.values():
        # Never negative: an add priced above the accrued balance (an operator
        # override, or a fill above the recommendation price) leaves zero, not a debt.
        agg["accrued_cash"] = round(max(agg["credited"] - agg["spent_on_lots"], 0.0), 2)

    return {
        "by_ticker": by_ticker,
        "records": records,
        "recommendations": recommendations,
        "total_accrued": round(sum(a["accrued_cash"] for a in by_ticker.values()), 2),
    }


# ---------------------------------------------------------------------------
# The lot builder
# ---------------------------------------------------------------------------
def lot_threshold(price_per_share: float | None) -> float | None:
    """Accrued cash required before a lot add is recommended:
    ``price x SHARES_PER_LOT x (1 + LOT_ADD_BUFFER_PCT)``. The buffer keeps a
    recommendation from being invalidated by a tick between the alert and the fill."""
    if price_per_share is None:
        return None
    return round(float(price_per_share) * config.SHARES_PER_LOT
                 * (1 + config.LOT_ADD_BUFFER_PCT), 2)


def progress(state: dict, ticker: str, price_per_share: float | None) -> dict:
    """Accrual progress toward the next 100-share lot for one ticker.

    Reports the balance and the threshold; it does NOT decide whether an add is
    actionable — that requires the Level 5 account gate (see ``lot_add_status``).
    ``ready`` here means only "the cash is there"."""
    ledger = (state.get("accrual_ledger") or {}).get("by_ticker") or {}
    agg = ledger.get((ticker or "").upper()) or ledger.get(ticker) or {}
    accrued = float(agg.get("accrued_cash") or 0.0)
    threshold = lot_threshold(price_per_share)
    pct = (round(min(accrued / threshold, 1.0) * 100, 1)
           if threshold else None)
    return {
        "ticker": ticker,
        "accrued_cash": round(accrued, 2),
        "threshold": threshold,
        "lot_cost": (round(float(price_per_share) * config.SHARES_PER_LOT, 2)
                     if price_per_share is not None else None),
        "shares_per_lot": config.SHARES_PER_LOT,
        "buffer_pct": config.LOT_ADD_BUFFER_PCT,
        "pct_to_next_lot": pct,
        "remaining": (round(max(threshold - accrued, 0.0), 2)
                      if threshold is not None else None),
        "ready": bool(threshold is not None and accrued >= threshold),
        # A partial balance is CASH, never exposure: it changes no covered-call
        # math until a whole lot is actually bought [HARD_CFM_RULE]. Nothing here
        # feeds covered_lots or position_capital — pinned by test_7.
        "by_source": agg.get("by_source") or {},
    }


def lot_add_status(state: dict, ticker: str, price_per_share: float | None) -> dict:
    """Is a lot add actionable — and if not, exactly why?

    A builder-recommended add is NEW RISK: one more 100-share lot on the book. It
    must therefore clear the SAME Level 5 account gate an ordinary entry does
    (cash reserve, position limit, deployed-capital cap, one-per-sector, the
    round-lot SIZE-BLOCK, and juice adequacy AT THE NEW SIZE), plus the
    reconciliation freeze. Accrued cash being sufficient is a precondition, never a
    licence.

    Returns ``actionable`` True only when the cash IS there AND the gate passes.
    When the cash is there but something else blocks, ``blocked`` is True with a
    typed ``blocked_reason`` and the failing check ids, so the UI can show
    blocked-with-reason rather than either hiding it or offering an add that would
    be rejected on submission.
    """
    import position_types
    prog = progress(state, ticker, price_per_share)
    out = {**prog, "actionable": False, "blocked": False, "blocked_reason": None,
           "blocking_failures": [], "gate": None, "freeze": None}
    if not prog["ready"]:
        return out  # not blocked — simply not there yet

    # The reconciliation freeze is checked FIRST, mirroring executor.execute's
    # ordering (a freeze wins over a gate rejection) so the reason the UI shows is
    # the reason a submission would actually hit.
    try:
        import reconcile
        freeze = reconcile.freeze_status(state)
    except Exception:  # noqa: BLE001 — a freeze read failure must not fake an all-clear
        freeze = {"frozen": True, "reason": "freeze status unavailable"}
    out["freeze"] = freeze
    if freeze.get("frozen") and (ticker or "").upper() in {
            t.upper() for t in (freeze.get("tickers") or [])}:
        out.update(blocked=True, blocked_reason=freeze.get("reason")
                   or "reconciliation freeze", blocking_failures=["reconciliation_freeze"])
        return out

    try:
        import account_gate
        import income_profile
        position = log.find_position(state, ticker) or {}
        gate = account_gate.evaluate(
            ticker, contracts=1, state=state,
            position_type=position_types.SHARES,
            stock_price=price_per_share,
            income_profile_tag=income_profile.of(position),
        )
    except Exception as exc:  # noqa: BLE001 — an un-evaluable gate is BLOCKED, never open
        out.update(blocked=True, blocked_reason=f"Level 5 gate unavailable: {exc}",
                   blocking_failures=["gate_unavailable"])
        return out

    out["gate"] = gate
    if gate.get("pass"):
        out["actionable"] = True
        return out
    failures = list(gate.get("blocking_failures") or [])
    labels = "; ".join(c["label"] for c in gate.get("checks") or []
                       if c.get("blocking") and not c.get("pass"))
    out.update(blocked=True, blocking_failures=failures,
               blocked_reason=f"Level 5 gate: {labels}" if labels else "Level 5 gate blocked")
    return out
