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
def test_the_put_committers_construct_no_order():
    """The put *committers* build no order, at any stage. Placement (Stage 3)
    happens at the dispatch site through the shared ``_place_live`` path; the
    ``_put_*`` functions only mutate state from an already-terminal execution.
    Kept from Stage 1, where it also asserted the placement flag's absence --
    Stage 3 legitimately adds that flag, so the assertion moved to its default."""
    import inspect
    src = inspect.getsource(executor)
    put_block = src[src.index("def _put_opened("):src.index("def _close_if_empty(")]
    for forbidden in ("_place_live", "build_single_leg_order", "submit_order",
                      "place_order", "preview_order"):
        assert forbidden not in put_block, forbidden
    assert config.CSP_ORDER_PLACEMENT_ENABLED is False


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


# ===========================================================================
# STAGE 2 — MONITOR
#
# The load-bearing test in this section is the NEGATIVE one:
# `test_a_close_below_ma21_alone_does_not_advise_close`. Everything else proves
# a signal fires; that one proves the system does NOT act on the signal most
# likely to look like a reason and not be one. Quality names in intact uptrends
# dip below MA21 routinely, and a put struck at the MA21 zone is SUPPOSED to be
# approached — closing on that would systematically abandon positions doing
# exactly what they were sold to do.
# ===========================================================================
import alert_scheduler  # noqa: E402
import alerts  # noqa: E402


def _regate(**kw):
    return sv.put_close_advice(blocks=sv.put_close_triggers(**kw))


# ---------------------------------------------------------------------------
# §2.4 — the daily re-gate
# ---------------------------------------------------------------------------
def test_a_name_breaking_the_50_day_advises_close():
    """Three consecutive closes below the 50-day — the circuit breaker's own bar,
    NOT the entry veto's single close. The two rules share a name and differ:
    entry asks 'is this a good place to start' (waiting costs nothing); closing
    asks 'is the thesis broken' (acting costs a realized loss)."""
    advice = _regate(ma_fast_breached=True)
    assert advice["action"] == "close"
    assert advice["blocked_by"] == ["close_below_ma50"]
    assert advice["assignment_is_a_good_entry"] is False


def test_an_intact_name_holds_and_assignment_is_a_good_entry():
    advice = _regate(ma_fast_breached=False, ma_slow_breached=False,
                     rs3m_vs_spy=4.0, line_breached=False)
    assert advice["action"] == "hold"
    assert advice["assignment_is_a_good_entry"] is True


@pytest.mark.parametrize("kwargs,expected", [
    ({"ma_fast_breached": True}, "close_below_ma50"),
    ({"ma_slow_breached": True}, "close_below_ma200"),
    ({"rs3m_vs_spy": -0.01}, "rs3m_vs_spy"),
    ({"line_breached": True}, "line_in_the_sand"),
])
def test_each_structural_signal_closes_the_put(kwargs, expected):
    """§2.1 enumerates exactly four. Each asserted individually."""
    assert _regate(**kwargs)["blocked_by"] == [expected]


def test_the_close_trigger_set_is_exactly_the_four_structural_signals():
    assert set(sv.PUT_CLOSE_TRIGGERS) == {
        "close_below_ma50", "close_below_ma200", "rs3m_vs_spy", "line_in_the_sand"}
    # A STRICT SUBSET of the entry veto set: closing is narrower than refusing to
    # enter, deliberately.
    assert set(sv.PUT_CLOSE_TRIGGERS) < set(sv.VETO_IDS)


def test_the_regate_reads_the_rule_owners_and_does_not_fork_them(store):
    """Rule reuse, never a fork: the MA legs and the line come from
    circuit_breaker, RS from kill_switch. One definition each."""
    import inspect
    src = inspect.getsource(pm.put_regate)
    assert "circuit_breaker.evaluate" in src and "kill_switch.evaluate" in src
    # And drawdown is deliberately excluded — see the docstring.
    assert "drawdown" in src


# ---------------------------------------------------------------------------
# §2.4 — THE NEGATIVE TEST. MA21 must not close a put.
# ---------------------------------------------------------------------------
def test_a_close_below_ma21_alone_does_not_advise_close():
    """THE LOAD-BEARING NEGATIVE TEST.

    MA21 is a TIMING reference — it is what put the name on the put route in the
    first place. Structural, not remembered: there is no parameter through which
    it could reach the decision and no id for it in the trigger set."""
    advice = _regate(ma_fast_breached=False, ma_slow_breached=False,
                     rs3m_vs_spy=3.0, line_breached=False)
    assert advice["action"] == "hold"          # THE POSITION STAYS OPEN
    for name in ("ma21", "below_ma21", "extension_atr", "pct_above_ma21"):
        with pytest.raises(TypeError):
            sv.put_close_triggers(**{name: True})
    assert not any("ma21" in t for t in sv.PUT_CLOSE_TRIGGERS)


