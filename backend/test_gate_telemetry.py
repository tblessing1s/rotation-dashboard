"""Gate rejection telemetry — emission, aggregation, and the safety invariant.

The PRIMARY test here is not the dashboard: it is that telemetry perturbs
NOTHING. A calibration instrument that changes the thing it measures has failed
regardless of how good the readout looks, so the canonical fixtures must produce
byte-identical gate outcomes with the telemetry path exercised.

Offline throughout: fixture parquet frames, monkeypatched providers, a tmp_path
store. No network, no live Schwab, no clock dependence in the aggregation.
"""
from __future__ import annotations

import json
import os

import pandas as pd
import pytest

import config
import gate_telemetry as gt

FIX_REGIME = os.path.join(os.path.dirname(__file__), "fixtures", "regime")
FIX_STRUCT = os.path.join(os.path.dirname(__file__), "fixtures", "structure")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated per-day telemetry store."""
    d = tmp_path / "gate_telemetry"
    monkeypatch.setattr(gt, "STORE_DIR", str(d))
    return d


# ===========================================================================
# Helpers — synthetic gate dicts with hand-computed outcomes.
# ===========================================================================
def _gate(*, regime="green", rs3m_vs_spy=1.0, below_ma50=False, below_ma200=False,
          has_weeklies=True, stale=False, account_gate=None,
          greens=4, entrability="READY", atr_5d=0.9, extension=0.5,
          sector_rs1m=1.0, sector_breadth=60.0, inst_flow="ACCUMULATING"):
    """A synthetic entry-gate dict shaped exactly like ``screening.entry_gate``'s,
    carrying only what ``build_results`` reads. Defaults are an all-pass candidate.

    Note what is NO LONGER an axis of this helper's VETO output: sector strength,
    sector breadth, sector distribution, the light vote, structure entrability and
    the right spot. They are ranking inputs now, so they arrive through ``ranking``
    and can never appear in ``blocks``. That asymmetry is the change, expressed in
    a fixture.
    """
    import scan_verdict as sv
    blocks = sv.evaluate(regime_color=regime, rs3m_vs_spy=rs3m_vs_spy,
                         below_ma50=below_ma50, below_ma200=below_ma200,
                         has_weeklies=has_weeklies, stale=stale,
                         account_gate=account_gate)
    return {
        "ticker": "TEST",
        "verdict": sv.compose(blocks)["verdict"],
        "blocks": blocks,
        "ranking": {"stock_greens": greens, "entrability": entrability,
                    "atr_momentum": atr_5d, "extension_atr": extension,
                    "sector_rs1m": sector_rs1m, "sector_breadth": sector_breadth,
                    "inst_flow": inst_flow},
    }


_ACCOUNT_EARNINGS = {"checks": [{"id": "earnings_in_cycle",
                                 "detail": {"earnings": {"date": "2026-09-10"}}}],
                     "blocking_failures": ["earnings_in_cycle"]}


def _row(ticker, gate, floor=None):
    r = {"ticker": ticker}
    if floor is not None:
        r["shadow_floor"] = floor
    r["gate_results"] = gt.build_results(gate, r)
    return r


def _by_id(results):
    return {r["gate_id"]: r for r in results}


# ===========================================================================
# 1. SAFETY — the canonical fixtures produce byte-identical gate outcomes.
#
# This is the primary test of the whole change. Telemetry that perturbs gate
# results has failed regardless of how good the dashboard looks.
# ===========================================================================
def test_xlk_july6_gate_outcome_is_byte_identical_with_telemetry():
    """The canonical XLK July-6 rollover fixture. Its lights/veto/right-spot
    outcome is pinned by test_stock_lights, test_chart_structure and
    test_gate_ruleset; here the SAME evaluation is run and then handed to
    build_results, and the outcome must be untouched afterwards."""
    import stock_lights

    df = pd.read_parquet(os.path.join(FIX_REGIME, "xlk_july6_rollover.parquet"))
    before = stock_lights.compute(df, ivr_percentile=95.0, is_etf=True)
    snapshot = json.dumps(before, sort_keys=True, default=str)

    # The IVR veto is not in the §1.1 registry — a rich-IV expanding-ATR name is
    # not something you would EXIT for, so under the governing principle it cannot
    # block an entry either. It survives as a stock-lights read (asserted below)
    # with no authority. The telemetry sees the tradeability veto instead.
    gate = _gate(greens=before["greens"], has_weeklies=False)
    results = gt.build_results(gate, {})

    after = stock_lights.compute(df, ivr_percentile=95.0, is_etf=True)
    assert json.dumps(after, sort_keys=True, default=str) == snapshot
    assert before["verdict"] == stock_lights.RED
    assert "veto:atr_expanding_high_ivr" in before["veto_reasons"]
    # And the telemetry SAW the veto it must not have caused.
    assert _by_id(results)["veto:no_weeklies"]["passed"] is False


def test_low_juice_fixture_gate_outcome_is_byte_identical_with_telemetry():
    """The `early_advance_low_juice` fixture — the repo's canonical stand-in for
    the "GDDY Aug 21" artifact, which does not exist here (see
    test_juice_capacity.py and AUDIT §Q5). Structure/RS/SYM all green; only the
    economics are wrong. The income floor is SHADOW, so it must be visible in the
    telemetry and must NOT affect admission."""
    import stock_lights

    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_low_juice.parquet"))
    before = stock_lights.compute(df, ivr_percentile=20.0, is_etf=False)
    snapshot = json.dumps(before, sort_keys=True, default=str)

    gate = _gate(greens=before["greens"])
    row = {"shadow_floor": {"pass": False, "measured_pct": 0.41,
                            "floor_pct": 0.75, "basis": "juice"}}
    results = gt.build_results(gate, row)

    after = stock_lights.compute(df, ivr_percentile=20.0, is_etf=False)
    assert json.dumps(after, sort_keys=True, default=str) == snapshot
    # Shadow floor failing does NOT cost admission.
    assert gt.admitted(results) is True
    assert _by_id(results)["shadow:income_floor"]["authority"] == gt.SHADOW


def test_score_ticker_row_is_unchanged_where_no_gate_was_built(monkeypatch):
    """`gate_results` is purely ADDITIVE on the scorecard row. With no gate there
    are no results, and every authority-bearing field is produced exactly as
    before — the telemetry key can neither add nor remove a verdict."""
    import data_handler
    import dividends
    from metrics import scorecard as sc

    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_accum.parquet"))
    spy = pd.read_parquet(os.path.join(FIX_REGIME, "sustained_green.parquet"))
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    monkeypatch.setattr(dividends, "cached_annual_yield_pct",
                        lambda t, state=None: None)
    row = sc.score_ticker("TEST", spy, "XLK", spy, gate=None)
    assert row.get("gate_results") in (None, [])
    assert row["verdict"] in ("ELIGIBLE", "BLOCKED")
    assert row["suitability"] in ("GO", "CAUTION", "AVOID")


# ===========================================================================
# 2. Synthetic multi-candidate scan — exact hand-computed rates.
# ===========================================================================
@pytest.fixture
def hand_scan(store):
    """Five candidates with hand-computed veto outcomes, one scan run.

      AAA — all pass                          -> admitted
      BBB — ONLY RS3M-vs-SPY fails            -> sole block: veto:rs3m_vs_spy
      CCC — ONLY RS3M-vs-SPY fails            -> sole block: veto:rs3m_vs_spy
      DDD — regime RED **and** below MA200    -> co-block pair
      EEE — ONLY regime RED                   -> sole block: veto:regime_red

    Every candidate is evaluated against the WHOLE veto registry (10 gates), which
    is what keeps the sole-blocker rate computable: there is no stop-on-first-fail
    anywhere in the new path, so "this veto failed and no other did" is answerable
    for every name in every run.
    """
    rows = [
        _row("AAA", _gate()),
        _row("BBB", _gate(rs3m_vs_spy=-1.0)),
        _row("CCC", _gate(rs3m_vs_spy=-1.0)),
        _row("DDD", _gate(regime="red", below_ma200=True)),
        _row("EEE", _gate(regime="red")),
    ]
    gt.record_scan(rows, scan_id="run-1", day="2026-08-10")
    return rows


def test_evaluated_n_and_admitted(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    assert agg["evaluated_n"] == 5
    assert agg["admitted_n"] == 1
    assert agg["runs"] == 1 and agg["days"] == 1


def test_exact_block_and_sole_blocker_rates(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}

    ext = g["veto:rs3m_vs_spy"]
    assert ext["evaluated_n"] == 5
    assert ext["failed_n"] == 2                       # BBB, CCC
    assert ext["block_rate"] == 0.4                   # 2/5
    assert ext["sole_blocker_n"] == 2
    assert ext["sole_blocker_rate"] == 0.4

    reg = g["veto:regime_red"]
    assert reg["failed_n"] == 2                       # DDD, EEE
    assert reg["block_rate"] == 0.4
    assert reg["sole_blocker_n"] == 1                 # EEE only (DDD co-fires)
    assert reg["sole_blocker_rate"] == 0.2

    dist = g["veto:close_below_ma200"]
    assert dist["failed_n"] == 1                      # DDD
    assert dist["block_rate"] == 0.2
    assert dist["sole_blocker_n"] == 0                # never alone
    assert dist["sole_blocker_rate"] == 0.0

    clean = g["veto:line_in_the_sand"]
    assert clean["failed_n"] == 0 and clean["block_rate"] == 0.0
    assert clean["sole_blocker_rate"] == 0.0


def test_sorted_by_sole_blocker_rate_descending(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    rated = [row for row in agg["gates"] if not row["indeterminate"]]
    rates = [row["sole_blocker_rate"] for row in rated]
    assert rates == sorted(rates, reverse=True)
    assert rated[0]["gate_id"] == "veto:rs3m_vs_spy"
    # Indeterminate rows sort LAST — never floated to the top on a null.
    assert all(row["indeterminate"] for row in agg["gates"][len(rated):])


def test_exact_co_block_matrix(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    m = agg["co_block_matrix"]
    # DDD is the only candidate with two veto failures.
    assert m["veto:regime_red"]["veto:close_below_ma200"] == 1
    assert m["veto:close_below_ma200"]["veto:regime_red"] == 1
    # Symmetric, and zero everywhere else.
    assert m["veto:rs3m_vs_spy"]["veto:regime_red"] == 0
    assert m["veto:regime_red"]["veto:rs3m_vs_spy"] == 0
    for a, rowm in m.items():
        for b, n in rowm.items():
            assert n == m[b][a], f"{a}/{b} asymmetric"


def test_near_miss_distribution_is_over_failures_only(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}
    nm = g["veto:rs3m_vs_spy"]["near_miss"]
    # RS3M-vs-SPY's threshold IS zero (the kill-switch line), so a FRACTIONAL
    # distance from it is undefined and the raw distance is reported instead —
    # the same discipline the old zero-threshold sector gate got. Both failures
    # sat at -1.0, i.e. 1.0 past the line.
    assert nm["normalized"] is False and nm["n"] == 0
    assert nm["raw_n"] == 2 and round(nm["raw_median"], 4) == 1.0
    # Passing candidates contribute nothing.
    assert g["veto:no_weeklies"]["near_miss"]["raw_n"] == 0


def test_weekly_time_series(hand_scan):
    rows = [_row("FFF", _gate(rs3m_vs_spy=-1.0))]
    gt.record_scan(rows, scan_id="run-2", day="2026-08-17")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    series = agg["time_series"]["veto:rs3m_vs_spy"]
    assert [p["week"] for p in series] == ["2026-08-10", "2026-08-17"]
    assert series[0]["block_rate"] == 0.4 and series[0]["evaluated_n"] == 5
    assert series[1]["block_rate"] == 1.0 and series[1]["sole_blocker_rate"] == 1.0


# ===========================================================================
# 3. A shadow gate failing beside a passing veto stack is NOT a sole block.
#    Getting this wrong inverts the entire diagnostic.
# ===========================================================================
def test_shadow_failure_does_not_block_or_register_as_a_sole_block(store):
    rows = [_row("AAA", _gate(), floor={"pass": False, "measured_pct": 0.30,
                                        "floor_pct": 0.75, "basis": "juice"})]
    gt.record_scan(rows, scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}

    # The candidate is ADMITTED — every veto gate passed.
    assert agg["admitted_n"] == 1
    floor = g["shadow:income_floor"]
    assert floor["authority"] == gt.SHADOW
    assert floor["block_rate"] == 1.0            # it did flag
    # ...but a shadow flag is NOT a block: no sole-blocker rate at all, and it
    # never appears in the veto-only co-block matrix.
    assert floor["sole_blocker_rate"] is None
    assert floor["sole_blocker_n"] is None
    assert "shadow:income_floor" not in agg["co_block_matrix"]


def test_shadow_gate_never_suppresses_a_real_sole_block(store):
    """A veto failing alongside a shadow failure is still a SOLE block — the
    shadow flag must not be counted as "another gate failed"."""
    rows = [_row("BBB", _gate(rs3m_vs_spy=-1.0),
                 floor={"pass": False, "measured_pct": 0.1, "floor_pct": 0.75,
                        "basis": "juice"})]
    gt.record_scan(rows, scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}
    assert g["veto:rs3m_vs_spy"]["sole_blocker_rate"] == 1.0


# ===========================================================================
# 4. Short-circuit conditions -> explicit `indeterminate`, never an imputed value.
# ===========================================================================
def test_level5_gates_are_indeterminate_not_zero(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}
    for gid in ("L5:cash_reserve", "L5:position_limit", "L5:capital_limit",
                "L5:sector_concentration", "L5:earnings_in_cycle",
                "L5:round_lot_size", "L5:juice_rich", "L5:ex_div_in_cycle",
                "L5:juice_adequacy", "L5:juice_floor"):
        row = g[gid]
        assert row["indeterminate"] is True, gid
        # Explicitly NOT zero — the two must never be confused at a glance.
        assert row["block_rate"] is None, gid
        assert row["sole_blocker_rate"] is None, gid
        assert row["indeterminate_reason"], gid
        # Never silently dropped from the table.
        assert row["authority"] in (gt.VETO, gt.RANK, gt.SHADOW), gid


def test_juice_floor_records_why_it_can_never_fire():
    """The LEAP-denominated NET juice veto returns None unconditionally in shares
    mode, so a 0% block rate would be an artefact, not evidence. It is recorded
    as inactive rather than as a veto that never binds."""
    entry = gt._juice_floor_entry()
    assert entry["authority"] == gt.VETO
    assert entry["reason"] == ("inactive_shares_mode" if config.LEGACY_LEAP_READONLY
                               else "not_evaluated_in_scan")


def test_an_unevaluated_veto_blocks_sole_attribution(store):
    """When some veto gate did not run for a candidate, no sole block may be
    attributed from that candidate — an unevaluated gate is an unknown, never an
    assumed pass."""
    row = _row("AAA", _gate(rs3m_vs_spy=-1.0))
    # Simulate a gate that ran but produced no verdict.
    row["gate_results"].append({"gate_id": "L9:unknown", "level": 9,
                                "authority": gt.VETO, "label": "unknown",
                                "passed": None, "value": None,
                                "threshold": None, "direction": None})
    gt.record_scan([row], scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["veto:rs3m_vs_spy"]["failed_n"] == 1
    assert g["veto:rs3m_vs_spy"]["sole_blocker_n"] == 0     # not imputed
    # The unknown gate is counted as neither pass nor fail.
    assert g["L9:unknown"]["evaluated_n"] == 0


# ===========================================================================
# 5. Ruleset segmentation — never pool.
# ===========================================================================
def test_never_pools_across_rulesets(store):
    """Segmentation survives the ruleset switch's deletion, and matters MORE now:
    runs recorded under the old legacy/proposed filter are not comparable to runs
    recorded under the veto set, so the aggregation must refuse to merge them."""
    gt.record_scan([_row("AAA", _gate(rs3m_vs_spy=-1.0))],
                   scan_id="r1", day="2026-08-10", ruleset="legacy")
    gt.record_scan([_row("BBB", _gate())],
                   scan_id="r2", day="2026-08-11", ruleset="proposed")

    pooled = gt.aggregate(start="2026-08-01", end="2026-08-31")
    assert pooled["pooled"] is False
    assert pooled["gates"] == []                       # refuses to merge
    assert pooled["rulesets_present"] == {"legacy": 1, "proposed": 1}
    assert "ruleset" in (pooled.get("note") or "")

    legacy = gt.aggregate(start="2026-08-01", end="2026-08-31",
                          gate_ruleset="legacy")
    assert legacy["evaluated_n"] == 1
    assert {g["gate_id"]: g for g in legacy["gates"]}["veto:rs3m_vs_spy"]["failed_n"] == 1

    proposed = gt.aggregate(start="2026-08-01", end="2026-08-31",
                            gate_ruleset="proposed")
    assert proposed["evaluated_n"] == 1
    assert {g["gate_id"]: g for g in proposed["gates"]}["veto:rs3m_vs_spy"]["failed_n"] == 0


def test_empty_store_reports_absence_and_does_not_error(store):
    agg = gt.aggregate(start="2026-01-01", end="2026-01-31")
    assert agg["evaluated_n"] == 0
    assert agg["gates"] == []
    assert agg["low_confidence"] is True
    assert agg["first_day"] is None and agg["last_day"] is None


def test_older_state_without_telemetry_loads_cleanly(store, tmp_path, monkeypatch):
    """A state.json predating this feature carries no telemetry. The reader must
    degrade to "no telemetry available for this period", never crash — and
    nothing about state.json is touched by this feature at all."""
    import logging_handler as log
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"schema_version": 21, "executions": [],
                             "positions": [], "metadata": {}}))
    monkeypatch.setattr(config, "STATE_FILE", str(p), raising=False)
    state = json.loads(p.read_text())
    assert "gate_telemetry" not in state
    agg = gt.aggregate(start="2026-01-01", end="2026-01-31")
    assert agg["evaluated_n"] == 0 and agg["gates"] == []
    assert log is not None


def test_corrupt_day_file_is_skipped_not_fatal(store):
    gt.record_scan([_row("AAA", _gate())], scan_id="r", day="2026-08-10")
    os.makedirs(str(store), exist_ok=True)
    with open(os.path.join(str(store), "2026-08-11.json"), "w") as fh:
        fh.write("{not json")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    assert agg["evaluated_n"] == 1


# ===========================================================================
# 7. Emission discipline — append-only, idempotent per run, never raises.
# ===========================================================================
def test_append_only_across_runs_and_idempotent_within_one(store):
    rows = [_row("AAA", _gate())]
    gt.record_scan(rows, scan_id="run-1", day="2026-08-10")
    gt.record_scan(rows, scan_id="run-1", day="2026-08-10")   # retry of THIS run
    gt.record_scan(rows, scan_id="run-2", day="2026-08-10")   # a second run
    data = json.loads((store / "2026-08-10.json").read_text())
    assert [r["scan_run_id"] for r in data["runs"]] == ["run-1", "run-2"]
    assert gt.aggregate(start="2026-08-01", end="2026-08-31")["evaluated_n"] == 2


def test_prior_runs_are_never_mutated(store):
    gt.record_scan([_row("AAA", _gate())], scan_id="run-1", day="2026-08-10")
    first = json.loads((store / "2026-08-10.json").read_text())["runs"][0]
    gt.record_scan([_row("BBB", _gate(rs3m_vs_spy=-1.0))], scan_id="run-2",
                   day="2026-08-10")
    after = json.loads((store / "2026-08-10.json").read_text())["runs"][0]
    assert after == first


def test_record_scan_never_raises_into_its_caller(store, monkeypatch):
    monkeypatch.setattr(gt, "_save_day",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = gt.record_scan([_row("AAA", _gate())], scan_id="r", day="2026-08-10")
    assert out["ok"] is False and "disk full" in out["error"]


def test_rows_without_gate_results_are_skipped_not_recorded_as_failures(store):
    out = gt.record_scan([{"ticker": "AAA"}, {"ticker": "BBB", "gate_results": None}],
                         scan_id="r", day="2026-08-10")
    assert out["ok"] is True and out["recorded"] == 0


def test_retention_prunes_by_day_file(store, monkeypatch):
    monkeypatch.setattr(config, "GATE_TELEMETRY_RETENTION_DAYS", 2)
    for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
        gt.record_scan([_row("AAA", _gate())], scan_id="r", day=d)
    assert gt.stored_days() == ["2026-08-02", "2026-08-03"]


# ===========================================================================
# 8. Authority is carried on the record, not looked up at read time.
# ===========================================================================
def test_authority_is_read_from_the_record_not_live_config(store, monkeypatch):
    """A gate that graduates out of shadow mode must not retroactively rewrite the
    rows recorded while it had none."""
    rows = [_row("AAA", _gate(), floor={"pass": False, "measured_pct": 0.1,
                                        "floor_pct": 0.75, "basis": "juice"})]
    gt.record_scan(rows, scan_id="r", day="2026-08-10")
    # The authority lives on the RUN's own manifest, written at record time.
    stored = json.loads((store / "2026-08-10.json").read_text())
    manifest = {g["gate_id"]: g for g in stored["runs"][0]["gates"]}
    assert manifest["shadow:income_floor"]["authority"] == gt.SHADOW
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["shadow:income_floor"]["authority"] == gt.SHADOW
    assert g["shadow:income_floor"]["sole_blocker_rate"] is None


def test_authority_change_within_a_range_is_flagged(store):
    row_a = _row("AAA", _gate())
    row_a["gate_results"].append({"gate_id": "X:gate", "level": 4,
                                  "authority": gt.SHADOW, "label": "x",
                                  "passed": False, "value": 1.0,
                                  "threshold": 2.0, "direction": gt.LOWER})
    row_b = _row("BBB", _gate())
    row_b["gate_results"].append({"gate_id": "X:gate", "level": 4,
                                  "authority": gt.VETO, "label": "x",
                                  "passed": False, "value": 1.0,
                                  "threshold": 2.0, "direction": gt.LOWER})
    gt.record_scan([row_a], scan_id="r1", day="2026-08-10")
    gt.record_scan([row_b], scan_id="r2", day="2026-08-11")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["X:gate"].get("authority_changed_in_range") is True


# ===========================================================================
# 9. Near-miss edge cases.
# ===========================================================================
def test_zero_threshold_gate_reports_raw_distance_not_a_fabricated_ratio(store):
    """`veto:rs3m_vs_spy` has threshold 0.0 — the kill-switch line — so a
    fractional distance from it is undefined. The raw distance is reported and
    `normalized` is False rather than a ratio being invented."""
    gt.record_scan([_row("AAA", _gate(rs3m_vs_spy=-0.8))], scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    nm = {r["gate_id"]: r for r in agg["gates"]}["veto:rs3m_vs_spy"]["near_miss"]
    assert nm["n"] == 0 and nm["normalized"] is False and nm["median"] is None
    assert nm["raw_n"] == 1 and round(nm["raw_median"], 4) == 0.8


def test_non_numeric_gate_has_no_near_miss(store):
    gt.record_scan([_row("AAA", _gate(account_gate=_ACCOUNT_EARNINGS))],
                   scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {r["gate_id"]: r for r in agg["gates"]}["veto:earnings_in_cycle"]
    assert g["failed_n"] == 1
    assert g["near_miss"]["n"] == 0 and g["near_miss"]["raw_n"] == 0


def test_higher_is_better_gate_near_miss_is_positive_on_failure(store):
    """A higher-is-better gate's overshoot must be a POSITIVE "how far past the
    line" number, comparable with a lower-is-better gate's rather than signed by
    direction. RS3M-vs-SPY is the one numeric veto, and it is higher-is-better."""
    gt.record_scan([_row("AAA", _gate(rs3m_vs_spy=-2.5))],
                   scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    nm = {r["gate_id"]: r for r in agg["gates"]}["veto:rs3m_vs_spy"]["near_miss"]
    assert nm["raw_n"] == 1 and round(nm["raw_median"], 4) == 2.5


# ===========================================================================
# 10. Filters and the denominator.
# ===========================================================================
def test_symbol_universe_filter_narrows_the_denominator(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31", symbols=["BBB", "CCC"])
    assert agg["evaluated_n"] == 2
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["veto:rs3m_vs_spy"]["block_rate"] == 1.0


def test_date_range_bounds_the_denominator(hand_scan):
    gt.record_scan([_row("ZZZ", _gate())], scan_id="r9", day="2026-09-01")
    assert gt.aggregate(start="2026-08-01", end="2026-08-31")["evaluated_n"] == 5
    assert gt.aggregate(start="2026-09-01", end="2026-09-30")["evaluated_n"] == 1


def test_low_confidence_flag_tracks_the_configured_floor(hand_scan, monkeypatch):
    assert gt.aggregate(start="2026-08-01", end="2026-08-31")["low_confidence"] is True
    monkeypatch.setattr(config, "GATE_TELEMETRY_MIN_EVALUATED_N", 3)
    assert gt.aggregate(start="2026-08-01", end="2026-08-31")["low_confidence"] is False


def test_default_range_is_the_configured_lookback():
    start, end = gt.default_range(today="2026-08-25")
    assert end == "2026-08-25"
    assert start == "2026-05-28"        # 90 days inclusive
    assert config.GATE_TELEMETRY_LOOKBACK_DAYS == 90


# ===========================================================================
# 11. Provenance — no new constant may be a HARD_CFM_RULE.
# ===========================================================================
def test_new_constants_are_proposed_default_not_hard_cfm_rule():
    import re
    src = open(os.path.join(os.path.dirname(__file__), "config.py"),
               encoding="utf-8").read()
    for name in ("GATE_TELEMETRY_RETENTION_DAYS", "GATE_TELEMETRY_LOOKBACK_DAYS",
                 "GATE_TELEMETRY_NEAR_MISS_BUCKETS", "GATE_TELEMETRY_MIN_EVALUATED_N"):
        m = re.search(rf"^{name} = .*$", src, re.M)
        assert m, name
        assert "PROPOSED_DEFAULT" in m.group(0), name
        assert "HARD_CFM_RULE" not in m.group(0), name


def test_no_config_switch_can_grant_telemetry_blocking_authority():
    """There is deliberately no boolean knob here: granting authority is a
    reviewed code change, never a flag flip."""
    for name in dir(config):
        if name.startswith("GATE_TELEMETRY"):
            assert not isinstance(getattr(config, name), bool), name


# ===========================================================================
# 12. The endpoint.
# ===========================================================================
def test_endpoint_returns_the_rollup(store):
    import app as app_module
    gt.record_scan([_row("AAA", _gate(rs3m_vs_spy=-1.0))], scan_id="r",
                   day="2026-08-10")
    res = app_module.app.test_client().get(
        "/api/scan/gate-telemetry?start=2026-08-01&end=2026-08-31"
        "&ruleset=ranker")
    assert res.status_code == 200
    data = res.get_json()
    assert data["evaluated_n"] == 1
    by_id = {g["gate_id"]: g for g in data["gates"]}
    assert by_id["veto:rs3m_vs_spy"]["sole_blocker_rate"] == 1.0
    assert by_id["L5:cash_reserve"]["indeterminate"] is True


def test_endpoint_refuses_to_pool_rulesets(store):
    import app as app_module
    gt.record_scan([_row("AAA", _gate())], scan_id="r1", day="2026-08-10",
                   ruleset="legacy")
    gt.record_scan([_row("BBB", _gate())], scan_id="r2", day="2026-08-11",
                   ruleset="proposed")
    res = app_module.app.test_client().get(
        "/api/scan/gate-telemetry?start=2026-08-01&end=2026-08-31")
    assert res.status_code == 200
    data = res.get_json()
    assert data["gates"] == [] and data["evaluated_n"] == 0
    assert set(data["rulesets_present"]) == {"legacy", "proposed"}


def test_scorecard_response_does_not_ship_the_telemetry_payload(monkeypatch):
    """`gate_results` rides the sweep row for the nightly recorder only. Shipping
    it to every Scan tab mount would add ~1 MB of unread JSON to a request path
    that already has a 60s client abort, so the API boundary drops it."""
    import app as app_module
    import logging_handler as log
    from metrics import scorecard as sc

    rows = [{"ticker": "AAA", "sector": "XLK", "lot_cost": 100.0,
             "verdict": "READY", "gate_results": [{"gate_id": "veto:rs3m_vs_spy"}]}]
    # The full-universe path PEEKS (scorecard_warm) and never calls the sweep.
    monkeypatch.setattr(sc, "scorecard_warm",
                        lambda price_overrides=None: {"as_of": "x", "results": rows})
    monkeypatch.setattr(sc, "split_by_affordability",
                        lambda r, state: (list(r), [], {"active": False}))
    monkeypatch.setattr(log, "load_state", lambda *a, **k: {})
    body = app_module.app.test_client().get("/api/scan/scorecard").get_json()
    assert body["results"][0]["ticker"] == "AAA"
    assert "gate_results" not in body["results"][0]
    # ...and the sweep row itself still carries it for the recorder.
    assert "gate_results" in rows[0]


# ===========================================================================
# 13. Wire codec (schema 2) — the compaction must be lossless.
# ===========================================================================
def _round_trip(per_candidate):
    gates, rows = gt._encode_run(per_candidate)
    run = {"gates": gates, "candidates": rows, "schema_version": 2}
    return list(gt.decoded_candidates(run))


def test_codec_round_trip_is_lossless():
    """Compaction is a STORAGE change, not an information change: decoding must
    reproduce the typed results exactly, field for field."""
    src = [("AAA", True, gt.build_results(_gate(), {})),
           ("BBB", False, gt.build_results(_gate(rs3m_vs_spy=-1.0, below_ma200=True), {}))]
    out = _round_trip(src)
    assert [(s, a) for s, a, _ in out] == [("AAA", True), ("BBB", False)]
    for (_sym, _adm, original), (_s2, _a2, decoded) in zip(src, out):
        assert {r["gate_id"] for r in decoded} == {r["gate_id"] for r in original}
        by_orig = {r["gate_id"]: r for r in original}
        for r in decoded:
            o = by_orig[r["gate_id"]]
            for k in ("level", "authority", "label", "passed", "value",
                      "threshold", "direction"):
                assert r[k] == o[k], f"{r['gate_id']}.{k}: {r[k]!r} != {o[k]!r}"


def test_constant_thresholds_are_hoisted_into_the_run_manifest():
    """The whole saving: a threshold that is constant across a run is written ONCE
    into the manifest instead of on every candidate row.

    Under the veto set every threshold is constant — the vetoes are booleans and
    one zero-line comparison, none of them a per-name bar. The varying-threshold
    path is still exercised by `test_a_varying_threshold_stays_on_the_cell` below,
    which drives it directly rather than through a gate that no longer produces
    one."""
    gates, rows = gt._encode_run([
        ("AAA", True, gt.build_results(_gate(), {})),
        ("BBB", True, gt.build_results(_gate(), {})),
    ])
    m = {g["gate_id"]: g for g in gates}
    assert m["veto:rs3m_vs_spy"]["threshold"] == 0.0        # the kill-switch line
    assert m["veto:rs3m_vs_spy"]["threshold_varies"] is False
    # Hoisted -> every cell is 2-element (passed, value), not 3.
    i = [g["gate_id"] for g in gates].index("veto:rs3m_vs_spy")
    assert all(len(r["r"][i]) == 2 for r in rows)


def test_a_varying_threshold_stays_on_the_cell():
    """A gate whose bar differs per candidate cannot be hoisted; the manifest
    carries none and each cell carries its own."""
    def _res(threshold):
        return [gt._result("veto:custom", 1, gt.VETO, False, value=1.0,
                           threshold=threshold, direction=gt.HIGHER, label="x")]
    gates, rows = gt._encode_run([("AAA", False, _res(90.0)),
                                  ("BBB", False, _res(150.0))])
    m = {g["gate_id"]: g for g in gates}
    assert m["veto:custom"]["threshold"] is None
    assert m["veto:custom"]["threshold_varies"] is True
    i = [g["gate_id"] for g in gates].index("veto:custom")
    assert rows[0]["r"][i][2] == 90.0 and rows[1]["r"][i][2] == 150.0
    decoded = dict((sym, {r["gate_id"]: r for r in res})
                   for sym, _a, res in gt.decoded_candidates(
                       {"gates": gates, "candidates": rows}))
    assert decoded["AAA"]["veto:custom"]["threshold"] == 90.0
    assert decoded["BBB"]["veto:custom"]["threshold"] == 150.0

def test_heterogeneous_gate_sets_align_via_null_cells():
    """A run whose candidates carry different gate sets must still align: the
    manifest is the union, and an absent gate is a null cell — distinct from a
    present gate that produced no verdict."""
    with_floor = gt.build_results(_gate(), {"shadow_floor": {
        "pass": False, "measured_pct": 0.1, "floor_pct": 0.75, "basis": "juice"}})
    without = gt.build_results(_gate(), {})
    assert len(with_floor) == len(without) + 1
    gates, rows = gt._encode_run([("AAA", True, with_floor), ("BBB", True, without)])
    i = [g["gate_id"] for g in gates].index("shadow:income_floor")
    assert rows[0]["r"][i] is not None
    assert rows[1]["r"][i] is None                 # ABSENT, not "no verdict"
    out = {s: {r["gate_id"] for r in res} for s, _a, res in gt.decoded_candidates(
        {"gates": gates, "candidates": rows})}
    assert "shadow:income_floor" in out["AAA"]
    assert "shadow:income_floor" not in out["BBB"]


def test_absent_gate_is_distinct_from_a_gate_with_no_verdict():
    """`null` cell = the gate was not emitted for this candidate. `[null, value]`
    = it was emitted and produced no verdict. Conflating them would let an
    unevaluated veto silently become an absent one, and sole-blocker attribution
    depends on the difference."""
    results = gt.build_results(_gate(), {})
    results.append({"gate_id": "Z:no_verdict", "level": 4, "authority": gt.VETO,
                    "label": "z", "passed": None, "value": 1.0,
                    "threshold": 2.0, "direction": gt.LOWER})
    gates, rows = gt._encode_run([("AAA", True, results), ("BBB", True,
                                                           gt.build_results(_gate(), {}))])
    i = [g["gate_id"] for g in gates].index("Z:no_verdict")
    assert rows[0]["r"][i] == [None, 1.0]          # present, no verdict
    assert rows[1]["r"][i] is None                 # absent
    dec = dict((s, {r["gate_id"]: r for r in res})
               for s, _a, res in gt.decoded_candidates(
                   {"gates": gates, "candidates": rows}))
    assert dec["AAA"]["Z:no_verdict"]["passed"] is None
    assert "Z:no_verdict" not in dec["BBB"]


def test_schema_1_runs_are_still_read_transparently(store):
    """Runs written before the compaction carry typed dicts inline and no
    manifest. They must keep aggregating, not error — a store spanning the change
    reads as one series."""
    results = gt.build_results(_gate(rs3m_vs_spy=-1.0), {})
    legacy_run = {
        "scan_run_id": "old", "evaluated_at": "2026-08-10T02:00:00Z",
        "gate_ruleset": gt.RULESET_MARKER, "schema_version": 1,
        "unevaluated_gates": gt.unevaluated_gates(),
        "candidates": [{"symbol": "AAA", "overall_admitted": False,
                        "results": results}],
    }
    os.makedirs(str(store), exist_ok=True)
    with open(os.path.join(str(store), "2026-08-10.json"), "w") as fh:
        json.dump({"date": "2026-08-10", "schema": 1, "runs": [legacy_run]}, fh)
    # ...and a schema-2 run alongside it.
    gt.record_scan([_row("BBB", _gate(rs3m_vs_spy=-1.0))], scan_id="new",
                   day="2026-08-11")

    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    assert agg["evaluated_n"] == 2
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["veto:rs3m_vs_spy"]["failed_n"] == 2
    assert g["veto:rs3m_vs_spy"]["sole_blocker_rate"] == 1.0


def test_events_yields_the_typed_event_shape(store):
    """The typed event is the contract; the positional packing is an
    implementation detail of the store."""
    gt.record_scan([_row("AAA", _gate(rs3m_vs_spy=-1.0))], scan_id="run-1",
                   day="2026-08-10")
    evs = list(gt.events(start="2026-08-01", end="2026-08-31"))
    assert len(evs) == 1
    e = evs[0]
    assert set(e) == {"scan_run_id", "evaluated_at", "symbol", "gate_ruleset",
                      "results", "overall_admitted", "schema_version"}
    assert e["symbol"] == "AAA" and e["gate_ruleset"] == gt.RULESET_MARKER
    assert e["overall_admitted"] is False
    assert e["schema_version"] == gt.SCHEMA_VERSION
    r = {x["gate_id"]: x for x in e["results"]}["veto:rs3m_vs_spy"]
    assert r["authority"] == gt.VETO and r["passed"] is False
    assert r["threshold"] == 0.0        # the kill-switch line


def test_compaction_materially_shrinks_the_day_file(store):
    """The point of schema 2. Encoded against the same results, the compact form
    must be well under half the inline-typed form."""
    per = [(f"S{i:03d}", False, gt.build_results(_gate(rs3m_vs_spy=-1.0), {}))
           for i in range(200)]
    gates, rows = gt._encode_run(per)
    compact = len(json.dumps({"gates": gates, "candidates": rows}))
    inline = len(json.dumps({"candidates": [
        {"symbol": s, "overall_admitted": a, "results": r} for s, a, r in per]}))
    assert compact < inline * 0.5, f"compact={compact} inline={inline}"


# ===========================================================================
# 14. The read paths must never block on the sweep.
#
# /api/scan/ready and /api/scan/scorecard used to call scorecard(), which on a
# cold memo IS the full-universe sweep and holds the `scorecard:full` lock for
# its duration. The background scan holds that same lock, so a Scan-tab mount
# during a sweep waited the whole sweep out and the client aborted at 60s. These
# pin the fix: the read paths peek, and a not-warm peek is reported as PENDING —
# never as an empty result set.
# ===========================================================================
def test_full_universe_reads_never_call_the_sweep(monkeypatch):
    """The regression. If either endpoint calls scorecard() for the full
    universe again, this fails — that call is what blocked."""
    import app as app_module
    import logging_handler as log
    from metrics import scorecard as sc

    def _boom(*a, **k):
        raise AssertionError("read path called the blocking sweep")

    monkeypatch.setattr(sc, "scorecard", _boom)
    monkeypatch.setattr(sc, "scorecard_warm", lambda price_overrides=None: None)
    monkeypatch.setattr(log, "load_state", lambda *a, **k: {})
    client = app_module.app.test_client()
    for path in ("/api/scan/scorecard", "/api/scan/ready"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert res.get_json()["scan_pending"] is True, path


def test_pending_is_never_rendered_as_an_empty_result(monkeypatch):
    """"The sweep has not finished" and "the gate admitted nobody" are different
    facts. The payload must carry an explicit marker, not just empty lists."""
    import app as app_module
    import logging_handler as log
    from metrics import scorecard as sc

    monkeypatch.setattr(sc, "scorecard_warm", lambda price_overrides=None: None)
    monkeypatch.setattr(log, "load_state", lambda *a, **k: {})
    body = app_module.app.test_client().get("/api/scan/ready").get_json()
    assert body["scan_pending"] is True
    assert body["eligible"] == [] and body["blocked"] == []
    assert "running" in body            # so the client can say WHY it is pending


def test_an_explicit_ticker_subset_still_computes_fresh(monkeypatch):
    """Only the full-universe path peeks. A named subset is cheap and must keep
    computing, or per-ticker inspection would silently go stale."""
    import app as app_module
    import logging_handler as log
    from metrics import scorecard as sc

    seen = {}

    def _fake_scorecard(t=None, **k):
        seen["tickers"] = t
        return {"as_of": "x", "results": [{"ticker": "AAA", "sector": "XLK"}]}

    monkeypatch.setattr(sc, "scorecard", _fake_scorecard)
    monkeypatch.setattr(sc, "scorecard_warm",
                        lambda price_overrides=None: (_ for _ in ()).throw(
                            AssertionError("subset must not peek")))
    monkeypatch.setattr(sc, "split_by_affordability",
                        lambda r, state: (list(r), [], {"active": False}))
    monkeypatch.setattr(log, "load_state", lambda *a, **k: {})
    body = app_module.app.test_client().get("/api/scan/scorecard?tickers=AAA").get_json()
    assert body["results"][0]["ticker"] == "AAA"
    assert seen["tickers"] == ["AAA"]


def test_scorecard_warm_prefers_the_memo_then_the_disk_cache(monkeypatch):
    """Memo first (hot process), then the day's disk cache (survives a restart —
    which the memo does not, and a restart is exactly when this matters)."""
    import screening
    from metrics import scorecard as sc

    monkeypatch.setattr(screening, "peek_cached", lambda k, max_age=None: {"src": "memo"})
    assert sc.scorecard_warm()["src"] == "memo"

    monkeypatch.setattr(screening, "peek_cached", lambda k, max_age=None: None)
    monkeypatch.setattr(sc, "_current_regime_color", lambda: "green")
    import scan_cache
    monkeypatch.setattr(scan_cache, "reusable",
                        lambda names, color, now=None: {
                            "complete": True, "result": {"src": "disk", "results": [1]}})
    assert sc.scorecard_warm()["src"] == "disk"


def test_scorecard_warm_refuses_a_partial_disk_hit(monkeypatch):
    """A partial universe silently rendered as the whole one is the quiet kind of
    wrong this dashboard must never show. Incomplete -> not warm."""
    import scan_cache
    import screening
    from metrics import scorecard as sc

    monkeypatch.setattr(screening, "peek_cached", lambda k, max_age=None: None)
    monkeypatch.setattr(sc, "_current_regime_color", lambda: "green")
    monkeypatch.setattr(scan_cache, "reusable",
                        lambda names, color, now=None: {
                            "complete": False, "result": {"results": [1]}})
    assert sc.scorecard_warm() is None


def test_scorecard_warm_never_raises_into_a_read_path(monkeypatch):
    import screening
    from metrics import scorecard as sc

    monkeypatch.setattr(screening, "peek_cached", lambda k, max_age=None: None)
    monkeypatch.setattr(sc, "_current_regime_color",
                        lambda: (_ for _ in ()).throw(RuntimeError("no VIX")))
    assert sc.scorecard_warm() is None


def test_a_price_override_request_is_never_served_from_a_peek():
    """Overrides exist to get CURRENT numbers; serving a cached sweep for one
    would answer a different question than the caller asked."""
    from metrics import scorecard as sc
    assert sc.scorecard_warm({"AAA": 1.0}) is None
