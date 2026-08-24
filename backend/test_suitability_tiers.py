"""Suitability suppression tiers — the classification table, both hysteresis
directions, the shadow-first rollout, the transition-event stream, and the hard
safety invariant that suppression touches the ENTRY universe only.

Offline: fixture rows and synthetic observations, never a provider.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-tiers-test-"))

import config  # noqa: E402
import juice_capacity as jc  # noqa: E402
import suitability_tiers as st  # noqa: E402

FLOOR = 0.70          # the prompt's worked floor, used for the pure-logic table


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "LOG_PATH", str(tmp_path / "tiers.json"))
    monkeypatch.setattr(st, "_parsed", None)
    monkeypatch.setattr(jc, "LOG_PATH", str(tmp_path / "capacity.json"))
    monkeypatch.setattr(jc, "_parsed", None)
    # Shadow by default — every enforcement test opts in explicitly.
    monkeypatch.setattr(config, "SUPPRESSION_ENFORCE", False)
    monkeypatch.setattr(config, "SUPPRESSION_REVIEW_DATE", None)
    yield


def _enforce(monkeypatch, review="2026-01-01"):
    """Turn enforcement fully on: the flag, the dated capacity review, and enough
    elapsed shadow days."""
    monkeypatch.setattr(config, "SUPPRESSION_ENFORCE", True)
    monkeypatch.setattr(config, "SUPPRESSION_REVIEW_DATE", review)


def _row(ticker, capacity, current, floor=0.75, *, obs=60, source=None,
         verdict="WATCH", bench=True, **extra):
    """A scan row shaped like the real one, with a juice_capacity block."""
    source = source or {jc.SOURCE_LIVE: obs}
    return {
        "ticker": ticker, "verdict": verdict, "bench": bench,
        "combined_weekly_yield_pct": current, "juice_weekly_pct": current,
        "juice_capacity": {
            "capacity": capacity, "floor_pct": floor, "obs": obs,
            "by_source": source, "min_obs": config.CAPACITY_MIN_OBS,
            "insufficient_history": capacity == jc.INSUFFICIENT_HISTORY,
        },
        **extra,
    }


# ---------------------------------------------------------------------------
# 1.7 — the tier logic table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("capacity,current,prior,expected", [
    # Clears on both legs.
    (1.00, 1.00, None, st.SUITABLE),
    (0.75, 0.80, None, st.SUITABLE),
    # Structural: capacity below 60% of the floor (0.42).
    (0.30, 0.30, None, st.SUPPRESSED_STRUCTURAL),
    (0.41, 0.90, None, st.SUPPRESSED_STRUCTURAL),   # capacity alone decides
    # Condition: capacity clears, current below 80% of the floor (0.56).
    (0.85, 0.40, None, st.SUPPRESSED_CONDITION),
    # Insufficient history is unsuppressible whatever the current reading.
    (jc.INSUFFICIENT_HISTORY, 0.01, None, st.SUITABLE),
    (jc.INSUFFICIENT_HISTORY, 0.01, st.SUPPRESSED_STRUCTURAL, st.SUITABLE),
])
def test_tier_logic_table(capacity, current, prior, expected):
    assert st.classify(capacity, current, FLOOR, current_tier=prior)["tier"] == expected


def test_structural_boundary_at_59_and_61_percent_of_floor():
    """0.60 x floor is the line; 0.59 is below it and 0.61 is not."""
    assert st.classify(FLOOR * 0.59, FLOOR, FLOOR)["tier"] == st.SUPPRESSED_STRUCTURAL
    assert st.classify(FLOOR * 0.61, FLOOR, FLOOR)["tier"] == st.SUITABLE


def test_condition_hysteresis_suppresses_at_79_holds_at_85_readmits_at_101():
    """The band that stops a name flickering in and out of the scan week to
    week: suppress below 80% of the floor, readmit only at 100%, and everything
    between holds whatever the name already was."""
    # 0.79 x floor -> suppress (from SUITABLE).
    down = st.classify(FLOOR, FLOOR * 0.79, FLOOR, current_tier=st.SUITABLE)
    assert down["tier"] == st.SUPPRESSED_CONDITION

    # 0.85 x floor while already CONDITION -> stays. Above the suppress bar but
    # below the readmit bar: this is the whole point of the band.
    hold = st.classify(FLOOR, FLOOR * 0.85, FLOOR, current_tier=st.SUPPRESSED_CONDITION)
    assert hold["tier"] == st.SUPPRESSED_CONDITION
    assert hold["reason"] == st.REASON_HOLD_NO_READMIT

    # ...and the same 0.85 reading would NOT have suppressed a SUITABLE name.
    assert st.classify(FLOOR, FLOOR * 0.85, FLOOR,
                       current_tier=st.SUITABLE)["tier"] == st.SUITABLE

    # 1.01 x floor -> readmit.
    up = st.classify(FLOOR, FLOOR * 1.01, FLOOR, current_tier=st.SUPPRESSED_CONDITION)
    assert up["tier"] == st.SUITABLE


def test_structural_hysteresis_exits_only_above_70_percent_capacity():
    # 0.65 x floor: above the 0.60 entry bar but below the 0.70 exit bar — a name
    # already STRUCTURAL stays there.
    assert st.classify(FLOOR * 0.65, FLOOR, FLOOR,
                       current_tier=st.SUPPRESSED_STRUCTURAL)["tier"] == st.SUPPRESSED_STRUCTURAL
    # 0.71 x floor clears the exit band.
    assert st.classify(FLOOR * 0.71, FLOOR, FLOOR,
                       current_tier=st.SUPPRESSED_STRUCTURAL)["tier"] == st.SUITABLE


def test_structural_exit_lands_in_condition_when_current_is_still_weak():
    """A recovered MEDIAN says the instrument can pay — not that it is paying
    today. Jumping straight to SUITABLE would put a name with a 0.20%/wk current
    reading back in the main table."""
    out = st.classify(FLOOR * 0.90, FLOOR * 0.30, FLOOR,
                      current_tier=st.SUPPRESSED_STRUCTURAL)
    assert out["tier"] == st.SUPPRESSED_CONDITION


def test_unpriceable_current_holds_the_existing_tier():
    """A pricing outage is neither grounds to suppress nor evidence to readmit."""
    for prior in (st.SUITABLE, st.SUPPRESSED_CONDITION):
        out = st.classify(FLOOR, None, FLOOR, current_tier=prior)
        assert out["tier"] == prior and out["reason"] == st.REASON_UNPRICEABLE


def test_missing_floor_never_suppresses():
    for floor in (None, 0):
        assert st.classify(0.01, 0.01, floor)["tier"] == st.SUITABLE


# ---------------------------------------------------------------------------
# The backfill-provenance guard on STRUCTURAL (audit §8.4)
# ---------------------------------------------------------------------------
def test_structural_requires_live_observations_not_backfill_alone():
    """A backfilled median is juice-only and understates a dividend payer — for
    ET the dividend leg is most of the combined number. Permanently hiding a
    payer on a number we know is biased low is this feature's worst failure
    mode, so STRUCTURAL waits for real observations."""
    thin = config.SUPPRESS_STRUCTURAL_MIN_LIVE_OBS - 1
    out = st.classify(0.20, 0.20, FLOOR, live_obs=thin)
    assert out["tier"] != st.SUPPRESSED_STRUCTURAL
    assert out["reason"] == st.REASON_STRUCTURAL_NEEDS_LIVE_OBS
    # It is still judged on its CURRENT reading, which is always live.
    assert out["tier"] == st.SUPPRESSED_CONDITION

    enough = config.SUPPRESS_STRUCTURAL_MIN_LIVE_OBS
    assert st.classify(0.20, 0.20, FLOOR,
                       live_obs=enough)["tier"] == st.SUPPRESSED_STRUCTURAL


# ---------------------------------------------------------------------------
# 1.7 — the motivating case: ET/XLE-shaped
# ---------------------------------------------------------------------------
def test_et_xle_shaped_name_is_structural_and_loses_its_bench_slot(monkeypatch):
    """The case that motivated the whole feature: a name at ~0.30%/wk capacity
    against a 0.70 floor, reaching BENCH with a displayed path to READY that
    leads nowhere. Enforced, it must be absent from the main scan, present in
    the suppressed set, and NOT benched — whatever its Level-4 status."""
    from metrics import scorecard

    _enforce(monkeypatch)
    row = _row("ET", 0.30, 0.30, floor=0.70, verdict="WATCH", bench=True,
               gate_cleared_level=4, right_spot=True)
    st.record_classification([row], day="2026-08-24")
    assert st.current_tier("ET") == st.SUPPRESSED_STRUCTURAL

    shown, suppressed, info = scorecard.split_by_suitability([row])
    assert [r["ticker"] for r in shown] == []
    assert [r["ticker"] for r in suppressed] == ["ET"]
    assert info["enforced"] is True
    # No path-to-ready is advertised for a name that cannot clear the floor.
    assert suppressed[0]["bench"] is False


def test_compression_shaped_name_is_condition_with_a_weekly_recheck():
    row = _row("AAA", 0.85, 0.40, floor=0.70)
    st.record_classification([row], day="2026-08-24")
    assert st.current_tier("AAA") == st.SUPPRESSED_CONDITION
    ev = st.events("AAA")[-1]
    assert ev["next_recheck_date"] == "2026-08-31"          # +7 days


def test_recheck_below_readmit_stays_suppressed_above_readmit_readmits():
    """Both directions of the recheck, per the prompt's worked example."""
    st.record_classification([_row("AAA", 0.85, 0.40, floor=0.70)], day="2026-08-24")
    assert st.current_tier("AAA") == st.SUPPRESSED_CONDITION

    # Recovered to 0.65 vs a 0.70 floor = 0.93 of it — above the 0.80 suppress
    # bar but BELOW the 1.00 readmit bar. Still suppressed.
    st.record_classification([_row("AAA", 0.85, 0.65, floor=0.70)], day="2026-08-31")
    assert st.current_tier("AAA") == st.SUPPRESSED_CONDITION

    # Recovered to 0.72 vs 0.70 = 1.03 of it. Readmitted, with an event.
    st.record_classification([_row("AAA", 0.85, 0.72, floor=0.70)], day="2026-09-07")
    assert st.current_tier("AAA") == st.SUITABLE
    assert st.events("AAA")[-1]["to_tier"] == st.SUITABLE


