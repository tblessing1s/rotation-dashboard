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
def _gate(*, regime="green", rs1m=1.0, breadth=60.0, inst_flow="ACCUMULATING",
          greens=4, core_green=True, insufficient=False,
          ivr=10.0, atr_expanding=False, close=100.0, ma200=90.0,
          entrability="READY", atr_pct=2.0, atr_5d=0.9, extension=0.5,
          ruleset="legacy"):
    """A synthetic entry-gate dict shaped exactly like screening.entry_gate's,
    carrying only the fields build_results reads. Defaults are an all-pass
    candidate; each test perturbs one axis."""
    reasons = []
    if rs1m is not None and rs1m < 0:
        reasons.append("rs1m_negative")
    if breadth is not None and breadth < config.SECTOR_BREADTH_COLLAPSE:
        reasons.append("breadth_collapsing")
    if inst_flow == "DISTRIBUTING":
        reasons.append("under_distribution")
    veto1 = bool(atr_expanding and ivr is not None
                 and ivr >= config.VETO_IVR_PERCENTILE_MIN)
    veto2 = bool(close is not None and ma200 is not None and close < ma200)
    spot = {"pass": atr_pct <= config.CONSOLIDATION_ATR_PCT_MAX
                    and atr_5d <= config.SPOT_ATR_MOMENTUM_MAX
                    and extension <= config.SPOT_ATR_EXTENSION_MAX,
            "checks": [
                {"id": "atr_pct", "value": atr_pct,
                 "pass": atr_pct <= config.CONSOLIDATION_ATR_PCT_MAX},
                {"id": "atr_5d_ema", "value": atr_5d,
                 "pass": atr_5d <= config.SPOT_ATR_MOMENTUM_MAX},
                {"id": "extension", "value": extension,
                 "pass": extension <= config.SPOT_ATR_EXTENSION_MAX},
            ]}
    return {
        "ticker": "TEST", "ruleset": ruleset,
        "levels": [
            {"level": 1, "name": "Market regime green", "pass": regime == "green",
             "checks": [], "detail": {"published_regime": regime}},
            {"level": 2, "name": "Sector not deteriorating", "pass": not reasons,
             "checks": [], "detail": {"rs1m": rs1m, "breadth": breadth,
                                      "inst_flow": inst_flow,
                                      "deteriorating_reasons": reasons}},
            {"level": 3, "name": "Stock lights green",
             "pass": greens >= 4 and not veto1 and not veto2 and not insufficient,
             "checks": [], "detail": {
                 "greens": greens, "core_green": core_green,
                 "insufficient": insufficient,
                 "vetoes": [
                     {"id": "atr_expanding_high_ivr", "tripped": veto1,
                      "value": {"atr_expanding": atr_expanding,
                                "ivr_percentile": ivr,
                                "ivr_min": config.VETO_IVR_PERCENTILE_MIN}},
                     {"id": "close_below_ma200", "tripped": veto2,
                      "value": {"close": close, "ma200": ma200}},
                 ]}},
            {"level": 3.5, "name": "Structure entrable",
             "pass": entrability in ("READY", "CAUTION"),
             "checks": [], "detail": {"entrability": entrability}},
            {"level": 4, "name": "Right spot (not extended)", "pass": spot["pass"],
             "checks": [], "detail": {"right_spot": spot,
                                      "right_spot_by_ruleset": {"legacy": spot}}},
        ],
    }


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

    gate = _gate(greens=before["greens"],
                 core_green=bool(before.get("core_green")),
                 insufficient=bool(before["insufficient"]),
                 ivr=95.0, atr_expanding=True)
    results = gt.build_results(gate, {})

    after = stock_lights.compute(df, ivr_percentile=95.0, is_etf=True)
    assert json.dumps(after, sort_keys=True, default=str) == snapshot
    assert before["verdict"] == stock_lights.RED
    assert "veto:atr_expanding_high_ivr" in before["veto_reasons"]
    # And the telemetry SAW the veto it must not have caused.
    assert _by_id(results)["L3:veto:atr_expanding_high_ivr"]["passed"] is False


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

    gate = _gate(greens=before["greens"],
                 core_green=bool(before.get("core_green")),
                 insufficient=bool(before["insufficient"]),
                 ivr=20.0, atr_expanding=False)
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
    assert row["verdict"] in ("READY", "CAUTION", "WATCH", "BLOCKED")
    assert row["suitability"] in ("GO", "CAUTION", "AVOID")


