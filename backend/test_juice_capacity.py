"""Trailing juice CAPACITY (SHADOW) — emission, median, guard, and the
no-authority invariant.

Offline and fixture-driven: no provider is ever reached (the juice computation
under test is itself pure over cached bars), and every store write lands in a
temp DATA_DIR.

The load-bearing test in this file is `test_no_authority_*`: capacity must not
gate, hide, rank, bench or reorder anything, and a fixture whose capacity sits far
below the floor must produce a scan row byte-identical to one computed with the
feature disabled.
"""
from __future__ import annotations

import copy
import json
import inspect
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-capacity-test-"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import config  # noqa: E402
import juice_capacity as jc  # noqa: E402

FIX_STRUCT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fixtures", "structure")
FIX_REGIME = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fixtures", "regime")


@pytest.fixture
def shares_mode(monkeypatch):
    """Pin SHARES-PRIMARY for any test that exercises the juice computation.

    `conftest._legacy_leap_open_in_tests` is autouse and relaxes
    `config.LEGACY_LEAP_READONLY` so the suite can seed legacy LEAP history — which
    also flips `account_gate.juice_estimate` into its LEAP-denominated arm
    (extrinsic / LEAP cost, several-fold above the covered-call yield). Capacity is
    SHARE-denominated by construction: it is measured against the share-notional
    shadow floor and displayed beside the scan's share-notional GROSS/WK. So these
    tests restore the production value explicitly, exactly as test_shares_migration
    does when asserting the guard."""
    monkeypatch.setattr(config, "LEGACY_LEAP_READONLY", True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated capacity store. The parsed-file memo is keyed on (mtime, size),
    so it is reset alongside the path — otherwise a same-size store written twice
    in the same second could serve a stale parse."""
    monkeypatch.setattr(jc, "CAPACITY_PATH", str(tmp_path / "juice_capacity.json"))
    monkeypatch.setattr(jc, "_parsed", None, raising=False)
    yield tmp_path
    monkeypatch.setattr(jc, "_parsed", None, raising=False)


def _row(ticker, juice, *, dividend=None, combined=None, **extra):
    """A minimal scorecard-shaped row (the fields the observation reads)."""
    known = dividend is not None
    if combined is None and juice is not None:
        combined = round(juice + (dividend or 0.0), 4)
    return {"ticker": ticker, "juice_weekly_pct": juice,
            "dividend_weekly_pct": dividend, "dividend_known": known,
            "combined_weekly_yield_pct": combined,
            "short_strike": 100.0, "price": 105.0, "regime_color": "green",
            **extra}


def _seed(ticker, values, *, start_day=1, source=jc.SOURCE_LIVE):
    """Persist a synthetic series: values[i] recorded on a distinct date."""
    for i, v in enumerate(values):
        day = f"2026-01-{start_day + i:02d}" if start_day + i <= 31 else None
        if day is None:  # roll into a second month for long series
            n = start_day + i - 31
            day = f"2026-02-{n:02d}" if n <= 28 else f"2026-03-{n - 28:02d}"
        jc.record_observations([_row(ticker, v, combined=v)], day=day, source=source)


def _dates(n, *, start=0):
    """`n` consecutive ISO dates, so a series can exceed one month cleanly."""
    base = pd.Timestamp("2025-01-01")
    return [(base + pd.Timedelta(days=start + i)).strftime("%Y-%m-%d") for i in range(n)]


def _seed_dates(ticker, values, dates, *, source=jc.SOURCE_LIVE):
    for v, d in zip(values, dates):
        jc.record_observations([_row(ticker, v, combined=v)], day=d, source=source)


# ===========================================================================
# 1. Observation emission — shape + per-day idempotence
# ===========================================================================
def test_emission_shape_over_a_fixture_universe(store):
    rows = [_row("ET", 0.14, dividend=0.17), _row("XLE", 0.34, dividend=0.06),
            _row("NVDA", 1.42)]
    out = jc.record_observations(rows, day="2026-08-24")
    assert out["ok"] and out["recorded"] == 3 and out["skipped"] == 0

    obs = jc.series("ET")[-1]
    # Exactly the fields the spec names, plus provenance.
    assert obs["date"] == "2026-08-24"
    assert obs["achievable_juice_wk_pct"] == 0.14
    assert obs["dividend_wk_pct"] == 0.17
    assert obs["combined_wk_pct"] == 0.31
    assert obs["strike_used"] == 100.0
    assert obs["regime"] == "green"
    assert obs["source"] == jc.SOURCE_LIVE
    assert obs["schema"] == jc.SCHEMA_VERSION
    assert obs["atr_mult"] == config.CAPACITY_STRIKE_ATR_MULT


def test_unpriceable_row_is_skipped_not_recorded_as_zero(store):
    """An unmeasurable name is UNMEASURED, never a recorded zero — the same rule
    shadow_floor applies to its `pass`. A zero here would drag the median down and
    make a name we simply failed to price look structurally dead."""
    out = jc.record_observations([_row("DEAD", None), _row("OK", 0.9)], day="2026-08-24")
    assert out["recorded"] == 1 and out["skipped"] == 1
    assert jc.series("DEAD") == []


def test_rerunning_the_same_day_is_idempotent(store):
    """The store's convention is one observation per symbol per CALENDAR DAY, last
    write wins (iv_history's rule) — NOT scan_rejection_log's append-per-run. A
    busy session sweeps the universe dozens of times; appending each would let one
    heavily-rescanned day outvote twenty quiet ones in the median."""
    for _ in range(5):
        jc.record_observations([_row("ET", 0.14, dividend=0.17)], day="2026-08-24")
    assert len(jc.series("ET")) == 1

    # A later same-day write with new values REPLACES rather than appends.
    jc.record_observations([_row("ET", 0.19, dividend=0.17)], day="2026-08-24")
    series = jc.series("ET")
    assert len(series) == 1 and series[0]["achievable_juice_wk_pct"] == 0.19

    # A different day appends.
    jc.record_observations([_row("ET", 0.20, dividend=0.17)], day="2026-08-25")
    assert len(jc.series("ET")) == 2


def test_backfill_never_overwrites_a_live_observation(store):
    """Provenance beats recency: a live point came from a scan the operator
    actually ran, a replayed one is a reconstruction, and letting the
    reconstruction win would quietly rewrite real history."""
    jc.record_observations([_row("ET", 0.14, combined=0.14)], day="2026-08-24")
    jc.record_observations([_row("ET", 9.99, combined=9.99)], day="2026-08-24",
                           source=jc.SOURCE_BACKFILL_BAR_REPLAY)
    obs = jc.series("ET")
    assert len(obs) == 1
    assert obs[0]["combined_wk_pct"] == 0.14 and obs[0]["source"] == jc.SOURCE_LIVE

    # ...but a live point DOES replace a backfilled one for the same day.
    jc.record_observations([_row("XLE", 5.0, combined=5.0)], day="2026-08-24",
                           source=jc.SOURCE_BACKFILL_BAR_REPLAY)
    jc.record_observations([_row("XLE", 0.34, combined=0.34)], day="2026-08-24")
    assert jc.series("XLE")[0]["source"] == jc.SOURCE_LIVE


def test_a_telemetry_failure_never_raises_into_the_caller(store, monkeypatch):
    def _boom(*_a, **_kw):
        raise OSError("disk full")
    monkeypatch.setattr(jc, "_save", _boom)
    out = jc.record_observations([_row("ET", 0.14)], day="2026-08-24")
    assert out["ok"] is False and "disk full" in out["error"]


# ===========================================================================
# 2. Median correctness + the window boundary
# ===========================================================================
def test_median_of_a_known_series(store):
    _seed_dates("SYN", [0.10, 0.20, 0.30, 0.40, 0.50] * 5, _dates(25))
    # 25 points: median of five repeats of 0.1..0.5 is 0.30.
    assert jc.juice_capacity_wk_pct("SYN") == 0.30


def test_median_not_mean_is_what_makes_this_a_capacity_read(store):
    """The discrimination the whole feature rests on. A mean would be dragged down
    by a compression that a median shrugs off."""
    values = [0.85] * 40 + [0.05] * 20      # a deep, recent compression
    _seed_dates("SYN", values, _dates(len(values)))
    assert jc.juice_capacity_wk_pct("SYN") == 0.85
    assert sum(values) / len(values) < 0.65  # the mean would have moved a lot


def test_window_boundary_excludes_the_observation_past_the_window(store):
    """The window is the newest CAPACITY_WINDOW_DAYS observations (one per date),
    so the 253rd-newest is excluded. Seeded so the out-of-window point would move
    the median if it were counted."""
    window = config.CAPACITY_WINDOW_DAYS
    dates = _dates(window + 1)
    # Oldest point first: a huge outlier that must fall OUT of the window.
    values = [99.0] + [0.50] * window
    _seed_dates("SYN", values, dates)
    assert len(jc.series("SYN")) == window + 1        # all retained (< retention)
    assert jc.juice_capacity_wk_pct("SYN") == 0.50    # but the outlier is excluded

    # And the readout agrees about how many observations it actually used.
    assert jc.capacity("SYN")["obs"] == window


def test_retention_trims_by_distinct_date(store):
    over = config.CAPACITY_RETENTION_DAYS + 15
    _seed_dates("SYN", [0.5] * over, _dates(over))
    assert len(jc.series("SYN")) == config.CAPACITY_RETENTION_DAYS


# ===========================================================================
# 3. The minimum-observations guard
# ===========================================================================
def test_guard_19_observations_is_insufficient_history(store):
    _seed_dates("SYN", [0.5] * (config.CAPACITY_MIN_OBS - 1),
                _dates(config.CAPACITY_MIN_OBS - 1))
    assert jc.juice_capacity_wk_pct("SYN") == jc.INSUFFICIENT_HISTORY
    read = jc.capacity("SYN", floor_pct=0.75)
    assert read["status"] == jc.INSUFFICIENT_HISTORY
    assert read["capacity_wk_pct"] is None
    # Never a verdict against the floor on an unmeasured name.
    assert read["clears_floor"] is None


def test_guard_20_observations_is_numeric(store):
    _seed_dates("SYN", [0.5] * config.CAPACITY_MIN_OBS,
                _dates(config.CAPACITY_MIN_OBS))
    assert jc.juice_capacity_wk_pct("SYN") == 0.5
    read = jc.capacity("SYN", floor_pct=0.75)
    assert read["status"] == jc.OK and read["capacity_wk_pct"] == 0.5
    assert read["clears_floor"] is False


def test_insufficient_history_is_a_sentinel_never_a_number(store):
    """"Not measured yet" and "yields nothing" are opposite facts and must never
    share a representation. Prompt 2 treats the sentinel as UNSUPPRESSIBLE, so it
    must not be coercible to a falsy number."""
    assert jc.juice_capacity_wk_pct("UNKNOWN") == jc.INSUFFICIENT_HISTORY
    assert not isinstance(jc.juice_capacity_wk_pct("UNKNOWN"), (int, float))


# ===========================================================================
# 4. Structural vs transient — the discrimination prompt 2 depends on
# ===========================================================================
def test_structural_vs_transient_shape(store):
    """Two synthetic names, pinned here because the second prompt's suppression
    tiers rest entirely on this separation:

      STRUCTURAL — a flat 0.30%/wk series. It was never above the floor; a
        pullback would not fix it, and advertising a clearable L4 condition in
        front of it is the bug this metric exists to expose.
      TRANSIENT  — a 0.85%/wk name whose last 15 observations sit at 0.40%/wk
        (an IV compression). Capacity still reads 0.85: the premium is there, it
        is simply not there THIS week.
    """
    _seed_dates("STRUCT", [0.30] * 60, _dates(60))
    _seed_dates("TRANS", [0.85] * 45 + [0.40] * 15, _dates(60))

    assert jc.juice_capacity_wk_pct("STRUCT") == 0.30
    assert jc.juice_capacity_wk_pct("TRANS") == 0.85

    # The spot reading alone cannot tell them apart — which is the whole point.
    assert jc.series("STRUCT")[-1]["combined_wk_pct"] == 0.30
    assert jc.series("TRANS")[-1]["combined_wk_pct"] == 0.40

    floor = config.SHARES_JUICE_FLOOR_PCT
    assert jc.capacity("STRUCT", floor_pct=floor)["clears_floor"] is False
    assert jc.capacity("TRANS", floor_pct=floor)["clears_floor"] is True


# ===========================================================================
# 5. Dividend combination
# ===========================================================================
def test_dividend_combines_into_the_measured_figure(store):
    """The live ET shape: 0.14 juice + 0.17 dividend = 0.31 combined."""
    jc.record_observations([_row("ET", 0.14, dividend=0.17)], day="2026-08-24")
    obs = jc.series("ET")[0]
    assert obs["achievable_juice_wk_pct"] == 0.14
    assert obs["dividend_wk_pct"] == 0.17
    assert obs["combined_wk_pct"] == 0.31
    assert obs["dividend_known"] is True
    assert obs["dividend_basis"] == jc.DIVIDEND_BASIS_QUOTED
    assert "markers" not in obs


def test_stubbed_dividend_leaves_combined_equal_to_juice_with_a_marker(store):
    """An unresolved yield contributes 0.0 — but never as a confident zero: the
    DIVIDEND_STUBBED marker keeps a fundamentals outage distinguishable from a
    genuine non-payer forever."""
    jc.record_observations([_row("NVDA", 1.42)], day="2026-08-24")
    obs = jc.series("NVDA")[0]
    assert obs["combined_wk_pct"] == obs["achievable_juice_wk_pct"] == 1.42
    assert obs["dividend_wk_pct"] == 0.0
    assert obs["dividend_known"] is False
    assert obs["markers"] == [jc.DIVIDEND_STUBBED]
    assert jc.capacity("NVDA")["dividend_stubbed_obs"] == 1


def test_combined_matches_scan_triggers_combined_weekly_yield():
    """Capacity and the SHADOW floor must measure the same quantity, so they can
    never disagree about what a name yields. Pinned against the shared function
    rather than re-deriving the arithmetic here."""
    import scan_triggers
    parts = scan_triggers.combined_weekly_yield(0.14, 0.17 * config.DIVIDEND_WEEKS_PER_YEAR)
    assert parts["dividend_weekly_pct"] == 0.17
    assert parts["combined_weekly_yield_pct"] == 0.31


# ===========================================================================
# 6. Recompute-from-history — pure, and never mutating the record
# ===========================================================================
def test_capacity_recomputes_from_persisted_observations(store):
    """The median must be a PURE function over the persisted observations, never a
    stored running value. Written as a raw fixture file (not through the writer)
    so the read path is exercised on its own."""
    dates = _dates(25)
    fixture = {"schema": jc.SCHEMA_VERSION, "symbols": {"SYN": [
        {"date": d, "schema": 1, "source": jc.SOURCE_LIVE,
         "achievable_juice_wk_pct": v, "dividend_wk_pct": 0.0,
         "dividend_known": False, "combined_wk_pct": v}
        for d, v in zip(dates, [0.1, 0.2, 0.3, 0.4, 0.5] * 5)]}}
    with open(jc.CAPACITY_PATH, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh)
    jc._parsed = None

    assert jc.juice_capacity_wk_pct("SYN") == 0.30
    # Repeated reads are stable and non-mutating.
    assert jc.juice_capacity_wk_pct("SYN") == 0.30
    with open(jc.CAPACITY_PATH, encoding="utf-8") as fh:
        assert json.load(fh) == fixture


def test_no_running_aggregate_is_persisted(store):
    """A stored median would be a mutable running value — exactly what the
    append-only discipline forbids. Assert the persisted shape carries raw
    observations and nothing else."""
    _seed_dates("SYN", [0.5] * 25, _dates(25))
    with open(jc.CAPACITY_PATH, encoding="utf-8") as fh:
        blob = json.load(fh)
    assert set(blob) == {"schema", "symbols"}
    for obs in blob["symbols"]["SYN"]:
        assert "capacity" not in obs and "median" not in obs


def test_historical_observations_are_never_mutated_by_a_later_write(store):
    dates = _dates(5)
    _seed_dates("SYN", [0.1, 0.2, 0.3, 0.4, 0.5], dates)
    before = copy.deepcopy(jc.series("SYN"))
    jc.record_observations([_row("SYN", 9.9, combined=9.9)], day=_dates(1, start=99)[0])
    after = jc.series("SYN")
    assert after[:5] == before          # every prior point byte-identical
    assert after[5]["combined_wk_pct"] == 9.9


# ===========================================================================
# 7. Backfill — an EXACT replay, marked and distinguishable forever
# ===========================================================================
def test_backfill_replays_the_live_juice_function_exactly(shares_mode, store, monkeypatch):
    """The scan's juice is a pure function of the daily frame (Black-Scholes at
    trailing REALIZED vol — no IV input, no provider, no clock), so replaying it
    on the prefix df.iloc[:i+1] reproduces the number the scan WOULD have shown.
    Not an approximation: the same function on the same inputs."""
    import account_gate
    import data_handler
    import dividends

    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_low_juice.parquet"))
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    monkeypatch.setattr(dividends, "cached_annual_yield_pct", lambda t, state=None: None)

    out = jc.backfill(["LOWVOL"])
    assert out["ok"] and out["symbols"] == 1 and out["observations"] > 200

    obs = jc.series("LOWVOL")
    assert all(o["source"] == jc.SOURCE_BACKFILL_BAR_REPLAY for o in obs)

    # The newest backfilled point equals the live full-frame estimate, exactly.
    live = account_gate.juice_estimate("LOWVOL", df)
    assert obs[-1]["achievable_juice_wk_pct"] == live["weekly_yield_pct"]
    assert obs[-1]["strike_used"] == live["short_strike"]


def test_backfilled_observations_stay_distinguishable_from_live(shares_mode, store, monkeypatch):
    import data_handler
    import dividends
    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_low_juice.parquet"))
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    monkeypatch.setattr(dividends, "cached_annual_yield_pct", lambda t, state=None: 3.12)

    jc.backfill(["LOWVOL"])
    read = jc.capacity("LOWVOL", floor_pct=config.SHARES_JUICE_FLOOR_PCT)
    assert read["sources"] == [jc.SOURCE_BACKFILL_BAR_REPLAY]
    # The dividend anachronism is MARKED, never passed off as a historical read.
    assert all(o["dividend_basis"] == jc.DIVIDEND_BASIS_ANACHRONISTIC
               for o in jc.series("LOWVOL"))

    # A structurally low-vol name: enough history to be measurable, and far below
    # the floor. This is the shape the metric exists to name.
    assert read["status"] == jc.OK
    assert read["clears_floor"] is False


def test_backfill_skips_a_symbol_it_already_covered(shares_mode, store, monkeypatch):
    import data_handler
    import dividends
    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_low_juice.parquet"))
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    monkeypatch.setattr(dividends, "cached_annual_yield_pct", lambda t, state=None: None)
    jc.backfill(["LOWVOL"])
    again = jc.backfill(["LOWVOL"])
    assert again["symbols"] == 0
    assert again["skipped"] == [{"ticker": "LOWVOL", "why": "already backfilled"}]


def test_backfill_is_anchored_at_bar_zero(store, monkeypatch):
    """Wilder ATR is an EWM seeded from the first bar, so the replay is
    prefix-causal only across prefixes sharing bar 0 — a shifted start re-seeds and
    diverges. Pinned because a rolling sub-window would make a past date's capacity
    non-reproducible (the same rule regime_history.backfill states)."""
    import indicators
    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_low_juice.parquet"))
    i = 200
    anchored = indicators.atr(df.iloc[: i + 1])
    shifted = indicators.atr(df.iloc[50: i + 1])
    assert anchored != shifted           # not bit-identical — anchoring matters
    assert abs(anchored - shifted) < 1e-6  # ...though the seed does wash out


# ===========================================================================
# 8. NO AUTHORITY — the load-bearing invariant
# ===========================================================================
_AUTHORITY_KEYS = ("verdict", "verdict_reasons", "binding", "triggers",
                   "path_to_ready", "eligible_days", "bench", "suitability",
                   "suitability_reasons", "score", "verdict_by_ruleset",
                   "legacy_verdict", "proposed_verdict", "ruleset_divergence")


def _score_one(monkeypatch, df, spy, *, capacity_enabled):
    """One scorecard row, computed with the capacity feature on or off. Disabling
    it means the row never consults the store — the counterfactual the spec asks
    for."""
    import data_handler
    import dividends
    import earnings
    import weeklies
    from metrics import scorecard

    monkeypatch.setattr(data_handler, "get_daily",
                        lambda t, force=False: spy if t == config.BENCHMARK else df)
    monkeypatch.setattr(dividends, "cached_annual_yield_pct", lambda t, state=None: None)
    monkeypatch.setattr(earnings, "cached_earnings", lambda t: {})
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t: True)
    if not capacity_enabled:
        monkeypatch.setattr(jc, "capacity",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disabled")))
    return scorecard.score_ticker("LOWVOL", spy, "XLK", None, None,
                                  regime_color="green")


def test_no_authority_row_is_identical_with_and_without_capacity(shares_mode, store, monkeypatch):
    """A fixture whose capacity sits far below the floor must produce a scan row
    byte-identical to one computed with the feature disabled. Capacity moves no
    verdict, no bench status, no ordering, no score."""
    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_low_juice.parquet"))
    spy = pd.read_parquet(os.path.join(FIX_REGIME, "sustained_green.parquet"))

    # A store where LOWVOL's capacity is deep below the floor.
    _seed_dates("LOWVOL", [0.06] * 60, _dates(60))
    assert jc.juice_capacity_wk_pct("LOWVOL") == 0.06
    assert jc.juice_capacity_wk_pct("LOWVOL") < config.SHARES_JUICE_FLOOR_PCT

    with monkeypatch.context() as m:
        with_cap = _score_one(m, df, spy, capacity_enabled=True)
    with monkeypatch.context() as m:
        without = _score_one(m, df, spy, capacity_enabled=False)

    # The capacity readout is present and reports the sub-floor number...
    assert with_cap["juice_capacity"]["capacity_wk_pct"] == 0.06
    assert with_cap["juice_capacity"]["clears_floor"] is False
    assert without["juice_capacity"] is None

    # ...and NOTHING that decides anything differs.
    for key in _AUTHORITY_KEYS:
        assert with_cap.get(key) == without.get(key), f"capacity moved {key!r}"

    # Byte-identical once the purely-additive shadow key is removed.
    a = {k: v for k, v in with_cap.items() if k != "juice_capacity"}
    b = {k: v for k, v in without.items() if k != "juice_capacity"}
    assert a == b


def test_capacity_reaches_the_ranker_and_never_the_blocks_list():
    """THE INVARIANT, rewritten for the ranker (§1.7) rather than deleted.

    Capacity now reaches the RANKER — granted deliberately by this reviewed
    change. What is unchanged, and is the whole safety property, is that it can
    never reach ``blocks``, the list that carries VETO authority.

    Asserted two ways, both structural:

      1. no VETO-authority module IMPORTS the capacity store. Checked against the
         parsed import graph rather than a substring grep, because a grep over
         source also matches prose in a docstring — which made the old check pass
         or fail on comment wording rather than on behaviour.
      2. ``scan_verdict.evaluate`` is keyword-only and accepts no capacity-shaped
         argument, so there is no parameter through which one could become a block.
    """
    import ast
    import pytest as _pytest
    import scan_score
    import scan_verdict as _sv

    here = os.path.dirname(os.path.abspath(__file__))
    for module in ("scan_triggers.py", "scan_verdict.py", "screening.py",
                   "account_gate.py", "execution_gate.py", "executor.py",
                   "queue_state.py", "recommendation_engine.py",
                   "recommendation_runner.py", "position_manager.py", "alerts.py"):
        with open(os.path.join(here, module), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "juice_capacity" not in imported, \
            f"{module} imports the capacity store"

    # No parameter through which capacity could reach the veto set...
    for name in ("capacity", "capacity_pct", "juice_capacity"):
        with _pytest.raises(TypeError):
            _sv.evaluate(**{name: 0.0})
    # ...and a live one on the RANKER, which is where it belongs now.
    assert "capacity_pct" in inspect.signature(scan_score.compute_score).parameters


def test_capacity_declares_itself_shadow_and_non_blocking(store):
    """SHADOW is a literal, not a flag read from config: there is no switch that
    can make this blocking, and a reader (UI, log, test) can rely on that."""
    _seed_dates("SYN", [0.5] * 25, _dates(25))
    read = jc.capacity("SYN", floor_pct=0.75)
    assert read["shadow"] is True and read["blocking"] is False
    assert jc.summary()["shadow"] is True and jc.summary()["blocking"] is False


def test_no_config_switch_grants_capacity_authority():
    """Mirrors the shadow-floor / chart-structure contract: graduating this metric
    is a reviewed CODE change, so no env-tunable flag may exist that turns it on."""
    for name in dir(config):
        if "CAPACITY" not in name:
            continue
        value = getattr(config, name)
        assert not isinstance(value, bool), f"config.{name} looks like an authority switch"


# ===========================================================================
# 9. Canonical fixtures pass unmodified
# ===========================================================================
def test_canonical_xlk_july6_fixture_unchanged(store, monkeypatch):
    """The labeled failure case must behave EXACTLY as before. Pinned through the
    same lights/ATR path test_dividend_profile pins, with the capacity store
    populated — a shadow metric cannot touch it."""
    import genius_lights
    import indicators
    import stock_lights

    df = pd.read_parquet(os.path.join(FIX_REGIME, "xlk_july6_rollover.parquet"))
    _seed_dates("XLK", [0.02] * 60, _dates(60))   # deep sub-floor capacity on record

    eng = genius_lights.compute(df)
    assert eng["lights"]["sar"]["signal"] == "red" or eng["lights"]["momentum"]["signal"] == "red"
    assert eng["greens"] < 4
    assert indicators.atr_expanding(df) is True
    res = stock_lights.compute(df, ivr_percentile=95.0, is_etf=True)
    assert res["verdict"] == stock_lights.RED
    assert "veto:atr_expanding_high_ivr" in res["veto_reasons"]


def test_canonical_low_juice_fixture_is_structurally_thin(shares_mode, store, monkeypatch):
    """The PNC-shaped fixture — a pristine early advance on a genuinely low-vol
    name — is this feature's canonical STRUCTURAL case, and the substitute for the
    "GDDY Aug 21" artifact, which does not exist in this repo (AUDIT §Q5).

    Its structure/RS/SYM are all green; only the economics are wrong. Capacity is
    what makes that legible ahead of time, and it still changes nothing.
    """
    import data_handler
    import dividends
    df = pd.read_parquet(os.path.join(FIX_STRUCT, "early_advance_low_juice.parquet"))
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    monkeypatch.setattr(dividends, "cached_annual_yield_pct", lambda t, state=None: None)

    jc.backfill(["PNCLIKE"])
    read = jc.capacity("PNCLIKE", floor_pct=config.SHARES_JUICE_FLOOR_PCT)
    assert read["status"] == jc.OK
    assert read["obs"] >= config.CAPACITY_MIN_OBS
    # Structurally thin: the MEDIAN was never near the floor, so no pullback fixes
    # it. This is the transient/structural call the second prompt consumes.
    assert read["capacity_wk_pct"] < config.SHARES_JUICE_FLOOR_PCT
    assert read["clears_floor"] is False