def test_the_account_and_tradeability_vetoes_do_not_close_an_open_put():
    """The entry veto set is WIDER than the close set. Acting on any of these
    would realize a loss to satisfy a bookkeeping cap, cross the exact spread that
    made the name untradeable, or trade on the absence of a signal."""
    entry_blocks = sv.evaluate(regime_color="red", has_weeklies=False,
                               stale=True, spread_pct=99.0)
    assert sv.compose(entry_blocks)["verdict"] == sv.BLOCKED     # would not ENTER
    assert sv.put_close_advice(blocks=entry_blocks)["action"] == "hold"  # but HOLDS


# ---------------------------------------------------------------------------
# §2.4 — the 8/21 cross is tempo only
# ---------------------------------------------------------------------------
def test_the_8_21_cross_produces_a_flag_and_no_transition():
    down = sv.tempo_signal(9.0, 11.0)
    up = sv.tempo_signal(11.0, 9.0)
    assert down["signal"] == sv.TEMPO_DOWN and up["signal"] == sv.TEMPO_UP
    # Literal disclaimers, not config reads — no switch can grant tempo authority.
    for sig in (down, up):
        assert sig["tempo_only"] is True and sig["closes_nothing"] is True
    # A DOWN cross closes nothing: the decision takes no tempo argument.
    for name in ("tempo", "ema_fast", "ema_slow", "cross"):
        with pytest.raises(TypeError):
            sv.put_close_triggers(**{name: "TEMPO_DOWN"})
    assert _regate(rs3m_vs_spy=3.0)["action"] == "hold"


def test_an_unreadable_tempo_is_none_not_a_direction():
    assert sv.tempo_signal(None, 11.0)["signal"] is None


# ---------------------------------------------------------------------------
# §2.4 — the mandatory expiry-day check
# ---------------------------------------------------------------------------
def _state_with_put_expiring(day: str):
    return {"positions": [{"ticker": "AAA", "status": "active",
                           "position_type": "CASH_SECURED_PUT",
                           "short_puts": [{"strike": 50.0, "expiration": day,
                                           "contracts": 1}]}]}


def _at(hour, minute=45, day=None):
    from datetime import datetime as _dt
    d = day or _date.today()
    return _dt.combine(d, _dt.min.time()).replace(hour=hour, minute=minute)


def test_the_expiry_day_check_fires_on_the_correct_date():
    today = _date.today()
    due = alert_scheduler.mandatory_expiry_checks(
        _state_with_put_expiring(today.isoformat()), _at(15), last={})
    assert len(due) == 1 and due[0].startswith(today.isoformat())


def test_the_expiry_day_check_does_not_fire_before_the_pre_close_window():
    today = _date.today()
    assert alert_scheduler.mandatory_expiry_checks(
        _state_with_put_expiring(today.isoformat()), _at(9), last={}) == []


def test_the_expiry_day_check_does_not_fire_for_a_put_expiring_later():
    from datetime import timedelta as _td
    later = (_date.today() + _td(days=7)).isoformat()
    assert alert_scheduler.mandatory_expiry_checks(
        _state_with_put_expiring(later), _at(15), last={}) == []


def test_the_expiry_day_check_runs_once_per_day():
    today = _date.today()
    state = _state_with_put_expiring(today.isoformat())
    ran = {k: today for k in alert_scheduler.mandatory_expiry_checks(state, _at(15), last={})}
    assert alert_scheduler.mandatory_expiry_checks(state, _at(15), last=ran) == []


def test_the_expiry_day_check_is_covered_by_the_dead_mans_switch():
    """A missed expiry-day evaluation is a silent failure with real money attached.
    Unlike every other evaluator this one PAGES on failure rather than merely
    logging — it does not get another chance in an hour."""
    import inspect
    src = inspect.getsource(alert_scheduler._maybe_expiry_check)
    assert 'heartbeat.ping("/fail", force=True)' in src
    # And it runs on EVERY tick, BEFORE the slot-based early return — so a put
    # expiring on a day with no due slot is still evaluated.
    tick = inspect.getsource(alert_scheduler._tick)
    assert tick.index("_maybe_expiry_check(now)") < tick.index("due = due_slots(now)")


