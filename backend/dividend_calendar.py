"""Ex-dividend calendar — the internal data contract and its provider adapters.

Why this module exists: the early-assignment guard (``alerts.check_assignment_risk``
/ ``position_manager.short_call_health``) is only as good as the ex-date and amount
feeding it, and those were previously read straight out of a pile of best-effort
provider key-name guesses inline in ``dividends._fetch_event``. This module makes
the CONTRACT explicit and puts each provider behind an adapter that must satisfy
it, so a wrong guess is a failing adapter rather than a silently-empty guard.

THE CONTRACT
------------
Every adapter returns a ``DividendEvent``-shaped dict, or ``None`` when it has
nothing (never a partially-invented record):

    {
      "ex_date":  "YYYY-MM-DD" | None,  # the date the stock trades WITHOUT the
                                        # dividend. This is the assignment-risk
                                        # date — NOT the pay date, NOT the record
                                        # date. Getting this wrong by even a day
                                        # makes the guard fire late, which is the
                                        # same as not firing.
      "pay_date": "YYYY-MM-DD" | None,  # when cash actually arrives. Used to book
                                        # DIVIDEND income; never for the guard.
      "amount":   float | None,          # PER SHARE, PER PAYMENT, in dollars —
                                        # NOT annualized, NOT a percent yield.
                                        # Compared directly against the short
                                        # call's per-share extrinsic, so the units
                                        # must match exactly.
      "frequency": int | None,           # payments per year (4 = quarterly), only
                                        # used to derive a per-payment amount from
                                        # an annual figure.
      "source":   str,                   # provenance: which adapter produced this.
    }

Invariants an adapter MUST honor:
  * Never annualize into ``amount``, and never put a yield there. If only an
    annual figure is available, divide by ``frequency`` (defaulting to quarterly)
    and say so via ``source``.
  * Never substitute today, the pay date, or the record date for a missing
    ``ex_date``. A missing ex-date must stay None: the guard treats "unknown" as
    "no dividend risk", and a fabricated date would make it fire on the wrong day.
  * Never raise. A provider outage degrades to None; dividend data must never
    break a risk path or block an entry.

ADAPTERS
--------
  * ``alpha_vantage_adapter``  — WIRED. ``OVERVIEW.ExDividendDate`` and
    ``OVERVIEW.DividendPerShare``; the latter is ANNUAL, so it is divided by the
    payment frequency. This is the only adapter whose field names are confirmed.
  * ``fixture_adapter``        — WIRED. Reads an operator/test-supplied mapping, so
    the whole guard is exercisable offline with no provider at all.
  * ``schwab_adapter``         — **TODO, deliberately not implemented.** See below.

SCHWAB ADAPTER — TODO
---------------------
Not implemented, and deliberately NOT guessed. The Schwab fundamentals payload's
ex-dividend field names are unconfirmed against a live account; the candidates
previously tried inline were ``nextDivExDate`` / ``divExDate`` / ``dividendDate`` /
``divDate`` for the date and ``divPayAmount`` / ``divAmount`` / ``divFreq`` for the
amount, none verified. Shipping a guess here would be worse than shipping nothing:
a wrong key silently yields None, the guard goes quiet, and the failure looks
exactly like "this stock pays no dividend".

To implement:
  1. Dump a live ``get_instrument_fundamental`` payload for a known quarterly payer
     (KO or JNJ) and record which keys are actually present.
  2. Confirm the UNITS of the amount field — per-payment or annual — against a
     known dividend (KO pays ~$0.485/quarter, ~$1.94/year; the two are
     unmistakable).
  3. Confirm the DATE semantics: ``dividendDate`` in particular is ambiguous across
     provider vintages and is often the PAY date, not the ex-date. Cross-check
     against the same name's Alpha Vantage ``ExDividendDate``.
  4. Only then implement ``schwab_adapter`` to the contract above and add it to
     ``ADAPTERS`` ahead of Alpha Vantage.

Until then ``dividends._fetch_event`` keeps its existing best-effort Schwab probe
(unchanged, still flagged LIVE_VERIFY) as a pre-step, and this module is the
contract-checked path behind it.

PURE-ish: adapters do provider I/O, but ``normalize`` and ``merge`` are pure and
are where every unit/shape rule is enforced.
"""
from __future__ import annotations

EMPTY = {"ex_date": None, "pay_date": None, "amount": None,
         "frequency": None, "source": "none"}


def _clean_date(value) -> str | None:
    """A ``YYYY-MM-DD`` date, or None. Never substitutes a fallback.

    Delegates to ``logging_handler._parse_ymd``, the codebase's single strict
    date parser (CLAUDE.md: never re-derive a bespoke one). It already rejects
    every provider sentinel that would otherwise read as a real date — 'None',
    '', '0000-00-00', 'N/A' and out-of-range values all raise and become None."""
    import logging_handler as log
    return None if log._parse_ymd(value) is None else str(value)[:10]


