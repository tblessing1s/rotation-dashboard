"""Pure order-price construction + pre-submit validation (incident hotfix, D1).

Everything here is a pure function over plain numbers/dicts — no I/O, no clock, no
broker — so it is exercisable offline and the forthcoming order-lifecycle system can
adopt it unchanged. The executor does the I/O (fetch quotes, place the order) and
calls these to build a price it can trust or a list of reasons to refuse.

The load-bearing rules (from the incident audit):

  * All order prices are ``Decimal``, rounded to the instrument's valid tick via
    ``round_to_tick`` — never a bare ``round(x, 2)`` that can land off-tick.
  * Net direction (NET_CREDIT vs NET_DEBIT) and the price sign are derived from the
    legs in ONE place (``net_credit_debit``); a contradiction between the computed
    direction and what a caller intended is an assertion failure, not a submission.
  * A price is refused BEFORE construction unless every leg has a two-sided, nonzero,
    fresh quote (``validate_quote_freshness``). A price derived from a missing /
    one-sided / stale / zero quote is never returned.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import config

NET_CREDIT = "NET_CREDIT"
NET_DEBIT = "NET_DEBIT"

# Two cents of precision on the wire; ticks are coarser than this and quantizing to
# 2dp gives Schwab a clean string with no binary-float artifact (D1(d)).
_CENT = Decimal("0.01")


def _dec(value) -> Decimal:
    """Coerce to Decimal via str (so 2.35 doesn't arrive as 2.350000000000000088…).
    Raises for None/non-numeric — callers validate presence first."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def tick_for_price(price) -> Decimal:
    """The valid price increment for an option quoted at ``price`` (absolute value),
    per config's provenance-tagged tick table. $0.01 below the breakpoint, $0.05
    at/above it. LIVE_VERIFY: penny-pilot names quote finer; this returns the
    conservative venue minimum so a limit is never rejected for being too fine."""
    p = abs(_dec(price))
    brk = _dec(config.OPTION_TICK_BREAKPOINT)
    return _dec(config.OPTION_TICK_BELOW) if p < brk else _dec(config.OPTION_TICK_ABOVE)


def round_to_tick(price, tick=None) -> Decimal:
    """Round ``price`` to the nearest multiple of ``tick`` (ties away from zero),
    returned as a 2dp Decimal. ``tick`` defaults to ``tick_for_price(price)``.

    Sign-preserving and symmetric: round_to_tick(-2.37) mirrors round_to_tick(2.37).
    This is the ONLY place order prices are snapped to a valid increment."""
    d = _dec(price)
    t = _dec(tick) if tick is not None else tick_for_price(d)
    if t <= 0:
        raise ValueError(f"tick must be positive, got {t}")
    # Round the multiple-count half-up (away from zero for .5), then re-scale. The
    # count is exact in Decimal, so no float artifact leaks into the result.
    steps = (d / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * t).quantize(_CENT)


def net_credit_debit(buyback_price, new_premium, tick=None):
    """Compute a roll's net limit from its two legs in ONE place — the single source
    of direction. Returns ``(abs_price: Decimal, order_type: str)`` where the price is
    the tick-rounded magnitude and order_type is NET_CREDIT/NET_DEBIT.

    Convention (matches build_roll_order): the roll BUYS to close the old short
    (pays ``buyback_price``) and SELLS to open the new one (receives ``new_premium``).
    net = new_premium − buyback_price; net >= 0 is a credit received, net < 0 a debit
    paid. The magnitude is what Schwab's NET order price field carries."""
    net = _dec(new_premium) - _dec(buyback_price)
    net = round_to_tick(net, tick)
    order_type = NET_CREDIT if net >= 0 else NET_DEBIT
    return abs(net).quantize(_CENT), order_type


def assert_direction(order_type: str, expected_direction: Optional[str]) -> None:
    """Guard the D1(b) contradiction: if a caller carries an independently-known
    intended direction (e.g. the mid the operator staged implies a credit), the
    constructed order's derived orderType MUST agree. A mismatch means the price
    math and the intent disagree — an assertion failure, never a submission."""
    if expected_direction in (NET_CREDIT, NET_DEBIT) and order_type != expected_direction:
        raise AssertionError(
            f"order direction contradiction: computed {order_type} but the staged "
            f"legs imply {expected_direction} — refusing to submit a flipped-direction "
            "order (re-check the quotes)")


def format_price(price) -> str:
    """The exact string Schwab receives for a price: a 2dp Decimal, no float artifact.
    Accepts a Decimal or number; the magnitude is taken (order side lives in
    orderType, not the sign)."""
    return f"{abs(_dec(price)).quantize(_CENT)}"


# ---------------------------------------------------------------------------
# Pre-submit quote validation (pure: quotes in -> [] ok, or [reasons])
# ---------------------------------------------------------------------------
def _leg_quote_problem(label: str, symbol: str, quote: Optional[dict],
                       now_ms: Optional[float], max_age_s: float) -> Optional[str]:
    """One leg's rejection reason, or None if the quote is fit to price an order.
    Requires a two-sided, strictly-positive bid AND ask; when the quote carries a
    timestamp, also requires it to be no older than ``max_age_s`` (a missing
    timestamp cannot prove staleness and is a LIVE_VERIFY gap, not a hard reject)."""
    who = f"{label} leg ({symbol})"
    if not quote:
        return f"no quote returned for the {who} — cannot price the order"
    bid = quote.get("bid")
    ask = quote.get("ask")
    if bid is None or ask is None:
        return (f"one-sided quote on the {who}: bid={bid!r} ask={ask!r} — a mid from a "
                "one-sided book is not a real price")
    try:
        bid_f, ask_f = float(bid), float(ask)
    except (TypeError, ValueError):
        return f"non-numeric quote on the {who}: bid={bid!r} ask={ask!r}"
    if bid_f <= 0 or ask_f <= 0:
        return (f"zero/negative quote on the {who}: bid={bid_f} ask={ask_f} — refusing to "
                "derive a price from it")
    if ask_f < bid_f:
        return f"crossed quote on the {who}: bid={bid_f} > ask={ask_f}"
    qt = quote.get("quoteTimeMs")
    if qt is not None and now_ms is not None:
        try:
            age_s = (float(now_ms) - float(qt)) / 1000.0
        except (TypeError, ValueError):
            age_s = None
        if age_s is not None and age_s > max_age_s:
            return (f"stale quote on the {who}: {age_s:.0f}s old > "
                    f"{max_age_s:.0f}s tolerance — refusing to price off it")
    return None


def validate_roll_quotes(close_quote: Optional[dict], new_quote: Optional[dict], *,
                         close_symbol: str, new_symbol: str,
                         now_ms: Optional[float] = None,
                         max_age_s: Optional[float] = None) -> list[str]:
    """Pure pre-submit gate for a roll: returns [] when BOTH legs carry a two-sided,
    nonzero, fresh quote, else a list of specific, leg-named rejection reasons. The
    executor refuses to construct the order when this is non-empty (never submits a
    price derived from a bad quote). Shape (quotes in -> ok/reasons out) is exactly
    what the lifecycle system's pre-submit validator will consume."""
    if max_age_s is None:
        max_age_s = float(config.QUOTE_MAX_AGE_FOR_ORDER_SECONDS)
    reasons = []
    for label, symbol, quote in (
        ("buy-to-close (old short)", close_symbol, close_quote),
        ("sell-to-open (new short)", new_symbol, new_quote),
    ):
        problem = _leg_quote_problem(label, symbol, quote, now_ms, max_age_s)
        if problem:
            reasons.append(problem)
    return reasons


def quote_mid(quote: dict):
    """Tick-rounded mid of a validated two-sided quote, as a Decimal. Assumes the
    quote already passed validate_roll_quotes (two-sided, nonzero)."""
    bid = _dec(quote["bid"])
    ask = _dec(quote["ask"])
    return round_to_tick((bid + ask) / 2)