def test_the_expiry_check_extends_the_existing_daemon_not_a_second_one():
    """§2.2: extend it, do not build a second daemon."""
    import inspect
    src = inspect.getsource(alert_scheduler)
    assert src.count("def _tick(") == 1
    # One SCHEDULER thread. (The module also spawns a detached scan-warmup
    # thread, which is not a scheduler and does not evaluate anything.)
    assert src.count('name="cfm-alerts"') + src.count("name=\"alert-scheduler\"") <= 1
    assert "_maybe_expiry_check(now)" in inspect.getsource(alert_scheduler._tick)


# ---------------------------------------------------------------------------
# §2.4 — no roll path exists for a short put. Assert by absence.
# ---------------------------------------------------------------------------
def test_no_roll_path_exists_for_a_short_put():
    """§2.3: a put roll is a DEBIT and a Martingale structure — it converts a
    bounded mistake into an unbounded one. Not implemented, and not to be
    implemented "for completeness"."""
    assert "roll_put" not in executor.VALID_ACTIONS
    assert "put_roll" not in executor.VALID_ACTIONS
    for name in dir(executor):
        low = name.lower()
        assert not ("roll" in low and "put" in low), name
    # The close-reason enum offers no way to even SPELL a roll.
    assert set(executor._PUT_CLOSE_REASONS) == {
        "expired_worthless", "closed_gate_failure", "closed_structural",
        "closed_manual"}


def test_the_close_advice_never_offers_a_roll():
    import inspect
    src = inspect.getsource(alerts.check_put_regate)
    assert "do NOT roll" in src or "never roll" in src


# ---------------------------------------------------------------------------
# The alert wiring
# ---------------------------------------------------------------------------
def test_the_put_alerts_are_registered_with_severities():
    for t in ("PUT_GATE_FAILURE", "PUT_EXPIRY_ACTION", "PUT_ASSIGNED",
              "PUT_ACTIVITY_UNRECOGNIZED", "PUT_COLLATERAL_BREACH"):
        assert t in alerts.ALERT_TYPES, t
    # The undetected-assignment case is the expensive one and is CRITICAL.
    assert alerts.ALERT_TYPES["PUT_ACTIVITY_UNRECOGNIZED"][0] == "CRITICAL"


def test_unrecognized_broker_activity_raises_a_critical_alert():
    state = {"positions": [], "ingestion": {"last": {
        "unrecognized_put_activity": [
            {"transaction_id": "T9", "ticker": "AAA", "type": "MYSTERY",
             "summary": "unrecognized"}]}}}
    fired = alerts.check_put_assignment_detected(state)
    assert [a["type"] for a in fired] == ["PUT_ACTIVITY_UNRECOGNIZED"]
    assert "not assume it is benign" in fired[0]["action"].lower()


def test_a_detected_assignment_raises_an_alert_to_book_it():
    state = {"positions": [], "ingestion": {"last": {
        "assignment_proposals": [
            {"transaction_id": "T1", "ticker": "AAA", "shares": 100,
             "strike": 50.0}]}}}
    assert [a["type"] for a in alerts.check_put_assignment_detected(state)] == ["PUT_ASSIGNED"]


def test_the_expiry_alert_says_let_it_assign_when_the_name_still_passes(store, monkeypatch):
    _open_put()
    monkeypatch.setattr(pm, "put_regate",
                        lambda p, df=None: {"action": "hold",
                                            "assignment_is_a_good_entry": True})
    st = log.load_state()
    st["positions"][0]["short_puts"][0]["expiration"] = _date.today().isoformat()
    fired = alerts.check_put_expiry_day(st)
    assert len(fired) == 1 and "assign" in fired[0]["action"].lower()


def test_the_expiry_alert_says_close_when_the_name_would_be_refused(store, monkeypatch):
    _open_put()
    monkeypatch.setattr(pm, "put_regate",
                        lambda p, df=None: {"action": "close",
                                            "blocked_by": ["close_below_ma200"],
                                            "assignment_is_a_good_entry": False})
    st = log.load_state()
    st["positions"][0]["short_puts"][0]["expiration"] = _date.today().isoformat()
    assert "CLOSE" in alerts.check_put_expiry_day(st)[0]["action"]