def _clean_amount(value) -> float | None:
    """A positive per-share dollar amount, or None. A zero or negative figure is
    not a dividend; a NaN is provider junk."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount != amount or amount <= 0:
        return None
    return round(amount, 4)


def per_payment_amount(annual, frequency) -> float | None:
    """Derive a PER-PAYMENT amount from an ANNUAL one. Defaults to quarterly when
    the provider omits the frequency — the overwhelmingly common case for US
    payers, and the conservative direction: assuming 4 payments understates each
    one, which makes the assignment guard fire EARLIER, never later."""
    annual = _clean_amount(annual)
    if annual is None:
        return None
    try:
        freq = int(frequency) or 4
    except (TypeError, ValueError):
        freq = 4
    if freq <= 0:
        freq = 4
    return round(annual / freq, 4)


def normalize(ex_date=None, pay_date=None, amount=None, frequency=None,
              source: str = "none") -> dict:
    """Coerce raw adapter output to the contract. The single place the shape and
    the units are enforced — every adapter returns through this. PURE."""
    return {
        "ex_date": _clean_date(ex_date),
        "pay_date": _clean_date(pay_date),
        "amount": _clean_amount(amount),
        "frequency": frequency if isinstance(frequency, int) and frequency > 0 else None,
        "source": source,
    }


def is_empty(event: dict | None) -> bool:
    """True when an event carries nothing usable — no ex-date AND no amount."""
    return not event or (event.get("ex_date") is None and event.get("amount") is None)


def merge(*events: dict | None) -> dict:
    """First-non-empty-wins across adapters, field by field, so a provider that
    knows the date but not the amount can be completed by the next one. Provenance
    records every adapter that contributed. PURE."""
    out = dict(EMPTY)
    contributors: list[str] = []
    for event in events:
        if not event:
            continue
        used = False
        for field in ("ex_date", "pay_date", "amount", "frequency"):
            if out.get(field) is None and event.get(field) is not None:
                out[field] = event[field]
                used = True
        if used and event.get("source"):
            contributors.append(event["source"])
    out["source"] = "+".join(contributors) if contributors else "none"
    return out


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
def fixture_adapter(ticker: str, fixtures: dict | None = None) -> dict | None:
    """A fixture/override-backed adapter — the offline path.

    ``fixtures`` maps TICKER -> a partial event dict. Used by the test suite and by
    the operator's ``state.metadata.dividend_event_overrides``, so the guard is
    fully exercisable with no provider configured at all."""
    if not fixtures:
        return None
    raw = fixtures.get((ticker or "").strip().upper()) or fixtures.get(ticker)
    if not isinstance(raw, dict):
        return None
    return normalize(ex_date=raw.get("ex_date"), pay_date=raw.get("pay_date"),
                     amount=raw.get("amount"), frequency=raw.get("frequency"),
                     source="fixture")


def alpha_vantage_adapter(ticker: str) -> dict | None:
    """Alpha Vantage OVERVIEW. ``ExDividendDate`` is a true ex-date;
    ``DividendPerShare`` is ANNUAL despite the name, so it is divided by the payment
    frequency to reach the per-payment amount the contract requires."""
    import alpha_vantage
    if not alpha_vantage.configured():
        return None
    try:
        overview = alpha_vantage.overview(ticker) or {}
    except Exception:  # noqa: BLE001 — a provider outage degrades to "unknown"
        return None
    amount = per_payment_amount(overview.get("DividendPerShare"), 4)
    event = normalize(ex_date=overview.get("ExDividendDate"),
                      pay_date=overview.get("DividendDate"),
                      amount=amount, frequency=4, source="alpha_vantage")
    return None if is_empty(event) else event


def schwab_adapter(ticker: str) -> dict | None:
    """NOT IMPLEMENTED — see the module docstring's "SCHWAB ADAPTER — TODO".

    Returns None unconditionally and on purpose. The field names and units are
    unconfirmed against a live account, and a wrong guess here is indistinguishable
    from "this stock pays no dividend" — which silently disarms the early-assignment
    guard. Implement only after steps 1-4 in the docstring are done."""
    return None


# Adapter order — highest-confidence first. Schwab is listed so the TODO is
# visible in the resolution order it will occupy, and is a no-op until implemented.
ADAPTERS = (schwab_adapter, alpha_vantage_adapter)


def next_dividend(ticker: str, fixtures: dict | None = None) -> dict:
    """Resolve the next dividend event for a ticker to the contract shape.

    Fixtures/overrides win outright (the operator's word, and the offline test
    path); otherwise the adapters are merged in confidence order. Always returns a
    contract-shaped dict — never raises, never invents a date."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return dict(EMPTY)
    fixture = fixture_adapter(ticker, fixtures)
    if not is_empty(fixture):
        return fixture
    events = []
    for adapter in ADAPTERS:
        try:
            events.append(adapter(ticker))
        except Exception:  # noqa: BLE001 — never let one adapter sink the calendar
            continue
    return merge(*events)
