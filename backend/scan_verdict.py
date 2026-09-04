"""The scan DECISION layer — a thin veto set, a two-state verdict, and the entry route.

The scan used to answer "may I enter?" as a serial filter with eight veto levels
evaluated stop-on-first-fail. A serial filter's pass rate collapses
multiplicatively, and levels 2-4 screened for momentum LEADERSHIP — a screen for
appreciation — while an ITM covered call earns from time decay and sideways drift
with upside capped. Screening for the strongest movers systematically selected the
names most likely to blow through the strike. That was a structural mismatch, not a
calibration error.

This module is the replacement. The scan now answers "what is the best available
entry today?" — a thin hard floor here, plus a ranker in ``scan_score``.

THE GOVERNING PRINCIPLE
-----------------------
**The entry veto set equals the exit trigger set plus hard account constraints.
Everything else ranks. Nothing else blocks.** A condition that will not make you
EXIT a position has no business preventing you from ENTERING one. Every veto in
:data:`VETOES` names the exit rule it mirrors; a veto that cannot name one does not
belong here.

WHAT THIS MEANS IN PRACTICE

  * ``regime_red``       mirrors the CFM stated regime rule    [HARD_CFM_RULE]
  * ``rs3m_vs_spy``      mirrors ``kill_switch.classify``      [HARD_CFM_RULE]
  * ``close_below_ma50`` mirrors ``circuit_breaker`` fast-MA   [HARD_CFM_RULE]
  * ``close_below_ma200`` mirrors ``circuit_breaker`` slow-MA  [HARD_CFM_RULE]
  * ``line_in_the_sand`` mirrors ``circuit_breaker`` manual    [TRAVIS_EXTENSION]
  * the L5 account constraints and tradeability/staleness are hard account facts,
    not opinions about the chart.

Everything the old stack blocked on and this list omits — sector relative strength,
sector breadth, sector ATR expansion, the per-name four-light vote, RS3M-vs-SPY
MAGNITUDE above zero, ATR% of price, ATR vs its 5-EMA, extension above MA21,
structure entrability, and all five shadow features — is now a RANKING input in
``scan_score``. None of them may veto. **None may be reintroduced as a floor without
a separate reviewed change.**

RESIDUAL RISK, STATED PLAINLY
-----------------------------
RS3M-vs-SECTOR was removed system-wide on 2026-08-21
(``docs/decision-2026-08-21-remove-sector-rs.md``). With it gone, protection against
entering a sector LAGGARD now rests entirely on ``rs3m_vs_spy`` not being negative,
which is weaker than what the AAPL post-mortem concluded. Restoring RS-vs-sector on
an EQUAL-WEIGHTED benchmark would return to this veto list — as the exit mirror, per
the governing principle above — and is tracked separately. Do not paper over the gap
with a ranking weight; a rank cannot stop an entry.

PURITY
------
Everything here is a pure function of already-computed values. No I/O, no clock, no
provider access, no state load. The caller resolves the inputs (``metrics/scorecard``
for the chart values, ``account_gate.evaluate`` for the account overlay,
``data_cache`` for freshness) and passes them in.
"""
from __future__ import annotations

import config

# ---------------------------------------------------------------------------
# Verdict vocabulary — TWO states.
#
# CAUTION and WATCH were deleted with the filter they described. With a veto set
# this thin they no longer name anything the RANK does not: a name that clears
# every veto is entrable and its quality is a number, not a third adjective.
# ---------------------------------------------------------------------------
ELIGIBLE = "ELIGIBLE"
BLOCKED = "BLOCKED"

# The exhaustive veto registry: id -> (label, the exit rule or account constraint
# it mirrors). This tuple IS the contract — `evaluate` emits no id absent from it,
# and `test_scan_verdict` asserts the two stay in sync, so a veto cannot be added
# by editing one site.
VETOES = (
    ("regime_red", "Market regime RED", "CFM stated regime rule"),
    ("rs3m_vs_spy", "RS3M vs SPY negative", "kill switch"),
    ("close_below_ma50", "Close below the 50-day MA", "circuit breaker (fast MA)"),
    ("close_below_ma200", "Close below the 200-day MA", "circuit breaker (slow MA)"),
    ("line_in_the_sand", "Close at/below the stored line", "circuit breaker (manual)"),
    ("earnings_in_cycle", "Earnings inside the cycle", "CFM stated earnings rule"),
    ("no_weeklies", "No weekly options", "account constraint"),
    ("untradeable_spread", "Spread above the tradeability floor", "account constraint"),
    ("stale_inputs", "Input data stale or freshness unknown", "STALE_BLOCKS_GO"),
    # The Level-5 account overlay. Emitted with the account gate's own check id so
    # the blocked reason names the specific constraint (cash_reserve,
    # position_limit, capital_limit, sector_concentration, round_lot_size).
    ("account", "Account constraint", "account constraint"),
)