# ===========================================================================
# STAGE 3 — PLACE
#
# The flag is FALSE by default and every test here that exercises placement
# turns it on explicitly. The most important tests are the two that assert
# what happens when it is OFF: the behaviour must be byte-identical to Stage 1,
# because "we shipped the code but left it off" is only a safe position if the
# off path is genuinely the old path and not a new one wearing its clothes.
# ===========================================================================
import schwab_api  # noqa: E402


@pytest.fixture()
def placement_on(monkeypatch):
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    return True


# ---------------------------------------------------------------------------
# The flag, and what it is worth
# ---------------------------------------------------------------------------
def test_the_placement_flag_is_false_by_default():
    """Stages 1 and 2 track and monitor a put opened by hand. This flag is the
    only thing that lets the app PLACE one, and it is not to be turned on until
    Stage 2 has run against a real position for 14 days and a human has reviewed
    what it did."""
    assert config.CSP_ORDER_PLACEMENT_ENABLED is False


def test_with_the_flag_off_a_put_books_and_never_places(store, monkeypatch):
    """The off path must be the STAGE 1 path, not a new one wearing its clothes."""
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", False)
    monkeypatch.setattr(executor, "live_transmit", lambda: True)   # live, but flag off
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    called = []
    monkeypatch.setattr(executor, "_place_live",
                        lambda *a, **k: called.append(a) or {"status": "working"})
    res = _open_put()
    assert called == []                       # nothing was transmitted
    assert res["status"] == "filled"          # booked immediately, as in Stage 1
    assert _position()["short_puts"][0]["strike"] == STRIKE


def test_with_the_flag_off_placement_is_unreachable_even_in_live_mode(store, monkeypatch):
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", False)
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(executor, "_place_live",
                        lambda *a, **k: pytest.fail("placement reached with the flag off"))
    _open_put()


def test_paper_mode_never_places_even_with_the_flag_on(store, monkeypatch):
    """All three gates must be true. The flag alone is not enough."""
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(executor, "live_transmit", lambda: False)   # paper
    monkeypatch.setattr(executor, "_place_live",
                        lambda *a, **k: pytest.fail("placement reached in paper mode"))
    assert _open_put()["status"] == "filled"


# ---------------------------------------------------------------------------
# The option SIDE — the silent wrong-contract risk
# ---------------------------------------------------------------------------
def test_a_put_order_builds_a_PUT_symbol_not_a_call():
    """`occ_option_symbol` defaults call=True and its docstring says "CFM trades
    calls". A missed call=False does NOT fail loudly — it builds a VALID symbol
    for the WRONG instrument and sells a call against a position with no shares to
    cover it. The side is derived from PUT_ACTIONS at the one placement site."""
    put = schwab_api.occ_option_symbol("AAA", "2026-09-18", 50.0, call=False)
    call = schwab_api.occ_option_symbol("AAA", "2026-09-18", 50.0, call=True)
    assert put[12] == "P" and call[12] == "C"
    assert put != call
    # And the derivation is a set membership, not a defaulted argument.
    assert "put_opened" in executor.PUT_ACTIONS
    assert "sell_short" not in executor.PUT_ACTIONS


def test_the_placement_site_derives_the_side_from_put_actions():
    import inspect
    src = inspect.getsource(executor._place_live)
    assert "call=action not in PUT_ACTIONS" in src
    assert "call=True" not in src          # no defaulted side anywhere on the path


def test_the_put_instructions_are_the_same_verbs_as_the_call_leg():
    """The side is carried by the OCC symbol's C/P flag, not by the instruction —
    which is why the order builder needed no put-specific branch."""
    assert executor.INSTRUCTION["put_opened"] == "SELL_TO_OPEN"
    assert executor.INSTRUCTION["put_closed"] == "BUY_TO_CLOSE"


# ---------------------------------------------------------------------------
# §Stage 3 — weekly expiries only
# ---------------------------------------------------------------------------
def _gate(expiration, **payload):
    return executor._enforce_put_ticket_gates(
        {"expiration": expiration, **payload}, "AAA", STRIKE, expiration, 1)


def test_a_monthly_expiry_is_refused(placement_on):
    from datetime import timedelta as _td
    far = (_date.today() + _td(days=45)).isoformat()
    with pytest.raises(ValueError, match="weekly expiries only"):
        _gate(far)


def test_an_unparseable_expiry_is_refused(placement_on):
    with pytest.raises(ValueError, match="parseable expiration"):
        _gate("next friday")


def test_the_weekly_bar_is_a_named_constant():
    assert config.PUT_MAX_DTE == 10