# ===========================================================================
# 2. Synthetic multi-candidate scan — exact hand-computed rates.
# ===========================================================================
@pytest.fixture
def hand_scan(store):
    """Five candidates with hand-computed veto outcomes, one scan run.

      AAA — all pass                                   -> admitted
      BBB — ONLY extension fails                       -> sole block: L4:extension
      CCC — ONLY extension fails                       -> sole block: L4:extension
      DDD — regime RED and under_distribution          -> co-block pair
      EEE — ONLY regime RED                            -> sole block: L1:regime_green

    Veto gates evaluated per candidate: L1:regime_green, L2:rs1m_negative,
    L2:breadth_collapsing, L2:under_distribution, L3:sym_vote,
    L3:veto:atr_expanding_high_ivr, L3:veto:close_below_ma200,
    L3.5:structure_entrable, L4:atr_pct, L4:atr_5d_ema, L4:extension = 11.
    """
    rows = [
        _row("AAA", _gate()),
        _row("BBB", _gate(extension=2.0)),
        _row("CCC", _gate(extension=3.0)),
        _row("DDD", _gate(regime="red", inst_flow="DISTRIBUTING")),
        _row("EEE", _gate(regime="red")),
    ]
    gt.record_scan(rows, scan_id="run-1", day="2026-08-10", ruleset="legacy")
    return rows