def test_structural_recheck_is_monthly():
    st.record_classification([_row("XLE", 0.34, 0.34, floor=0.70)], day="2026-08-24")
    assert st.events("XLE")[-1]["next_recheck_date"] == "2026-09-23"   # +30 days


# ---------------------------------------------------------------------------
# 1.2 — transition events; the tier is DERIVED from them
# ---------------------------------------------------------------------------
def test_initial_classification_is_a_batch_of_unclassified_transitions():
    rows = [_row("AAA", 1.0, 1.0), _row("BBB", 0.20, 0.20), _row("CCC", 0.85, 0.10)]
    out = st.record_classification(rows, day="2026-08-24")
    assert out["ok"] and out["classified"] == 3 and out["transitions"] == 3
    assert all(e["from_tier"] == st.UNCLASSIFIED for e in st.events())


def test_every_event_carries_the_full_transition_payload():
    st.record_classification([_row("ET", 0.30, 0.31, floor=0.70)], day="2026-08-24")
    ev = st.events("ET")[-1]
    for key in ("symbol", "from_tier", "to_tier", "capacity", "current", "floor",
                "at", "next_recheck_date", "reason"):
        assert key in ev, f"event missing {key}"
    assert ev["capacity"] == 0.30 and ev["floor"] == 0.70


