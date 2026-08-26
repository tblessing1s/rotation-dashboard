"""Cash-secured put lifecycle, Stage 1 — TRACK (schema v22).

Stage 1's whole purpose is that a put opened MANUALLY in Schwab is correctly
represented, valued and reconciled. So the assertions that matter most are not
the arithmetic — they are the ones about what the application does when it is
WRONG about a put:

  * ``test_an_unrecognized_assignment_like_transaction_is_loud`` — the failure
    mode that costs real money. If assignment is not detected, the app believes
    it holds cash and collateral while the account holds 100 shares and no put;
    the covered-call machinery never engages and the shares sit uncovered.
  * ``test_an_open_put_is_visible_to_reconciliation`` — before v22 a put was
    caught only because it was foreign to state entirely. Modelling it without
    emitting it to the expected view would have spent that accident and replaced
    it with silence.
  * the not-applicable tests — a zero where the answer is "undefined" is the
    quiet version of the same class of bug.

Offline throughout: no provider, no network, a tmp_path store.
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-csp-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import position_manager as pm  # noqa: E402
import position_types  # noqa: E402
import reconcile  # noqa: E402
import scan_verdict as sv  # noqa: E402
import transaction_ingest as ti  # noqa: E402

from datetime import date as _date

_DAY = _date(2020, 2, 1)

STRIKE = 50.0
EXPIRY = "2026-09-18"
PREMIUM = 0.60


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    st = log.load_state()
    st["metadata"]["operating_cash"] = 100000
    log.save_state(st)
    return tmp_path


def _open_put(ticker="AAA", contracts=1, spot=53.0):
    return executor.execute({
        "action": "put_opened", "ticker": ticker, "strike": STRIKE,
        "contracts": contracts, "expiration": EXPIRY,
        "premium_per_share": PREMIUM, "stock_price": spot,
        "regime_at_entry": "green", "extension_from_ma21": 2.1,
        "collateral_venue": "sweep", "collateral_yield_pct": 0.05,
    })


def _position(ticker="AAA"):
    return log.find_position(log.load_state(), ticker)


# ---------------------------------------------------------------------------
# §1.5 — a put opened externally appears with correct collateral, extrinsic, state
# ---------------------------------------------------------------------------
def test_an_externally_opened_put_is_represented(store):
    _open_put()
    p = _position()
    assert position_types.of(p) == position_types.CASH_SECURED_PUT
    assert position_types.is_put(p) is True
    leg = p["short_puts"][0]
    assert leg["strike"] == STRIKE and leg["contracts"] == 1
    assert leg["collateral"] == 5000.0            # 50 x 100 x 1


def test_collateral_and_yield_reuse_the_scan_helpers_not_a_second_formula(store):
    """One definition, two callers. A second copy would be the first place the
    advisory figure (what the route selector says a put would tie up) and the
    booked figure could drift apart."""
    _open_put()
    e = [x for x in log.load_state()["executions"] if x["action"] == "put_opened"][0]
    assert e["collateral"] == sv.put_collateral(STRIKE, 1)
    assert e["yield_on_collateral_pct"] == sv.put_juice_pct(PREMIUM, STRIKE)


def test_premium_is_recorded_separately_and_never_netted_into_basis(store):
    """[HARD_CFM_RULE] Extrinsic is INCOME and stays visible as income."""
    _open_put()
    e = [x for x in log.load_state()["executions"] if x["action"] == "put_opened"][0]
    assert e["premium_total"] == 60.0            # 0.60 x 100 x 1
    assert e["premium_per_share"] == PREMIUM
    # The premium lives on its own field. Nothing on the record is strike-minus-
    # premium, which is what netting would produce.
    assert 49.40 not in e.values()


def test_the_entry_provenance_is_frozen_at_open(store):
    _open_put()
    e = [x for x in log.load_state()["executions"] if x["action"] == "put_opened"][0]
    assert e["entry_route"] == "csp"
    assert e["regime_at_entry"] == "green"
    assert e["extension_from_ma21"] == 2.1
    # Collateral venue + yield: idle cash at a sweep rate vs a money-market fund
    # is a real cost that is otherwise invisible. Recorded, never guessed.
    assert e["collateral_venue"] == "sweep" and e["collateral_yield_pct"] == 0.05


def test_valuation_splits_intrinsic_from_extrinsic(store, monkeypatch):
    """ONLY extrinsic may reach the juice ledger. Intrinsic on a short put is a
    share-purchase obligation, not income."""
    _open_put()
    monkeypatch.setattr(pm, "_stock_price", lambda t: 47.0)     # ITM by 3.00
    monkeypatch.setattr(pm, "_put_mark_per_share", lambda t, leg, spot: 3.40)
    leg = pm.enrich_position(_position())["short_puts"][0]
    assert leg["intrinsic_per_share"] == 3.0                    # 50 - 47
    assert leg["extrinsic_per_share"] == pytest.approx(0.40)    # mark - intrinsic
    assert leg["itm"] is True


def test_an_unpriceable_put_reads_unknown_not_zero_extrinsic(store, monkeypatch):
    _open_put()
    monkeypatch.setattr(pm, "_stock_price", lambda t: None)
    leg = pm.enrich_position(_position())["short_puts"][0]
    assert leg["extrinsic_per_share"] is None
    assert leg["mark_per_share"] is None


# ---------------------------------------------------------------------------
# §1.5 — deployed capital includes collateral; reserve is untouched
# ---------------------------------------------------------------------------
def test_collateral_counts_against_the_deployed_capital_cap(store):
    before = pm.deployed_capital(log.load_state())
    _open_put()
    after = pm.deployed_capital(log.load_state())
    assert after - before == 5000.0


def test_collateral_does_not_draw_the_cash_reserve(store):
    """Structural, not conventional: the reserve is a formula over ATR
    (RESERVE_ATR_MULT x ATR x contracts x 100) and is not computed from deployed
    capital at all, so a collateral term cannot reach it."""
    import account_gate
    import inspect
    src = inspect.getsource(account_gate)
    reserve_block = src[src.index("cash_reserve"):src.index("cash_reserve") + 2000]
    assert "deployed_capital" not in reserve_block
    assert "collateral" not in reserve_block


# ---------------------------------------------------------------------------
# §1.5 — position count and sector limits count the put
# ---------------------------------------------------------------------------
def test_an_open_put_counts_as_a_position_and_holds_its_sector(store):
    _open_put()
    state = log.load_state()
    open_positions = [p for p in state["positions"] if p.get("status") != "closed"]
    assert len(open_positions) == 1
    # The account gate counts `state["positions"]` where status != closed, so the
    # put is counted by both limits with no put-specific code.
    assert open_positions[0]["ticker"] == "AAA"
    assert position_types.is_put(open_positions[0])


# ---------------------------------------------------------------------------
# §1.5 — share-based metrics render NOT-APPLICABLE, never zero
# ---------------------------------------------------------------------------
def test_share_readouts_are_not_applicable_never_zero(store):
    _open_put()
    enriched = pm.enrich_position(_position())
    sh = enriched["shares"]
    assert sh["not_applicable"] is True
    # Every share readout is None. A 0 here would render a cap meter at 0% and a
    # coverage guardrail that looks satisfied.
    for key in ("count", "cap", "pct_to_cap", "locked", "coverable_lots",
                "fragment_shares"):
        assert sh[key] is None, key


def test_coverage_is_undefined_for_a_put_not_zero(store):
    _open_put()
    cov = pm.delta_coverage(_position(), 47.0)
    assert cov["assessable"] is False and cov["not_applicable"] is True
    assert cov["coverable_lots"] is None and cov["short_contracts"] is None
    assert cov["naked_short"] is None      # not False — the question does not apply


# ---------------------------------------------------------------------------
# §1.5 — assignment → shares at the strike → covered-call machinery engages
# ---------------------------------------------------------------------------
def test_assignment_converts_collateral_into_shares_at_the_strike(store):
    _open_put()
    executor.execute({"action": "put_assigned", "ticker": "AAA", "strike": STRIKE,
                      "contracts": 1, "expiration": EXPIRY, "stock_price": 47.0})
    p = _position()
    # THE HANDOFF: an ordinary SHARES position from here on.
    assert position_types.of(p) == position_types.SHARES
    assert p["shares"]["count"] == 100
    # Basis is the STRIKE, not the strike net of premium.
    assert p["shares"]["cost_basis_per_share"] == 50.0
    assert p["short_puts"] == []


def test_the_covered_call_machinery_engages_after_assignment_unmodified(store):
    _open_put()
    executor.execute({"action": "put_assigned", "ticker": "AAA", "strike": STRIKE,
                      "contracts": 1, "expiration": EXPIRY, "stock_price": 47.0})
    # Share readouts are DEFINED again, and one lot is coverable — the machinery
    # needs no knowledge that a put was ever involved.
    enriched = pm.enrich_position(_position())
    assert enriched["shares"].get("not_applicable") is not True
    assert enriched["shares"]["coverable_lots"] == 1
    cov = pm.delta_coverage(_position(), 47.0)
    assert cov["assessable"] is True and cov["coverable_lots"] == 1


def test_premium_survives_assignment_as_realized_income(store):
    _open_put()
    executor.execute({"action": "put_assigned", "ticker": "AAA", "strike": STRIKE,
                      "contracts": 1, "expiration": EXPIRY, "stock_price": 47.0})
    ledger = log.load_state()["put_ledger"]["by_ticker"]["AAA"]
    assert ledger["realized_premium"] == 60.0     # earned whether or not shares came
    assert ledger["open_collateral"] == 0.0       # collateral released


def test_early_and_expiry_assignment_are_the_same_path(store):
    """Early assignment on a short put is likelier than intuition suggests when
    short rates are elevated. A path that only handled expiry-day assignment
    would be wrong on exactly the surprising cases."""
    _open_put()
    executor.execute({"action": "put_assigned", "ticker": "AAA", "strike": STRIKE,
                      "contracts": 1, "expiration": EXPIRY, "stock_price": 47.0,
                      "assignment_date": "2026-08-04"})     # weeks before expiry
    e = [x for x in log.load_state()["executions"] if x["action"] == "put_assigned"][0]
    assert e["assignment_date"] == "2026-08-04"
    assert _position()["shares"]["cost_basis_per_share"] == 50.0


def test_a_closed_put_releases_collateral_and_realizes_premium(store):
    _open_put()
    executor.execute({"action": "put_closed", "ticker": "AAA", "strike": STRIKE,
                      "contracts": 1, "expiration": EXPIRY,
                      "reason": "expired_worthless", "stock_price": 55.0})
    ledger = log.load_state()["put_ledger"]["by_ticker"]["AAA"]
    assert ledger["open_collateral"] == 0.0
    assert ledger["realized_premium"] == 60.0     # no debit paid
    assert _position()["status"] == "closed"


def test_an_unknown_close_reason_is_rejected_before_anything_is_appended(store):
    _open_put()
    before = len(log.load_state()["executions"])
    with pytest.raises(ValueError):
        executor.execute({"action": "put_closed", "ticker": "AAA", "strike": STRIKE,
                          "contracts": 1, "expiration": EXPIRY,
                          "reason": "felt_like_it", "stock_price": 55.0})
    assert len(log.load_state()["executions"]) == before


# ---------------------------------------------------------------------------
# §1.5 — assignment detection: the loud path (the load-bearing test)
# ---------------------------------------------------------------------------
_OPEN_PUTS = {"AAA": {"strike": STRIKE, "expiration": EXPIRY, "contracts": 1}}
_ASSIGN_TXN = {
    "activityId": "T1", "type": "RECEIVE_AND_DELIVER",
    "transferItems": [
        {"instrument": {"assetType": "OPTION", "symbol": "AAA   260918P00050000"},
         "amount": 1},
        {"instrument": {"assetType": "EQUITY", "symbol": "AAA"}, "amount": 100}],
}


def test_an_assignment_transaction_becomes_a_put_assigned_proposal():
    props = ti.assignment_proposals([_ASSIGN_TXN], set(), _OPEN_PUTS)
    assert len(props) == 1
    p = props[0]
    assert p["action"] == "put_assigned" and p["ticker"] == "AAA"
    assert p["strike"] == STRIKE and p["contracts"] == 1
    # NEVER auto-booked: an assignment converts collateral into shares and retags
    # the position — a human decides, even when the evidence is unambiguous.
    assert p["proposal_id"].startswith("assign_")


def test_an_unrecognized_assignment_like_transaction_is_loud():
    """THE LOAD-BEARING TEST.

    An undocumented transaction type on a put-holding symbol is dropped by
    ``parse_transaction`` with NO error — the same silent discard that swallowed
    cash dividends before ``parse_dividend`` existed. The backstop must catch it
    anyway, so correctness does not depend on ``ASSIGNMENT_TYPES`` being right.
    """
    weird = {"activityId": "T2", "type": "SOME_UNDOCUMENTED_SCHWAB_TYPE",
             "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "AAA"},
                                "amount": 100}]}
    # Confirm the silent drop is real, then confirm it is caught anyway.
    assert ti.parse_transaction(weird) == (None, None)
    assert ti.parse_assignment(weird) is None
    loud = ti.unrecognized_on_open_puts([weird], _OPEN_PUTS)
    assert len(loud) == 1
    assert loud[0]["ticker"] == "AAA"
    assert "do NOT assume it is benign" in loud[0]["summary"]


def test_the_backstop_ignores_symbols_with_no_open_put():
    weird = {"activityId": "T3", "type": "SOME_UNDOCUMENTED_SCHWAB_TYPE",
             "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "ZZZ"},
                                "amount": 100}]}
    assert ti.unrecognized_on_open_puts([weird], _OPEN_PUTS) == []


def test_a_plain_trade_row_is_not_flagged_by_the_backstop():
    trade = {"activityId": "T4", "type": "TRADE",
             "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "AAA"},
                                "amount": 100}]}
    assert ti.unrecognized_on_open_puts([trade], _OPEN_PUTS) == []


# ---------------------------------------------------------------------------
# The Stage-1 blocker: reconciliation must be able to SEE a put
# ---------------------------------------------------------------------------
def test_an_open_put_is_visible_to_reconciliation(store):
    """Without this, modelling a put makes assignment drift SILENT.

    Before v22 a manually-opened put was caught only because it was foreign to
    state entirely — an accident of not supporting the feature, not a safety
    property. Emitting it here is what replaces the accident with a guarantee."""
    _open_put()
    expected, _excluded = reconcile.expected_view_from_state(log.load_state(), live_only=False)
    puts = [i for i in expected if i["instrument_type"] == reconcile.OPTION
            and i["put_call"] == reconcile.PUT]
    assert len(puts) == 1
    assert puts[0]["underlying"] == "AAA"
    assert float(puts[0]["quantity"]) == -1        # SHORT one contract


def test_a_short_put_past_expiry_expires_worthless_ABOVE_the_strike():
    """The worthless test INVERTS by side. A short call expires worthless below
    the strike; a short put expires worthless ABOVE it. This branch was hardcoded
    to CALL before v22, so a missing short put fell through to a plain MISSING —
    loud, but silent about the one thing worth knowing."""
    exp = reconcile._instrument(None, "AAA", reconcile.OPTION, reconcile.PUT,
                                50.0, "2020-01-17", -1)
    d = reconcile._classify_missing(0, exp, lambda t, e: 55.0, _DAY)
    assert d["classification"] == reconcile.EXPIRED_WORTHLESS_PENDING


def test_a_short_put_below_the_strike_on_expiry_suspects_assignment():
    exp = reconcile._instrument(None, "AAA", reconcile.OPTION, reconcile.PUT,
                                50.0, "2020-01-17", -1)
    d = reconcile._classify_missing(0, exp, lambda t, e: 45.0, _DAY)
    assert d["classification"] == reconcile.MISSING_AT_BROKER
    assert d.get("assignment_suspected") is True


def test_the_call_side_worthless_test_is_unchanged():
    """Regression lock: inverting the put case must not move the call case."""
    exp = reconcile._instrument(None, "AAA", reconcile.OPTION, reconcile.CALL,
                                50.0, "2020-01-17", -1)
    assert reconcile._classify_missing(
        0, exp, lambda t, e: 45.0, _DAY
    )["classification"] == reconcile.EXPIRED_WORTHLESS_PENDING
    assert reconcile._classify_missing(
        0, exp, lambda t, e: 55.0, _DAY
    )["classification"] == reconcile.MISSING_AT_BROKER


# ---------------------------------------------------------------------------
# §1.5 — old state loads unchanged; the type system refuses to guess
# ---------------------------------------------------------------------------
def test_old_state_migrates_additively_and_loses_nothing():
    old = {"schema_version": 21, "positions": [
        {"ticker": "OLD", "status": "active", "position_type": "SHARES",
         "shares": {"count": 200, "cost_basis_per_share": 10.0}}]}
    migrated, changed = migrations.migrate(dict(old), state_path=None)
    assert changed is True
    assert migrated["schema_version"] == 22
    p = migrated["positions"][0]
    assert p["position_type"] == "SHARES"          # untouched
    assert p["shares"]["count"] == 200             # untouched
    assert p["short_puts"] == []                   # additively seeded
    assert migrated["put_ledger"]["open_collateral"] == 0.0


def test_an_untagged_position_is_never_inferred_to_be_a_put():
    """`of()` never shape-sniffs. A half-built skeleton must stay legacy-shaped and
    visibly wrong rather than silently becoming a put."""
    looks_like_one = {"ticker": "X", "shares": {"count": 0},
                      "short_puts": [{"strike": 50.0, "contracts": 1}]}
    assert position_types.of(looks_like_one) == position_types.LEAP_PMCC_LEGACY
    assert position_types.is_put(looks_like_one) is False


def test_holds_shares_is_false_only_for_a_put():
    assert position_types.holds_shares({"position_type": "SHARES"}) is True
    assert position_types.holds_shares({"position_type": "LEAP_PMCC_LEGACY"}) is True
    assert position_types.holds_shares({"position_type": "CASH_SECURED_PUT"}) is False


# ---------------------------------------------------------------------------
# §1.5 — nothing in this stage touches the scan
# ---------------------------------------------------------------------------
def test_stage_1_adds_no_order_path():
    """No order construction anywhere in Stage 1. Asserted by absence: the three
    put actions never reach the live-placement path, and no put order builder
    exists. Placement is Stage 3, behind a flag that does not exist yet."""
    import inspect
    src = inspect.getsource(executor)
    put_block = src[src.index("def _put_opened("):src.index("def _close_if_empty(")]
    for forbidden in ("_place_live", "build_single_leg_order", "submit_order",
                      "place_order", "preview_order"):
        assert forbidden not in put_block, forbidden
    assert not hasattr(config, "CSP_ORDER_PLACEMENT_ENABLED")


def test_the_put_events_never_reach_the_juice_ledger(store):
    """Only a ``close_short`` feeds the theta ledger. A put's premium is booked to
    the put ledger, and its INTRINSIC never becomes income anywhere."""
    _open_put()
    executor.execute({"action": "put_closed", "ticker": "AAA", "strike": STRIKE,
                      "contracts": 1, "expiration": EXPIRY,
                      "reason": "expired_worthless", "stock_price": 55.0})
    state = log.load_state()
    assert state["theta_ledger"]["weeks"] == []
    assert state["theta_ledger"]["totals"]["ytd"] == 0
    assert state["put_ledger"]["realized_premium"] == 60.0