# ---------------------------------------------------------------------------
# §Stage 3 — the executor re-checks the FULL veto set at the ticket
# ---------------------------------------------------------------------------
def _next_weekly():
    from datetime import timedelta as _td
    d = _date.today() + _td(days=1)
    while d.weekday() != 4:                   # Friday
        d += _td(days=1)
    return d.isoformat()


def test_the_ticket_re_enforces_the_veto_set(placement_on, monkeypatch):
    """Scan output is ADVISORY; the executor enforces. The route selector may have
    said 'sell a put here' hours ago off a memoized sweep."""
    import screening
    monkeypatch.setattr(screening, "regime", lambda: {"published_regime": "red"})
    with pytest.raises(ValueError, match="the entry rules refuse this name"):
        _gate(_next_weekly())


def test_a_put_earns_no_exemption_for_being_an_option(placement_on, monkeypatch):
    """A put is a synthetic LONG position. Every veto that would refuse a shares
    entry refuses a put entry too."""
    import screening
    monkeypatch.setattr(screening, "regime", lambda: {"published_regime": "green"})
    with pytest.raises(ValueError, match="no_weeklies"):
        _gate(_next_weekly(), has_weeklies=False)
    with pytest.raises(ValueError, match="stale_inputs"):
        _gate(_next_weekly(), stale=True)


def test_an_eligible_name_passes_the_ticket_gates(placement_on, monkeypatch):
    import screening
    monkeypatch.setattr(screening, "regime", lambda: {"published_regime": "green"})
    _gate(_next_weekly())                     # must not raise


# ---------------------------------------------------------------------------
# §Stage 3 — put-side spread floor
# ---------------------------------------------------------------------------
def test_a_wide_put_side_spread_is_refused(placement_on, monkeypatch):
    """Put-side spreads run wider than call-side on many names, and a weekly
    cadence pays the round trip 52 times a year."""
    import screening
    monkeypatch.setattr(screening, "regime", lambda: {"published_regime": "green"})
    with pytest.raises(ValueError, match="tradeability floor"):
        _gate(_next_weekly(),
              put_spread_pct=config.TRADEABILITY_MAX_SPREAD_PCT + 0.1)


def test_a_tight_put_side_spread_passes(placement_on, monkeypatch):
    import screening
    monkeypatch.setattr(screening, "regime", lambda: {"published_regime": "green"})
    _gate(_next_weekly(), put_spread_pct=1.0)      # must not raise


def test_an_unknown_put_spread_does_not_block(placement_on, monkeypatch):
    """Unknown is not wide. The spread probe is provider-dependent."""
    import screening
    monkeypatch.setattr(screening, "regime", lambda: {"published_regime": "green"})
    _gate(_next_weekly(), put_spread_pct=None)


def test_the_gates_are_inert_when_the_flag_is_off(monkeypatch):
    """Nothing is being placed, so nothing is gated — and a monthly expiry that
    would be refused for a PLACED put is irrelevant to a BOOKED one."""
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", False)
    from datetime import timedelta as _td
    _gate((_date.today() + _td(days=45)).isoformat())     # must not raise


# ---------------------------------------------------------------------------
# §Stage 3 — the EXISTING machinery, reused rather than duplicated
# ---------------------------------------------------------------------------
def test_placement_reuses_the_existing_lifecycle_machinery():
    """§Stage 3: 'Put orders go through the existing executor, order state
    machine, resubmission lock, and reconciliation.' Asserted at the dispatch."""
    import inspect
    src = inspect.getsource(executor.execute)
    branch = src[src.index("if action in PUT_ACTIONS:"):]
    for shared in ("_enforce_execution_window", "_enforce_spread_quality",
                   "_place_live"):
        assert shared in branch, shared
    # And _place_live itself carries the resubmission lock for every action.
    assert "_guard_resubmit" in inspect.getsource(executor._place_live)


def test_a_filled_put_order_commits_through_the_same_committer(store):
    """One committer, two ways in. A second one would be the first place a PLACED
    put and a BOOKED put could diverge."""
    import inspect
    src = inspect.getsource(executor._commit_from_pending)
    assert "_put_opened if action ==" in src
    # The fill price lands on the right field for each side.
    assert 'payload["debit_per_share"] = fill_price' in src


def test_the_order_state_machine_stays_instrument_agnostic():
    """It was already generic — Phase 0 found zero instrument coupling. This is a
    regression lock so a put-specific branch cannot be added to it later."""
    import inspect
    import order_lifecycle
    src = inspect.getsource(order_lifecycle).lower()
    for word in ("put_opened", "put_closed", "putcall", "is_put"):
        assert word not in src, word