def test_current_tier_is_derivable_from_the_event_stream_alone():
    """No mutable current-tier field exists to drift out of step — the fold IS
    the derivation."""
    st.record_classification([_row("AAA", 0.85, 0.10)], day="2026-08-24")
    st.record_classification([_row("AAA", 0.85, 1.00)], day="2026-09-01")
    st.record_classification([_row("AAA", 0.20, 0.20)], day="2026-10-01")

    trail = [(e["from_tier"], e["to_tier"]) for e in st.events("AAA")]
    assert trail == [(st.UNCLASSIFIED, st.SUPPRESSED_CONDITION),
                     (st.SUPPRESSED_CONDITION, st.SUITABLE),
                     (st.SUITABLE, st.SUPPRESSED_STRUCTURAL)]
    assert st.current_tier("AAA") == trail[-1][1]

    # Derived on every read, from the stored events, with no memo in the way.
    st._parsed = None
    assert st.current_tier("AAA") == st.SUPPRESSED_STRUCTURAL
    assert "tier" not in st._load_raw()          # only `events` is persisted


def test_an_unchanged_tier_appends_nothing():
    """The stream is a transition log, not a daily snapshot — otherwise it would
    grow by the whole universe every night and "what changed" would be unreadable."""
    row = _row("AAA", 1.0, 1.0)
    st.record_classification([row], day="2026-08-24")
    out = st.record_classification([row], day="2026-08-25")
    assert out["transitions"] == 0
    assert len(st.events("AAA")) == 1