def test_evaluated_n_and_admitted(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    assert agg["evaluated_n"] == 5
    assert agg["admitted_n"] == 1
    assert agg["runs"] == 1 and agg["days"] == 1


def test_exact_block_and_sole_blocker_rates(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}

    ext = g["L4:extension"]
    assert ext["evaluated_n"] == 5
    assert ext["failed_n"] == 2                       # BBB, CCC
    assert ext["block_rate"] == 0.4                   # 2/5
    assert ext["sole_blocker_n"] == 2
    assert ext["sole_blocker_rate"] == 0.4

    reg = g["L1:regime_green"]
    assert reg["failed_n"] == 2                       # DDD, EEE
    assert reg["block_rate"] == 0.4
    assert reg["sole_blocker_n"] == 1                 # EEE only (DDD co-fires)
    assert reg["sole_blocker_rate"] == 0.2

    dist = g["L2:under_distribution"]
    assert dist["failed_n"] == 1                      # DDD
    assert dist["block_rate"] == 0.2
    assert dist["sole_blocker_n"] == 0                # never alone
    assert dist["sole_blocker_rate"] == 0.0

    clean = g["L2:rs1m_negative"]
    assert clean["failed_n"] == 0 and clean["block_rate"] == 0.0
    assert clean["sole_blocker_rate"] == 0.0


def test_sorted_by_sole_blocker_rate_descending(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    rated = [row for row in agg["gates"] if not row["indeterminate"]]
    rates = [row["sole_blocker_rate"] for row in rated]
    assert rates == sorted(rates, reverse=True)
    assert rated[0]["gate_id"] == "L4:extension"
    # Indeterminate rows sort LAST — never floated to the top on a null.
    assert all(row["indeterminate"] for row in agg["gates"][len(rated):])


def test_exact_co_block_matrix(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    m = agg["co_block_matrix"]
    # DDD is the only candidate with two veto failures.
    assert m["L1:regime_green"]["L2:under_distribution"] == 1
    assert m["L2:under_distribution"]["L1:regime_green"] == 1
    # Symmetric, and zero everywhere else.
    assert m["L4:extension"]["L1:regime_green"] == 0
    assert m["L1:regime_green"]["L4:extension"] == 0
    for a, rowm in m.items():
        for b, n in rowm.items():
            assert n == m[b][a], f"{a}/{b} asymmetric"


def test_near_miss_distribution_is_over_failures_only(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}
    nm = g["L4:extension"]["near_miss"]
    # threshold 1.5; failures at 2.0 and 3.0 -> overshoot 0.3333 and 1.0
    assert nm["n"] == 2 and nm["normalized"] is True
    assert round(nm["median"], 4) == 0.3333
    assert round(nm["p75"], 4) == 1.0
    assert sum(nm["buckets"].values()) == 2
    # Passing candidates contribute nothing.
    assert g["L4:atr_pct"]["near_miss"]["n"] == 0


def test_weekly_time_series(hand_scan):
    rows = [_row("FFF", _gate(extension=9.0))]
    gt.record_scan(rows, scan_id="run-2", day="2026-08-17", ruleset="legacy")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    series = agg["time_series"]["L4:extension"]
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
    gt.record_scan(rows, scan_id="r", day="2026-08-10", ruleset="legacy")
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
    rows = [_row("BBB", _gate(extension=2.0),
                 floor={"pass": False, "measured_pct": 0.1, "floor_pct": 0.75,
                        "basis": "juice"})]
    gt.record_scan(rows, scan_id="r", day="2026-08-10", ruleset="legacy")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {row["gate_id"]: row for row in agg["gates"]}
    assert g["L4:extension"]["sole_blocker_rate"] == 1.0


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
    row = _row("AAA", _gate(extension=2.0))
    # Simulate a gate that ran but produced no verdict.
    row["gate_results"].append({"gate_id": "L9:unknown", "level": 9,
                                "authority": gt.VETO, "label": "unknown",
                                "passed": None, "value": None,
                                "threshold": None, "direction": None})
    gt.record_scan([row], scan_id="r", day="2026-08-10", ruleset="legacy")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["L4:extension"]["failed_n"] == 1
    assert g["L4:extension"]["sole_blocker_n"] == 0     # not imputed
    # The unknown gate is counted as neither pass nor fail.
    assert g["L9:unknown"]["evaluated_n"] == 0


# ===========================================================================
# 5. Ruleset segmentation — never pool.
# ===========================================================================
def test_never_pools_across_rulesets(store):
    gt.record_scan([_row("AAA", _gate(extension=2.0))],
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
    assert {g["gate_id"]: g for g in legacy["gates"]}["L4:extension"]["failed_n"] == 1

    proposed = gt.aggregate(start="2026-08-01", end="2026-08-31",
                            gate_ruleset="proposed")
    assert proposed["evaluated_n"] == 1
    assert {g["gate_id"]: g for g in proposed["gates"]}["L4:extension"]["failed_n"] == 0


def test_ruleset_selects_the_level4_replay():
    """The proposed ruleset relaxes only the ATR-momentum ceiling; build_results
    must read that ruleset's own right-spot, not the authoritative one."""
    import stock_lights
    assert stock_lights.atr_momentum_max("proposed") > stock_lights.atr_momentum_max("legacy")
    gate = _gate(atr_5d=1.02)                      # fails legacy (1.0), passes proposed (1.05)
    legacy = _by_id(gt.build_results(gate, {}, ruleset="legacy"))
    assert legacy["L4:atr_5d_ema"]["passed"] is False
    assert legacy["L4:atr_5d_ema"]["threshold"] == config.SPOT_ATR_MOMENTUM_MAX
    prop = _by_id(gt.build_results(gate, {}, ruleset="proposed"))
    assert prop["L4:atr_5d_ema"]["threshold"] == config.L4_ATR_EXPANSION_MAX


# ===========================================================================
# 6. Absence of history is reported, never fabricated.
# ===========================================================================
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
    gt.record_scan([_row("BBB", _gate(extension=9.0))], scan_id="run-2",
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
    gt.record_scan(rows, scan_id="r", day="2026-08-10", ruleset="legacy")
    # Flip the world: pretend the floor graduated to veto authority today.
    stored = json.loads((store / "2026-08-10.json").read_text())
    rec = [r for r in stored["runs"][0]["candidates"][0]["results"]
           if r["gate_id"] == "shadow:income_floor"][0]
    assert rec["authority"] == gt.SHADOW
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
    gt.record_scan([row_a], scan_id="r1", day="2026-08-10", ruleset="legacy")
    gt.record_scan([row_b], scan_id="r2", day="2026-08-11", ruleset="legacy")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["X:gate"].get("authority_changed_in_range") is True


# ===========================================================================
# 9. Near-miss edge cases.
# ===========================================================================
def test_zero_threshold_gate_reports_raw_distance_not_a_fabricated_ratio(store):
    """`L2:rs1m_negative` has threshold 0.0 — a fractional distance from zero is
    undefined, so the raw distance is reported and `normalized` is False."""
    gt.record_scan([_row("AAA", _gate(rs1m=-0.8))], scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    nm = {r["gate_id"]: r for r in agg["gates"]}["L2:rs1m_negative"]["near_miss"]
    assert nm["n"] == 0 and nm["normalized"] is False and nm["median"] is None
    assert nm["raw_n"] == 1 and round(nm["raw_median"], 4) == 0.8


def test_non_numeric_gate_has_no_near_miss(store):
    gt.record_scan([_row("AAA", _gate(entrability="TOPPING"))],
                   scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    g = {r["gate_id"]: r for r in agg["gates"]}["L3.5:structure_entrable"]
    assert g["failed_n"] == 1
    assert g["near_miss"]["n"] == 0 and g["near_miss"]["raw_n"] == 0


def test_higher_is_better_gate_near_miss_is_positive_on_failure(store):
    """close_below_ma200 fails when close < ma200; the overshoot must be a
    positive "how far past the line" number, comparable with a lower-is-better
    gate's."""
    gt.record_scan([_row("AAA", _gate(close=90.0, ma200=100.0))],
                   scan_id="r", day="2026-08-10")
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31")
    nm = {r["gate_id"]: r for r in agg["gates"]}["L3:veto:close_below_ma200"]["near_miss"]
    assert nm["n"] == 1 and round(nm["median"], 4) == 0.1     # 10/100


# ===========================================================================
# 10. Filters and the denominator.
# ===========================================================================
def test_symbol_universe_filter_narrows_the_denominator(hand_scan):
    agg = gt.aggregate(start="2026-08-01", end="2026-08-31", symbols=["BBB", "CCC"])
    assert agg["evaluated_n"] == 2
    g = {r["gate_id"]: r for r in agg["gates"]}
    assert g["L4:extension"]["block_rate"] == 1.0


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
    gt.record_scan([_row("AAA", _gate(extension=2.0))], scan_id="r",
                   day="2026-08-10", ruleset="legacy")
    res = app_module.app.test_client().get(
        "/api/scan/gate-telemetry?start=2026-08-01&end=2026-08-31&ruleset=legacy")
    assert res.status_code == 200
    data = res.get_json()
    assert data["evaluated_n"] == 1
    by_id = {g["gate_id"]: g for g in data["gates"]}
    assert by_id["L4:extension"]["sole_blocker_rate"] == 1.0
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
             "verdict": "READY", "gate_results": [{"gate_id": "L4:extension"}]}]
    monkeypatch.setattr(sc, "scorecard",
                        lambda t=None, price_overrides=None, force=False:
                        {"as_of": "x", "results": rows})
    monkeypatch.setattr(sc, "split_by_affordability",
                        lambda r, state: (list(r), [], {"active": False}))
    monkeypatch.setattr(log, "load_state", lambda *a, **k: {})
    body = app_module.app.test_client().get("/api/scan/scorecard").get_json()
    assert body["results"][0]["ticker"] == "AAA"
    assert "gate_results" not in body["results"][0]
    # ...and the sweep row itself still carries it for the recorder.
    assert "gate_results" in rows[0]