VETO_IDS = tuple(v[0] for v in VETOES)
_LABELS = {v[0]: v[1] for v in VETOES}
_MIRRORS = {v[0]: v[2] for v in VETOES}


def _block(veto_id: str, observed: dict, *, detail_id: str | None = None) -> dict:
    """One veto failure. ``detail_id`` narrows the account veto to its specific
    account-gate check without inventing a veto id outside the registry."""
    return {"id": detail_id or veto_id, "veto": veto_id,
            "label": _LABELS[veto_id], "mirrors": _MIRRORS[veto_id],
            "observed": observed}


def evaluate(*, regime_color: str | None = None,
             rs3m_vs_spy: float | None = None,
             below_ma50: bool | None = None,
             below_ma200: bool | None = None,
             price: float | None = None,
             line_in_the_sand: float | None = None,
             has_weeklies: bool | None = None,
             spread_pct: float | None = None,
             stale: bool | None = None,
             account_gate: dict | None = None) -> list[dict]:
    """Every FAILING veto for one candidate, as structured blocks. PURE.

    The returned list IS the authority-carrying ``blocks`` list. Nothing outside
    this function may append to it — that invariant is the whole safety property
    of the scan layer and is asserted by ``test_juice_capacity`` and
    ``test_scan_verdict``.

    MISSING-DATA POLICY, which differs by veto and is deliberate:

      * The three chart vetoes (``below_ma50`` / ``below_ma200`` / ``rs3m_vs_spy``)
        **fail open on None**. They mirror EXIT triggers, and the exit rules
        themselves do not fire on an unknown — ``kill_switch.classify`` holds green
        when ``rs_vs_spy`` is None, and ``circuit_breaker`` needs a close to compare.
        A veto that fired on absence would block every name with short history,
        which is the multiplicative collapse this redesign exists to remove.
      * ``stale`` **fails closed**: ``config.STALE_BLOCKS_GO`` is a HARD rule and
        unknown freshness has always read as stale — it never permits action.
      * ``has_weeklies`` blocks only on an explicit ``False``. Unknown is not a
        false hide; the weeklies probe is provider-dependent and often None.
    """
    blocks: list[dict] = []

    # 1. Regime RED [HARD_CFM_RULE]. YELLOW does NOT block an entry — it constrains
    #    the ROUTE (see `route`: a wobbling tape gets shares, never a put that
    #    commits capital a week out).
    if regime_color == "red":
        blocks.append(_block("regime_red", {"regime_color": regime_color}))

    # 2-4. The exit mirrors. Fail-open on None — see the missing-data policy above.
    if rs3m_vs_spy is not None and rs3m_vs_spy < 0:
        blocks.append(_block("rs3m_vs_spy", {"rs3m_vs_spy": rs3m_vs_spy}))
    if below_ma50 is True:
        blocks.append(_block("close_below_ma50", {"below_ma50": True}))
    if below_ma200 is True:
        blocks.append(_block("close_below_ma200", {"below_ma200": True}))

    # 5. The operator's stored line-in-the-sand, when one exists. A scan candidate
    #    with no position has no line, so this is inert for most rows — it binds a
    #    name the operator has already drawn a line under.
    if (line_in_the_sand is not None and price is not None
            and price <= line_in_the_sand):
        blocks.append(_block("line_in_the_sand",
                             {"price": price, "line": line_in_the_sand}))

    # 6. Tradeability. CFM sells a WEEKLY covered call, so a name with no weekly
    #    chain cannot be entered at all; a spread above the floor means the round
    #    trip eats the premium the trade exists to collect.
    if has_weeklies is False:
        blocks.append(_block("no_weeklies", {"has_weeklies": False}))
    if (spread_pct is not None
            and spread_pct > config.TRADEABILITY_MAX_SPREAD_PCT):
        blocks.append(_block("untradeable_spread",
                             {"spread_pct": spread_pct,
                              "floor": config.TRADEABILITY_MAX_SPREAD_PCT}))

    # 7. Staleness [HARD_CFM_RULE STALE_BLOCKS_GO] — fails CLOSED.
    if stale:
        blocks.append(_block("stale_inputs", {"stale": True}))

    # 8. The Level-5 account overlay + the earnings-in-cycle rule, both already
    #    evaluated by `account_gate.evaluate`. A READ of its blocking failures,
    #    never a re-evaluation — the executor enforces the same gate at the ticket,
    #    and two evaluations that could disagree would be worse than one.
    if account_gate:
        by_id = {c.get("id"): c for c in account_gate.get("checks") or []}
        for cid in account_gate.get("blocking_failures") or []:
            check = by_id.get(cid) or {}
            veto_id = "earnings_in_cycle" if cid == "earnings_in_cycle" else "account"
            blocks.append(_block(veto_id, check.get("detail") or {}, detail_id=cid))

    return blocks