# ---------------------------------------------------------------------------
# 1.3 — manual recheck
# ---------------------------------------------------------------------------
def test_manual_recheck_forces_immediate_reclassification_and_an_event():
    """Suppression must never leave a name unreachable pending a date."""
    st.record_classification([_row("AAA", 0.85, 0.10, floor=0.70)], day="2026-08-24")
    assert st.current_tier("AAA") == st.SUPPRESSED_CONDITION

    out = st.recheck("AAA", row=_row("AAA", 0.85, 0.90, floor=0.70), day="2026-08-25")
    assert out["ok"] and out["changed"] is True
    assert out["tier"] == st.SUITABLE and out["from_tier"] == st.SUPPRESSED_CONDITION
    assert st.events("AAA")[-1]["to_tier"] == st.SUITABLE


def test_manual_recheck_that_changes_nothing_appends_no_event():
    st.record_classification([_row("AAA", 0.85, 0.10, floor=0.70)], day="2026-08-24")
    before = len(st.events("AAA"))
    out = st.recheck("AAA", row=_row("AAA", 0.85, 0.10, floor=0.70), day="2026-08-25")
    assert out["ok"] and out["changed"] is False
    assert len(st.events("AAA")) == before


# ---------------------------------------------------------------------------
# 1.5 — shadow-first rollout
# ---------------------------------------------------------------------------
def test_shadow_is_inert_scan_contents_and_bench_are_unchanged():
    """Enforcement off: contents, ORDER and bench statuses byte-identical to
    pre-feature behaviour, with tier chips present."""
    from metrics import scorecard
    rows = [_row("ET", 0.30, 0.30), _row("AAA", 1.0, 1.0), _row("BBB", 0.85, 0.10)]
    st.record_classification(rows, day="2026-08-24")
    before = [dict(r) for r in rows]

    shown, suppressed, info = scorecard.split_by_suitability(rows)
    assert info["enforced"] is False
    assert suppressed == []
    assert [r["ticker"] for r in shown] == [r["ticker"] for r in before]
    # Bench survives untouched even for the structural name.
    assert all(s["bench"] == b["bench"] for s, b in zip(shown, before))
    # ...and the classification is nonetheless visible.
    assert shown[0]["suitability_tier"] == st.SUPPRESSED_STRUCTURAL
    assert shown[0]["suitability_tier_enforced"] is False
    # The header still reports what WOULD be hidden — the point of a shadow run.
    assert info["counts"][st.SUPPRESSED_STRUCTURAL] == 1


def test_split_never_mutates_the_caller_s_rows(monkeypatch):
    """`scorecard()` serves rows straight out of the memoized day cache, so those
    dicts are SHARED. Clearing `bench` in place would write suppression into the
    cached sweep, leak it to every later reader, and leave it stuck there if
    enforcement were switched back off."""
    from metrics import scorecard

    _enforce(monkeypatch)
    st.record_classification([_row("ET", 0.30, 0.30, floor=0.70)], day="2026-08-24")
    cached = _row("ET", 0.30, 0.30, floor=0.70, bench=True)
    snapshot = dict(cached)

    shown, suppressed, _info = scorecard.split_by_suitability([cached])
    assert suppressed and suppressed[0]["bench"] is False    # the copy is suppressed
    assert cached == snapshot, "the caller's row was mutated"
    assert cached["bench"] is True
    assert "suitability_tier" not in cached