# ---------------------------------------------------------------------------
# §DO NOT — still true at Stage 3
# ---------------------------------------------------------------------------
def test_no_put_roll_path_exists_at_stage_3():
    assert "roll_put" not in executor.VALID_ACTIONS
    assert "put_roll" not in executor.VALID_ACTIONS
    assert "roll_put" not in executor.INSTRUCTION
    for name in dir(executor):
        low = name.lower()
        assert not ("roll" in low and "put" in low), name


def test_an_assignment_is_never_placed(store, monkeypatch):
    """An assignment is an event the operator did not choose. It books directly
    whatever the flag says — there is no order to place."""
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(executor, "live_transmit", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", False)
    _open_put()
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(executor, "_place_live",
                        lambda *a, **k: pytest.fail("an assignment was placed"))
    executor.execute({"action": "put_assigned", "ticker": "AAA", "strike": STRIKE,
                      "contracts": 1, "expiration": EXPIRY, "stock_price": 47.0})
    assert _position()["shares"]["count"] == 100


# ===========================================================================
# The put TICKET — the surface that was missing (schema v22)
# ===========================================================================
def scan_verdict_juice(premium, strike):
    import scan_verdict
    return scan_verdict.put_juice_pct(premium, strike)


def _chain_payload(underlying=100.0, exp="2026-09-04", dte=7, strikes=None):
    """A Schwab chain payload carrying BOTH sides, as the real one does."""
    strikes = strikes or {95.0: (1.00, 1.10), 96.0: (1.40, 1.55), 97.0: (1.90, 2.10)}
    put_map = {}
    for k, (bid, ask) in strikes.items():
        put_map[str(k)] = [{"strikePrice": k, "bid": bid, "ask": ask,
                            "daysToExpiration": dte, "delta": -0.25,
                            "symbol": f"X{k}P"}]
    return {"underlyingPrice": underlying,
            "callExpDateMap": {f"{exp}:{dte}": {}},
            "putExpDateMap": {f"{exp}:{dte}": put_map}}


def test_put_intrinsic_points_the_other_way():
    """The silent-failure case. Hard-coding the CALL form (underlying - strike)
    for a put does not raise — it reports zero intrinsic and books the whole
    premium as time value, which would let intrinsic masquerade as juice."""
    import indicators
    assert indicators.intrinsic_value(105, 100, put=True) == 5      # ITM put
    assert indicators.intrinsic_value(95, 100, put=True) == 0       # OTM put
    assert indicators.intrinsic_value(95, 100) == 5                 # ITM call
    assert indicators.intrinsic_value(105, 100) == 0                # OTM call


def test_spread_pct_is_unknown_not_zero_when_unmeasurable():
    """0.0 reads as 'perfectly tight' and would wave a name through the very
    tradeability gate this figure feeds."""
    import indicators
    assert indicators.spread_pct(1.00, 1.10) == 9.52
    assert indicators.spread_pct(None, 1.10) is None
    assert indicators.spread_pct(0, 0) is None


def test_both_chain_sides_share_one_parser():
    """One flattener, so a field the call side normalizes cannot quietly differ
    on the put side."""
    import schwab_api
    payload = _chain_payload()
    _, puts = schwab_api.parse_put_chain(payload)
    assert puts and all(p["expiration"] == "2026-09-04" for p in puts)
    assert {p["strike"] for p in puts} == {95.0, 96.0, 97.0}
    assert sorted(puts[0].keys()) == sorted(
        schwab_api.parse_call_chain(
            {"callExpDateMap": payload["putExpDateMap"]})[1][0].keys())


def test_ticket_offers_only_what_the_executor_will_accept(monkeypatch):
    """Offering a monthly here and refusing it at the ticket would train the
    operator to read a rejection as a bug rather than as the rule working."""
    import option_chain
    long_dated = _chain_payload(dte=45, exp="2026-10-16")
    monkeypatch.setattr(option_chain, "_fetch_chain", lambda t, refresh=False: long_dated)
    monkeypatch.setattr(option_chain.screening, "entry_gate",
                        lambda t: {"verdict": "ELIGIBLE", "blocked_by": [], "route": {}})
    monkeypatch.setattr(option_chain.data_handler, "get_daily", lambda s, force=False: None)
    monkeypatch.setattr(option_chain.data_handler, "latest_quote",
                        lambda s: {"price": 100.0})
    out = option_chain.put_chain("AAA")
    assert out["expirations"] == [], f"45 DTE is past PUT_MAX_DTE={config.PUT_MAX_DTE}"
    assert out["max_dte"] == config.PUT_MAX_DTE


def test_ticket_quotes_the_bid_not_the_midpoint(monkeypatch):
    """Selling to open hits the BID. Quoting a midpoint the operator will not get
    is how a juice floor clears on paper and misses in the account."""
    import option_chain
    monkeypatch.setattr(option_chain, "_fetch_chain",
                        lambda t, refresh=False: _chain_payload())
    monkeypatch.setattr(option_chain.screening, "entry_gate",
                        lambda t: {"verdict": "ELIGIBLE", "blocked_by": [], "route": {}})
    monkeypatch.setattr(option_chain.data_handler, "get_daily", lambda s, force=False: None)
    monkeypatch.setattr(option_chain.data_handler, "latest_quote", lambda s: {"price": 100.0})
    rows = option_chain.put_chain("AAA")["expirations"][0]["strikes"]
    row = next(r for r in rows if r["strike"] == 95.0)
    assert row["premium_per_share"] == 1.00           # the bid, not 1.05
    assert row["collateral"] == 95.0 * config.SHARES_PER_LOT
    assert row["juice_pct"] == scan_verdict_juice(1.00, 95.0)
    assert row["delta_abs"] == 0.25                   # magnitude, not -0.25


def test_ticket_collateral_and_juice_come_from_the_shared_helpers():
    """A second formula in the ticket is how it ends up disagreeing with the
    position it creates."""
    import option_chain
    import scan_verdict
    row = option_chain._put_strike_view(
        {"strike": 50.0, "bid": 1.0, "ask": 1.1, "delta": -0.3})
    assert row["collateral"] == scan_verdict.put_collateral(50.0, 1)
    assert row["juice_pct"] == scan_verdict.put_juice_pct(1.0, 50.0)


def test_placement_status_names_every_switch_that_is_off():
    """Greying a button out leaves the operator guessing WHICH switch is off."""
    import option_chain
    st = option_chain.placement_status()
    assert st["can_place"] is False
    assert not config.CSP_ORDER_PLACEMENT_ENABLED
    assert any("CSP_ORDER_PLACEMENT_ENABLED" in r for r in st["reasons"])
    assert set(st) == {"enabled", "live", "configured", "demo_data",
                       "live_trading_toggle", "can_place", "reasons"}


def test_placement_status_can_place_only_when_every_switch_is_on(monkeypatch):
    import option_chain
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(option_chain.executor, "live_enabled", lambda: True)
    monkeypatch.setattr(option_chain.config, "demo_enabled", lambda: False)
    monkeypatch.setattr(option_chain.schwab_api, "configured", lambda: True)
    st = option_chain.placement_status()
    assert st["can_place"] is True and st["reasons"] == []


def test_recording_a_fill_works_with_placement_off(store):
    """Stage 1 is the fallback the ticket always has: with every switch off it
    still books a put sold at the broker. THIS is what was missing from the UI."""
    assert not config.CSP_ORDER_PLACEMENT_ENABLED
    _open_put("AAA")
    pos = _position("AAA")
    assert pos["position_type"] == "CASH_SECURED_PUT"
    assert len(pos["short_puts"]) == 1


def test_a_recorded_put_is_marked_logged_not_live(store):
    """The field the UI keys on to tell the truth about what happened.

    With placement off, `put_opened` books to the ledger and sends NOTHING to
    Schwab. `mode == "logged"` is the ONLY signal distinguishing that from a real
    broker fill, and `orderFlow.js` reads it to decide between "RECORDED to your
    ledger — NO order was sent" and "filled & logged". If this field ever went
    missing the UI would silently claim every recorded entry was a broker fill —
    which is exactly the confusion that motivated this test.
    """
    assert not config.CSP_ORDER_PLACEMENT_ENABLED
    res = _open_put("AAA")
    assert res["mode"] == "logged"
    assert res.get("status") != "working"


def test_a_recorded_put_is_EXCLUDED_from_reconciliation(store):
    """The safety net does NOT cover this, and that is deliberate — which is
    exactly why the UI has to tell the truth at the moment of recording.

    `expected_view_from_state` runs `live_only=True` in production: a position
    established by a logged (paper) execution is excluded, because reconciling
    paper positions would mass-flag every one of them. Correct in general, but it
    means a put RECORDED and never actually sold at the broker is invisible to
    reconciliation — the app holds it, Schwab does not, and nothing complains.

    So there is no downstream backstop for a mistaken record. The only defence is
    at entry: the confirm dialog and the "NO order was sent to Schwab" toast.
    """
    import reconcile
    _open_put("AAA")
    instruments, excluded = reconcile.expected_view_from_state(log.load_state())
    assert not [i for i in instruments if i.get("instrument_type") == "OPTION"], (
        "a logged put must not be reconciled — paper positions would mass-flag")
    assert excluded, "and it must be recorded as excluded, with a reason"


def test_a_LIVE_put_is_committed_as_live_and_IS_reconciled(store):
    """The other half, and a bug this pinned: all three put committers used to
    hard-code mode="logged", including the path that commits a genuinely PLACED
    and FILLED order (`_commit_from_pending`). So a real live put was booked as
    paper — excluded from reconciliation, and reported to the operator as "no
    order was sent" for an order that was. Mode is a parameter now."""
    import reconcile
    executor._put_opened(
        {"expiration": _next_weekly(), "premium_per_share": PREMIUM},
        "AAA", STRIKE, 1, 53.0, mode="live")
    ex = [e for e in log.load_state()["executions"] if e["action"] == "put_opened"]
    assert ex and ex[-1]["mode"] == "live"
    instruments, _excluded = reconcile.expected_view_from_state(log.load_state())
    assert [i for i in instruments if i.get("instrument_type") == "OPTION"]


def test_reconciliation_covers_a_shares_position(store):
    """A PRE-EXISTING bug this work surfaced. `_ticker_liveness` inspected only
    `buy_leap` executions, so once the shares-primary migration (v20) made
    `buy_shares` the base leg, a real live-transmitted position returned None and
    was excluded as "unknown_live_status". Reconciliation — the thing that
    catches a position the app holds and the broker does not — was off for every
    position of the shares era."""
    import reconcile
    state = {"positions": [{"ticker": "AAA", "status": "active",
                            "shares": {"count": 100, "cost_basis_per_share": 50.0},
                            "short_calls": [], "short_puts": []}],
             "executions": [{"ticker": "AAA", "action": "buy_shares",
                             "mode": "live", "live_transmitted": True}]}
    assert reconcile._ticker_liveness(state, "AAA") is True
    instruments, excluded = reconcile.expected_view_from_state(state)
    assert instruments and not excluded

    # …and the exclusion still works, or this is just "reconcile everything".
    state["executions"][0]["live_transmitted"] = False
    _inst, excluded = reconcile.expected_view_from_state(state)
    assert excluded and excluded[0]["reason"] == "paper"


def test_placement_reasons_never_point_at_the_wrong_switch(monkeypatch):
    """FOUR switches gate a real put order and their names are close enough to be
    mistaken for each other. "Live data" in Settings is the DATA SOURCE toggle —
    it decides whether the app reads real state or a seeded demo store, and has
    nothing to do with whether an order reaches Schwab.

    `executor.live_transmit()` is itself the AND of two switches, so reporting it
    alone says "live trading is off" when the real cause is demo mode, pointing
    the operator at a control that is already set correctly."""
    import option_chain
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(option_chain.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(option_chain.executor, "live_enabled", lambda: True)
    # Live trading ON, Schwab connected, put placement ON — but DEMO data.
    monkeypatch.setattr(option_chain.config, "demo_enabled", lambda: True)

    st = option_chain.placement_status()
    assert st["can_place"] is False
    assert st["demo_data"] is True and st["live_trading_toggle"] is True
    joined = " ".join(st["reasons"])
    assert "Demo" in joined, joined
    assert "Live trading switch is off" not in joined, (
        "must not blame the live-trading toggle when it is ON")


def test_placement_status_separates_the_data_toggle_from_the_trading_toggle(monkeypatch):
    """The inverse: Live DATA on, live TRADING off. The reason must name the
    trading switch and say nothing about demo."""
    import option_chain
    monkeypatch.setattr(config, "CSP_ORDER_PLACEMENT_ENABLED", True)
    monkeypatch.setattr(option_chain.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(option_chain.config, "demo_enabled", lambda: False)
    monkeypatch.setattr(option_chain.executor, "live_enabled", lambda: False)

    st = option_chain.placement_status()
    joined = " ".join(st["reasons"])
    assert "Live trading switch is off" in joined, joined
    assert "Demo" not in joined, joined