def compose(blocks: list[dict] | None) -> dict:
    """The two-state verdict over the veto blocks. PURE.

    There is no severity ladder any more: a veto is a "no" and there is exactly one
    kind of no. ``blocked_by`` preserves registry order so the reason a reader sees
    first is stable across scans rather than depending on evaluation order.
    """
    blocks = list(blocks or [])
    if not blocks:
        return {"verdict": ELIGIBLE, "blocks": [], "blocked_by": []}
    order = {vid: i for i, vid in enumerate(VETO_IDS)}
    ordered = sorted(blocks, key=lambda b: (order.get(b.get("veto"), 99),
                                            str(b.get("id"))))
    return {"verdict": BLOCKED, "blocks": ordered,
            "blocked_by": [b["id"] for b in ordered]}


def is_eligible(verdict) -> bool:
    """Membership test for the ranked shortlist. Accepts the composed dict or the
    bare verdict string, so callers holding either shape ask the same question."""
    v = verdict.get("verdict") if isinstance(verdict, dict) else verdict
    return v == ELIGIBLE


# ---------------------------------------------------------------------------
# Entry route selection — ADVISORY OUTPUT ONLY.
#
# There is no put order construction and no execution path here or anywhere else
# in this change. `route` returns a recommendation the operator reads.
# ---------------------------------------------------------------------------
SHARES = "SHARES"
CASH_SECURED_PUT = "CASH_SECURED_PUT"


def route(*, extension_atr: float | None, regime_color: str | None = None,
          ma21: float | None = None) -> dict:
    """Which route enters an ELIGIBLE name: buy the shares, or sell a weekly put.

    The put is NOT a way to rescue an ineligible name — an ineligible name is
    BLOCKED and has no route. It is a route CHOICE **within** an eligible one:

      * near or below MA21          -> buy shares. There is no reason to wait a
                                       week for a fill that is available today.
      * extended beyond the threshold -> sell a weekly put struck at the MA21 zone.
                                       You are paid to wait for the price the
                                       strategy would rather pay.
      * regime YELLOW               -> shares only. A put commits capital a week
                                       out on a tape that is already wobbling.
      * regime RED                  -> the name is BLOCKED; no route is offered.

    Keyed off ``extension_atr`` — the SAME volatility-normalized extension the
    ranker consumes (``scan_score._extension_sub``), so the route and the rank can
    never disagree about how extended a name is. The threshold is
    ``config.CSP_ROUTE_ATR_EXTENSION_MAX``, its own constant, deliberately lower
    than (and no longer borrowed from) ``config.SPOT_ATR_EXTENSION_MAX`` — the old
    Level-4 right-spot veto bar. Splitting them means the put route can be
    loosened as an operator preference without also loosening the unrelated
    (shadow, no-authority) chart-structure extension display in
    ``chart_structure.py``.

    An unmeasurable extension routes to SHARES: the put route is the one that
    commits capital forward on a chart read, so an absent read takes the route that
    does not. PURE — no clock, no chain, no I/O.
    """
    threshold = config.CSP_ROUTE_ATR_EXTENSION_MAX
    if regime_color == "red":
        return {"route": None, "reason": "regime_red",
                "detail": {"regime_color": regime_color}}
    if extension_atr is None:
        return {"route": SHARES, "reason": "extension_unknown",
                "detail": {"extension_atr": None, "threshold": threshold}}
    extended = extension_atr > threshold
    if extended and regime_color == "yellow":
        return {"route": SHARES, "reason": "regime_yellow_shares_only",
                "detail": {"extension_atr": extension_atr, "threshold": threshold,
                           "regime_color": regime_color}}
    if extended:
        return {"route": CASH_SECURED_PUT, "reason": "extended_above_ma21",
                "detail": {"extension_atr": extension_atr, "threshold": threshold,
                           "target_strike_zone": ma21}}
    return {"route": SHARES, "reason": "near_or_below_ma21",
            "detail": {"extension_atr": extension_atr, "threshold": threshold}}