def test_enforcement_needs_the_flag_a_dated_review_and_the_shadow_period(monkeypatch):
    """Three conditions, none automated. The clock runs from the CAPACITY REVIEW,
    not the deploy: before that review every name reads INSUFFICIENT_HISTORY and
    classifies SUITABLE, so a shadow period started at deploy observes nothing."""
    assert st.enforcement("2026-08-24")["active"] is False      # flag off

    monkeypatch.setattr(config, "SUPPRESSION_ENFORCE", True)
    gate = st.enforcement("2026-08-24")
    assert gate["active"] is False and "no dated capacity review" in gate["reason"]

    # Review dated, but inside the shadow window.
    monkeypatch.setattr(config, "SUPPRESSION_REVIEW_DATE", "2026-08-20")
    gate = st.enforcement("2026-08-24")
    assert gate["active"] is False and gate["shadow_days_elapsed"] == 4
    assert "shadow period" in gate["reason"]

    # One day short.
    short = st.enforcement("2026-09-02")
    assert short["shadow_days_elapsed"] == config.SUPPRESSION_SHADOW_DAYS - 1
    assert short["active"] is False

    # Exactly the required days.
    assert st.enforcement("2026-09-03")["active"] is True


def test_unparseable_review_date_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "SUPPRESSION_ENFORCE", True)
    monkeypatch.setattr(config, "SUPPRESSION_REVIEW_DATE", "not-a-date")
    assert st.enforcement("2026-08-24")["active"] is False


# ---------------------------------------------------------------------------
# 1.6 — THE HARD SAFETY INVARIANT
# ---------------------------------------------------------------------------
def test_no_position_management_path_reads_a_tier():
    """[SUPPRESSION_IS_ENTRY_ONLY] as an executable assertion.

    Suppression must be architecturally confined to scan intake/visibility. A
    tier reference appearing in the kill switch, position manager, reconciler,
    spread monitor, portfolio risk, order lifecycle or accrual is the failure
    this pins. Keyed on the IMPORT, so a cross-referencing comment is not a
    violation."""
    import ast
    import pathlib
    backend = pathlib.Path(__file__).parent
    allowed = {"suitability_tiers.py",     # the module itself
               "metrics/scorecard.py",     # the visibility choke point
               "maintenance.py",           # the nightly classification pass
               "app.py"}                   # the entry-facing API + recheck route
    entry_only = {"recommendation_runner.py"}   # ENTER candidates; see below
    offenders = []
    for path in sorted(backend.rglob("*.py")):
        rel = path.relative_to(backend).as_posix()
        if rel.startswith("test_") or "/test_" in rel:
            continue
        if rel in allowed or rel in entry_only:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                    a.name == "suitability_tiers" for a in node.names):
                offenders.append(rel)
            elif isinstance(node, ast.ImportFrom) and node.module == "suitability_tiers":
                offenders.append(rel)
    assert offenders == [], f"suppression leaked into: {offenders}"


