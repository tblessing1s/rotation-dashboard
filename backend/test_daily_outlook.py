"""The daily outlook digest — regime, price, distance to strike, DTE.

The one INFORMATIONAL alert in the engine. Every other evaluator fires because a
condition became true and something needs doing; this one fires because the
operator asked to be told where things stand.

The subtle part is delivery. `alerts.run` is a state machine over ACTIVE
CONDITIONS, not a message queue: an alert fires once when its fingerprint first
appears, then only refreshes `last_seen` for as long as it keeps firing, and only
NEW alerts are pushed. A digest with a stable fingerprint would therefore be
delivered exactly once, ever, and silently degrade into a one-off. Keying the
fingerprint by the date is what makes it daily — and reuses the existing dedup,
scheduler and notifier rather than growing a second delivery path.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import alerts


@pytest.fixture()
def regime(monkeypatch):
    import screening
    monkeypatch.setattr(screening, "regime", lambda: {
        "published_regime": "yellow",
        "lights": {"close_vs_ma": {"signal": "green"},
                   "fast_vs_slow": {"signal": "green"},
                   "sar": {"signal": "red"}, "momentum": {"signal": "red"}}})


@pytest.fixture()
def prices(monkeypatch):
    monkeypatch.setattr(alerts, "_last_close",
                        lambda t: {"AAA": 142.94, "BBB": 51.20}.get(t))


def _exp(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _book(*legs):
    return {"positions": [{"ticker": t, "status": "active",
                           "shares": {"count": 100 if kind == "call" else 0},
                           "short_calls": [leg] if kind == "call" else [],
                           "short_puts": [leg] if kind == "put" else []}
                          for t, kind, leg in legs]}


# ---------------------------------------------------------------------------
# It must actually be DAILY
# ---------------------------------------------------------------------------
def test_the_fingerprint_is_keyed_by_the_date(regime, prices):
    """Without the date the dedup fires it once and then only refreshes
    last_seen — a daily report silently becomes a one-off."""
    a = alerts.check_daily_outlook({"positions": []})[0]
    assert a["fingerprint"] == f"DAILY_OUTLOOK|{date.today().isoformat()}"


def test_repeated_slots_in_one_day_produce_one_fingerprint(regime, prices):
    """The evaluator runs at every slot (08:30, 10:00, 12:30, 15:30, 16:15). All
    five must dedup to a single alert, so the operator is pushed once."""
    fps = {alerts.check_daily_outlook({"positions": []})[0]["fingerprint"]
           for _ in range(5)}
    assert len(fps) == 1


def test_it_is_informational_and_demands_nothing(regime, prices):
    """It must sort under every real alert in a batched push, and must not read
    as an instruction — a digest that looks like a trigger trains the operator to
    ignore triggers."""
    a = alerts.check_daily_outlook({"positions": []})[0]
    assert a["severity"] == "LOW"
    assert a["ticker"] is None
    assert "not a trigger" in a["action"]


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------
def test_it_leads_with_the_regime(regime, prices):
    msg = alerts.check_daily_outlook({"positions": []})[0]["message"]
    assert msg.startswith("Regime YELLOW (2/4 lights)")


def test_a_flat_book_still_reports_the_regime(regime, prices):
    """The regime read is useful with nothing on — it is what decides whether to
    put something on."""
    msg = alerts.check_daily_outlook({"positions": []})[0]["message"]
    assert "no open positions" in msg


def test_a_covered_call_reports_price_cushion_and_dte(regime, prices):
    msg = alerts.check_daily_outlook(
        _book(("AAA", "call", {"strike": 139.0, "expiration": _exp(4)})))[0]["message"]
    assert "AAA $142.94" in msg          # the price
    assert "call 139.0" in msg           # the strike
    assert "2.8% ITM cushion" in msg     # how close to it
    assert "4d left" in msg              # how close to DTE


def test_the_sign_means_opposite_things_on_the_two_sides(regime, prices):
    """Same arithmetic, opposite meaning. A covered call is sold ITM ON PURPOSE —
    spot above the strike is the design and spot falling THROUGH it is the defend
    trigger. A put is the mirror: below the strike is assignment territory."""
    call = alerts.check_daily_outlook(
        _book(("AAA", "call", {"strike": 139.0, "expiration": _exp(4)})))[0]["message"]
    put = alerts.check_daily_outlook(
        _book(("BBB", "put", {"strike": 55.0, "expiration": _exp(4)})))[0]["message"]
    assert "ITM cushion" in call and "assignment" not in call
    assert "through the strike — assignment likely" in put


def test_a_call_below_its_strike_reads_as_the_defend_zone(regime, prices):
    msg = alerts.check_daily_outlook(
        _book(("AAA", "call", {"strike": 150.0, "expiration": _exp(4)})))[0]["message"]
    assert "BELOW strike — defend zone" in msg


def test_expiry_day_is_called_out_in_words(regime, prices):
    """'0d left' is easy to skim past on a lock screen; expiry day is the one day
    where doing nothing has a consequence that cannot be undone."""
    msg = alerts.check_daily_outlook(
        _book(("AAA", "call", {"strike": 139.0, "expiration": _exp(0)})))[0]["message"]
    assert "expires TODAY" in msg


def test_a_stored_dte_is_preferred_over_recomputing(regime, prices):
    msg = alerts.check_daily_outlook(
        _book(("AAA", "call", {"strike": 139.0, "expiration": _exp(4), "dte": 2})))[0]["message"]
    assert "2d left" in msg


def test_missing_numbers_say_unknown_rather_than_zero(regime, monkeypatch):
    """A zero distance reads as 'at the strike', which is a specific and alarming
    claim. Absence must not be reported as a value."""
    monkeypatch.setattr(alerts, "_last_close", lambda t: None)
    msg = alerts.check_daily_outlook(
        _book(("AAA", "call", {"strike": 139.0})))[0]["message"]
    assert "price unavailable" in msg
    assert "distance unknown" in msg and "DTE unknown" in msg


def test_a_position_with_no_short_is_reported_not_skipped(regime, prices):
    """Shares held with nothing sold against them is the engine idling — worth
    seeing in the daily read."""
    state = {"positions": [{"ticker": "AAA", "status": "active",
                            "shares": {"count": 100}, "short_calls": [],
                            "short_puts": []}]}
    assert "no open short" in alerts.check_daily_outlook(state)[0]["message"]


def test_a_broken_regime_read_does_not_sink_the_digest(regime, prices, monkeypatch):
    """A digest that raises would take the whole alert run down with it, killing
    the CRITICAL alerts that share the pass."""
    import screening
    monkeypatch.setattr(screening, "regime",
                        lambda: (_ for _ in ()).throw(RuntimeError("provider down")))
    msg = alerts.check_daily_outlook({"positions": []})[0]["message"]
    assert "Regime UNKNOWN" in msg


def test_it_is_registered_first_so_it_cannot_be_lost(regime):
    assert alerts.check_daily_outlook in alerts.EVALUATORS
    assert "DAILY_OUTLOOK" in alerts.ALERT_TYPES