def put_collateral(strike: float | None, contracts: int = 1) -> float | None:
    """Cash-secured put collateral: ``strike x 100 x contracts``.

    This counts against the DEPLOYED-CAPITAL cap (``config.MAX_DEPLOYED_CAPITAL``)
    and does **not** draw from the ATR cash reserve — the reserve exists to defend
    open positions, and collateral is not a defence, it is the position. Keeping
    the two apart is why this is its own function rather than an inline product.
    """
    if strike is None or strike <= 0 or contracts <= 0:
        return None
    return round(float(strike) * config.SHARES_PER_LOT * int(contracts), 2)


def put_juice_pct(premium_per_share: float | None, strike: float | None) -> float | None:
    """The put's weekly yield on COLLATERAL: ``premium / (strike x 100)``, percent.

    A DIFFERENT denominator from the covered-call floor, which is share cost. The
    covered-call constant must not be reused here and is not
    (``config.PUT_JUICE_FLOOR_PCT`` vs ``config.SHARES_JUICE_FLOOR_PCT``): the two
    measure a yield on different capital and a shared bar would silently mean two
    different things. RANKING input only — like the covered-call floor it has no
    veto authority.
    """
    collateral = put_collateral(strike, 1)
    if premium_per_share is None or not collateral:
        return None
    return round(premium_per_share * config.SHARES_PER_LOT / collateral * 100, 4)


# The STRUCTURAL close triggers — a strict SUBSET of the veto set (§2.1).
#
# This is the most important distinction in the whole put lifecycle, so it is a
# named constant rather than a filter written at a call site. The ENTRY veto set
# is "the exit trigger set PLUS hard account constraints"; the CLOSE trigger set
# is the exit mirrors ONLY. The account and tradeability vetoes are entry
# constraints and are nonsense as close reasons:
#
#   * ``account`` — being at the position limit is not a reason to close a put you
#     already hold; closing it would not free a slot you were about to use, and it
#     would realize a loss to satisfy a bookkeeping cap.
#   * ``no_weeklies`` / ``untradeable_spread`` — a name that became untradeable is
#     a name you CANNOT close cheaply. Treating that as a close signal would
#     mandate crossing the exact spread that made it untradeable.
#   * ``stale_inputs`` — unknown data is a reason to withhold a GO, never a reason
#     to act. Closing on stale inputs would be trading on the absence of a signal.
#   * ``earnings_in_cycle`` — earnings inside the cycle blocks a NEW entry. On an
#     open put it is already priced in, and closing into the event realizes the
#     premium crush you were paid to accept.
#   * ``regime_red`` — a red tape blocks new entries [HARD_CFM_RULE]; it is not one
#     of the exit rules, and the exit rules are what this mirrors.
#
# What remains is exactly the four signals §2.1 enumerates. MA21 appears nowhere,
# which is what makes the "a close below MA21 must never close a put" guarantee
# structural rather than a matter of remembering.
PUT_CLOSE_TRIGGERS = ("close_below_ma50", "close_below_ma200",
                      "rs3m_vs_spy", "line_in_the_sand")


def put_close_triggers(*, ma_fast_breached: bool | None = None,
                       ma_slow_breached: bool | None = None,
                       rs3m_vs_spy: float | None = None,
                       line_breached: bool | None = None) -> list[dict]:
    """The structural signals that close an open put, as blocks. PURE.

    Note the 50-day leg: it is **three consecutive closes** below the 50-day
    (``circuit_breaker``'s ma_fast condition), NOT the single close the ENTRY veto
    uses. That asymmetry is deliberate and worth stating, because the two rules
    share a name and differ:

      * ENTRY asks "is this a good place to start" — one close below the 50-day is
        enough to wait, and waiting costs nothing.
      * CLOSING asks "is the thesis broken" — one close below the 50-day is noise
        that quality names in intact uptrends produce routinely, and acting on it
        would realize a loss on a position that recovers. Three consecutive closes
        is the circuit breaker's own bar, and the circuit breaker is what this
        mirrors.

    The caller supplies the already-evaluated conditions (``circuit_breaker
    .evaluate`` for the MA legs and the line, ``kill_switch.classify`` for RS), so
    there is exactly one definition of each rule in the codebase and this reads it.
    """
    blocks: list[dict] = []
    if ma_fast_breached:
        blocks.append(_block("close_below_ma50", {"consecutive_closes": True}))
    if ma_slow_breached:
        blocks.append(_block("close_below_ma200", {"below_ma200": True}))
    if rs3m_vs_spy is not None and rs3m_vs_spy < 0:
        blocks.append(_block("rs3m_vs_spy", {"rs3m_vs_spy": rs3m_vs_spy}))
    if line_breached:
        blocks.append(_block("line_in_the_sand", {"line_breached": True}))
    return blocks