def test_position_management_paths_derive_from_positions_not_scan_rows():
    """The structural reason the invariant holds: every position path takes its
    working set from state["positions"]. Pinned so a future refactor that keys
    one off scan membership fails here rather than in production."""
    import ast
    import pathlib
    backend = pathlib.Path(__file__).parent
    forbidden = {"scorecard", "scan_cache", "queue_state"}
    for module in ("kill_switch.py", "position_manager.py", "reconcile.py",
                   "spread_monitor.py", "portfolio_risk.py", "order_lifecycle.py",
                   "accrual.py"):
        tree = ast.parse((backend / module).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
                imported.update(a.name for a in node.names)
        leaked = imported & forbidden
        assert not leaked, f"{module} imports scan machinery: {leaked}"


def _open_position_state(ticker="ET"):
    return {
        "schema_version": 21,
        "positions": [{
            "ticker": ticker, "status": "open", "sector": "XLE",
            "entry_date": "2026-06-01", "leap_legs": [], "short_calls": [],
            "shares": {"count": 100, "cost_basis_per_share": 17.0},
        }],
        "executions": [], "cycles": [],
    }


def test_open_position_paths_are_byte_identical_under_suppression(monkeypatch):
    """THE critical test. A name classified SUPPRESSED_STRUCTURAL with
    enforcement ON must produce byte-identical position-management output to the
    same fixture with suppression disabled: the kill switch still fires,
    defend/roll still evaluates, reconciliation still sees the position."""
    from datetime import datetime, timezone

    import kill_switch
    import portfolio_risk
    import recommendation_engine
    import recommendation_runner
    import reconcile

    bars = _bars()
    monkeypatch.setattr("data_handler.get_daily", lambda t, force=False: bars)
    monkeypatch.setattr("data_handler.live_price", lambda t: float(bars["Close"].iloc[-1]))
    state = _open_position_state("ET")
    now = datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)

    def _outputs():
        # The defend/roll path: build the position snapshot the engine consumes,
        # then evaluate it. include_entry=False isolates POSITION management from
        # the entry sweep, which is the half suppression is allowed to touch.
        market = recommendation_runner.build_market_snapshot(state, include_entry=False)
        recs = recommendation_engine.evaluate(market, state, now)
        # live_only=False: the fixture has no transmitted orders behind it, and
        # broker-liveness is orthogonal to suppression. What matters here is that
        # reconciliation still derives the position's expected holdings.
        expected, _extra = reconcile.expected_view_from_state(state, live_only=False)
        return {
            "kill_switch": kill_switch.evaluate_all(state),
            "defend_roll": recs,
            "risk": portfolio_risk.portfolio_view(state),
            "reconcile_expected": expected,
            "market_tickers": sorted(market["tickers"]),
        }

    # Suppression disabled entirely.
    baseline = _outputs()
    # Guard against a vacuous pass: each path must actually produce something
    # about ET, or "identical" would only mean "identically empty".
    assert len(baseline["kill_switch"]) == 1
    assert baseline["kill_switch"][0]["ticker"] == "ET"
    assert baseline["kill_switch"][0].get("status")
    assert baseline["market_tickers"] == ["ET"]
    assert baseline["reconcile_expected"], "reconciliation must see the position"
    assert baseline["risk"]

    # Now classify ET structural AND turn enforcement on.
    _enforce(monkeypatch)
    st.record_classification([_row("ET", 0.30, 0.30, floor=0.70)], day="2026-08-24")
    assert st.current_tier("ET") == st.SUPPRESSED_STRUCTURAL
    assert st.enforcing("2026-08-24") is True

    assert _outputs() == baseline


def test_a_suppressed_name_with_an_open_position_keeps_its_refresh_priority(monkeypatch):
    """refresh_policy Tier 1 is open positions, never truncated. A suppressed
    name holding a position must keep its intraday bar refresh."""
    import refresh_policy
    _enforce(monkeypatch)
    st.record_classification([_row("ET", 0.30, 0.30, floor=0.70)], day="2026-08-24")
    monkeypatch.setattr(refresh_policy, "_candidate_rows", lambda: [])
    assert "ET" in refresh_policy.hot_tickers(_open_position_state("ET"))


# ---------------------------------------------------------------------------
# Bar-ingestion + observation continuity
# ---------------------------------------------------------------------------
def _bars(n: int = 300, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": np.full(n, 1_000_000.0),
    }, index=pd.bdate_range("2025-01-01", periods=n))


def test_bar_ingestion_covers_suppressed_names_because_prefetch_precedes_the_loop():
    """A suppressed name's price history must stay warm, or readmission would
    return it as INSUFFICIENT_DATA on structure and INST. The structural reason
    it does: `_compute_scorecard` prefetches EVERY name in one batch BEFORE the
    per-name loop, so bar ingestion is not downstream of evaluation. Pinned so a
    refactor that moves the prefetch inside the loop fails here."""
    import inspect
    from metrics import scorecard
    src = inspect.getsource(scorecard._compute_scorecard)
    assert src.index("data_handler.prefetch(") < src.index("for t in names:"), \
        "bar prefetch must precede the per-name loop, or suppression could starve it"


def test_suppressed_names_still_emit_capacity_observations(monkeypatch):
    """Observation continuity: suppression governs VISIBILITY, not evaluation, so
    a suppressed name keeps producing observations daily. Its median stays dense
    and its `current` reading stays today's — a scheme that starved its own input
    would eventually flip a structural name back to INSUFFICIENT_HISTORY (i.e.
    un-suppress it)."""
    _enforce(monkeypatch)
    row = _row("ET", 0.30, 0.31, floor=0.70)
    st.record_classification([row], day="2026-08-24")
    assert st.current_tier("ET") == st.SUPPRESSED_STRUCTURAL

    # The nightly emitter takes the sweep's rows, which still contain ET.
    for i, day in enumerate(("2026-08-25", "2026-08-26", "2026-08-27")):
        jc.record_scan([{"ticker": "ET", "juice_weekly_pct": 0.14 + i * 0.01}], day=day)
    dates = [o["date"] for o in jc.series("ET")]
    assert dates == ["2026-08-25", "2026-08-26", "2026-08-27"]   # no gap


def test_manual_recheck_emits_a_fresh_observation_via_the_full_evaluation(monkeypatch):
    """The recheck runs the complete scan evaluation, so the observation series
    continues at recheck cadence rather than stopping dead."""
    from metrics import scorecard

    bars = _bars()
    monkeypatch.setattr("data_handler.get_daily", lambda t, force=False: bars)
    monkeypatch.setattr("data_handler.prefetch", lambda *a, **k: None)
    calls = []

    real = scorecard.scorecard

    def _spy(tickers=None, **kw):
        calls.append(tickers)
        return real(tickers, **kw)

    monkeypatch.setattr(scorecard, "scorecard", _spy)
    st.recheck("AAA", day="2026-08-25")
    assert calls == [["AAA"]], "recheck must run the full per-name evaluation"


# ---------------------------------------------------------------------------
# Canonical fixtures
# ---------------------------------------------------------------------------
def test_canonical_xlk_july6_fixture_is_unperturbed(monkeypatch):
    """The XLK July 6th regression is an entry-GATE fixture. Suppression is a
    visibility filter applied after the gate, so gate composition must be
    identical whatever XLK's tier — asserted with XLK classified STRUCTURAL and
    enforcement ON, the most hostile configuration.

    (Prompt 2 also names a "GDDY Aug 21" fixture. No such fixture exists
    anywhere in this repo — see AUDIT_SUITABILITY_SUPPRESSION_PHASE0.md §8.6 —
    so it is not asserted here rather than invented.)"""
    from metrics import scorecard

    _enforce(monkeypatch)
    st.record_classification([_row("XLK", 0.10, 0.10, floor=0.70)], day="2026-08-24")
    assert st.current_tier("XLK") == st.SUPPRESSED_STRUCTURAL

    row = _row("XLK", 0.10, 0.10, floor=0.70, verdict="BLOCKED", bench=False,
               verdict_reasons=["structure:BLOCKED"], gate_cleared_level=2)
    before = {k: row[k] for k in ("verdict", "verdict_reasons", "gate_cleared_level")}
    scorecard.split_by_suitability([row])
    assert {k: row[k] for k in before} == before


def test_threshold_constants_are_centralized_with_no_literals_at_use_sites():
    """Threshold provenance: every ratio and cadence lives in config, tagged.
    A bare literal at a use site is how a calibrated number silently drifts."""
    import pathlib
    src = (pathlib.Path(__file__).parent / "suitability_tiers.py").read_text(encoding="utf-8")
    for name in ("SUPPRESS_STRUCTURAL_CAPACITY_RATIO", "SUPPRESS_CONDITION_CURRENT_RATIO",
                 "READMIT_CURRENT_RATIO", "STRUCTURAL_READMIT_CAPACITY_RATIO",
                 "RECHECK_CONDITION_DAYS", "RECHECK_STRUCTURAL_DAYS",
                 "SUPPRESS_STRUCTURAL_MIN_LIVE_OBS", "SUPPRESSION_SHADOW_DAYS"):
        assert hasattr(config, name), f"{name} missing from config"
        assert f"config.{name}" in src, f"{name} not read through config at its use site"
    for literal in ("0.60", "0.80", "1.00", "0.70", " 7", " 30"):
        assert f"= {literal}" not in src, f"bare threshold literal {literal} in the module"