# ---------------------------------------------------------------------------
# Tempo signal — 8/21 EMA cross. A FLAG AND NOTHING ELSE.
# ---------------------------------------------------------------------------
TEMPO_UP = "TEMPO_UP"
TEMPO_DOWN = "TEMPO_DOWN"


def tempo_signal(ema_fast: float | None, ema_slow: float | None) -> dict:
    """The 8/21 EMA relationship, as a DISPLAY flag. PURE.

    **This must never close a put and must never transition anything** (§2.1). It
    is a tempo read — how fast the name is moving relative to itself — and tempo is
    not thesis. The guarantee is structural rather than remembered: this function's
    output has no path into :func:`put_close_triggers` (which accepts no tempo
    argument) or into :data:`PUT_CLOSE_TRIGGERS` (which contains no tempo id), so
    there is no parameter through which a cross could become a close.

    Returned for the position card and the alert payload, read by nothing that
    decides anything.
    """
    if ema_fast is None or ema_slow is None:
        return {"signal": None, "ema_fast": ema_fast, "ema_slow": ema_slow,
                "tempo_only": True, "closes_nothing": True}
    return {
        "signal": TEMPO_UP if ema_fast > ema_slow else TEMPO_DOWN,
        "ema_fast": round(float(ema_fast), 4),
        "ema_slow": round(float(ema_slow), 4),
        # Both flags are literals, not config reads: there is no switch that can
        # give a tempo signal authority, and a reader can rely on that.
        "tempo_only": True,
        "closes_nothing": True,
    }


def put_close_advice(*, blocks: list[dict] | None) -> dict:
    """Whether an open put should be CLOSED rather than allowed to assign. PURE.

    Re-gate before assignment: the structural signals are re-evaluated DAILY while
    a put is open and MANDATORILY on expiry day before the close. The question is
    "would this name be admitted today?" — if yes, assignment is a good entry and
    the recommendation is to let it happen; if no, close the put rather than accept
    delivery of shares the entry rules would refuse.

    ``blocks`` must come from :func:`put_close_triggers`, NOT from :func:`evaluate`.
    The entry veto set is deliberately wider — see :data:`PUT_CLOSE_TRIGGERS` for
    why each account/tradeability/staleness veto is nonsense as a close reason.
    Any block whose id is outside the close-trigger set is IGNORED here rather than
    trusted, so passing the wrong list produces a hold, never a spurious close.

    **A close below MA21 is NOT a reversal and is NOT a close trigger.** MA21 is a
    timing reference — it is what put the name on this route in the first place,
    and a put struck at the MA21 zone is SUPPOSED to be approached. Quality names
    in intact uptrends dip below it routinely. MA21 appears nowhere in
    :data:`PUT_CLOSE_TRIGGERS`, so this cannot fire on it even by accident.

    Rolling a short put down or out is deliberately NOT offered anywhere in this
    codebase. A put roll is a DEBIT and a Martingale structure: it converts a
    bounded mistake into an unbounded one by paying to take on more downside in the
    same name. If the position needs defending, CLOSE it.
    """
    structural = [b for b in (blocks or [])
                  if b.get("veto") in PUT_CLOSE_TRIGGERS]
    if not structural:
        return {"action": "hold", "reason": "still_eligible", "blocked_by": [],
                "assignment_is_a_good_entry": True}
    order = {vid: i for i, vid in enumerate(PUT_CLOSE_TRIGGERS)}
    structural.sort(key=lambda b: order.get(b.get("veto"), 99))
    return {"action": "close", "reason": "structural_break",
            "blocked_by": [b["id"] for b in structural],
            "assignment_is_a_good_entry": False}
