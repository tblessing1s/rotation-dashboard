"""Trailing juice capacity — observation emission, the median + its guard, the
structural-vs-transient discrimination, backfill provenance, and the no-authority
invariant. Offline: cached bars and fixture observations only, never a provider.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-capacity-test-"))

import config  # noqa: E402
import juice_capacity as jc  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Every test gets its own store file, and the mtime memo starts cold."""
    monkeypatch.setattr(jc, "LOG_PATH", str(tmp_path / "capacity.json"))
    monkeypatch.setattr(jc, "_parsed", None)
    yield


def _day(n: int) -> str:
    """Day n as a date string, ordered so a plain string sort is chronological."""
    return f"2026-{1 + n // 28:02d}-{1 + n % 28:02d}"


def _write(symbol: str, values, source: str = jc.SOURCE_LIVE, start: int = 0):
    """Persist a series of combined-yield observations, one per distinct day."""
    rows = [{"ticker": symbol, "juice_weekly_pct": v} for v in values]
    for i, row in enumerate(rows):
        jc.record_scan([row], day=_day(start + i))
    if source != jc.SOURCE_LIVE:  # rewrite the tag without re-deriving the values
        data = jc._load_raw()
        for obs in data["symbols"][symbol]:
            obs["source"] = source
        jc._save(data)
        jc._parsed = None


# ---------------------------------------------------------------------------
# Observation emission (1.5) — shape, and one point per symbol per day
# ---------------------------------------------------------------------------
def test_scan_pass_emits_correctly_shaped_observations():
    rows = [
        {"ticker": "NVDA", "juice_weekly_pct": 1.20, "short_strike": 168.5,
         "annual_dividend_yield_pct": None},
        {"ticker": "ET", "juice_weekly_pct": 0.14, "short_strike": 17.0,
         "annual_dividend_yield_pct": 8.84},
    ]
    out = jc.record_scan(rows, day="2026-08-24", regime="green")
    assert out["ok"] and out["recorded"] == 2

    nvda = jc.series("NVDA")[-1]
    assert nvda["symbol"] == "NVDA" and nvda["date"] == "2026-08-24"
    assert nvda["achievable_juice_wk_pct"] == 1.20
    assert nvda["strike_used"] == 168.5 and nvda["regime"] == "green"
    assert nvda["source"] == jc.SOURCE_LIVE and nvda["schema"] == jc.SCHEMA_VERSION
    # An unresolved dividend yield stays UNKNOWN and contributes nothing — it is
    # never recorded as a confident zero.
    assert nvda["dividend_known"] is False and nvda["dividend_wk_pct"] is None
    assert nvda["combined_wk_pct"] == 1.20

    et = jc.series("ET")[-1]
    assert et["dividend_known"] is True
    assert et["dividend_wk_pct"] == round(8.84 / config.DIVIDEND_WEEKS_PER_YEAR, 4)
    assert et["combined_wk_pct"] == round(0.14 + 8.84 / 52, 4)


def test_unpriceable_name_emits_no_observation_rather_than_a_zero():
    """A name we can't price is UNMEASURED. Recording a 0 would drag its own
    median down and manufacture a structural verdict out of a data outage."""
    out = jc.record_scan([{"ticker": "AAA", "juice_weekly_pct": None}], day=_day(0))
    assert out["ok"] and out["recorded"] == 0
    assert jc.series("AAA") == []


def test_same_day_re_emission_replaces_rather_than_appends():
    """[CAPACITY_ONE_PER_DAY] A regime flip forces a second sweep the same
    session. That day must still contribute exactly one point, or a median would
    be weighted toward precisely the days juice is least representative."""
    jc.record_scan([{"ticker": "AAA", "juice_weekly_pct": 1.0}], day="2026-08-24")
    jc.record_scan([{"ticker": "AAA", "juice_weekly_pct": 1.4}], day="2026-08-24")
    obs = jc.series("AAA")
    assert len(obs) == 1 and obs[0]["combined_wk_pct"] == 1.4  # last write wins


# ---------------------------------------------------------------------------
# Median correctness + window boundary (1.5)
# ---------------------------------------------------------------------------
def test_median_of_a_known_series():
    _write("AAA", [0.1, 0.9, 0.3, 0.7, 0.5] + [0.5] * 15)   # 20 days
    assert jc.juice_capacity_wk_pct("AAA") == 0.5


def test_median_of_an_even_series_averages_the_middle_pair():
    _write("AAA", [1.0, 2.0] * 10)          # 20 points: median = (1.0+2.0)/2
    assert jc.juice_capacity_wk_pct("AAA") == 1.5


def test_window_boundary_excludes_the_observation_past_the_window():
    """An observation at day WINDOW+1 is outside the trailing window. Written
    OLDEST first, so the stale point is the one that must fall out."""
    window = config.CAPACITY_WINDOW_DAYS
    # One ancient outlier, then a full window of 0.50s.
    _write("AAA", [99.0] + [0.50] * window, start=0)
    detail = jc.capacity_detail("AAA")
    assert detail["obs"] == window          # the outlier is outside the window
    assert detail["capacity"] == 0.50       # and cannot move the median
    # It is still on disk — windowing is a read-time concern, not a deletion.
    assert len(jc.series("AAA")) == window + 1


# ---------------------------------------------------------------------------
# The minimum-observations guard (1.5)
# ---------------------------------------------------------------------------
def test_guard_reports_insufficient_history_below_the_minimum():
    _write("AAA", [0.5] * (config.CAPACITY_MIN_OBS - 1))
    assert jc.juice_capacity_wk_pct("AAA") == jc.INSUFFICIENT_HISTORY
    detail = jc.capacity_detail("AAA")
    assert detail["insufficient_history"] is True
    assert detail["obs"] == config.CAPACITY_MIN_OBS - 1


def test_guard_releases_at_exactly_the_minimum():
    _write("AAA", [0.5] * config.CAPACITY_MIN_OBS)
    assert jc.juice_capacity_wk_pct("AAA") == 0.5
    assert jc.capacity_detail("AAA")["insufficient_history"] is False


def test_insufficient_history_is_a_sentinel_not_none_and_not_zero():
    """A consumer must be able to tell "not watched long enough" from "cannot be
    priced" (None) and from a genuine low reading (0.0). All three are different
    facts and only the sentinel is unsuppressible."""
    assert jc.juice_capacity_wk_pct("NEVER_SEEN") == jc.INSUFFICIENT_HISTORY
    assert jc.INSUFFICIENT_HISTORY is not None
    assert jc.INSUFFICIENT_HISTORY != 0 and jc.INSUFFICIENT_HISTORY != 0.0


def test_guard_counts_distinct_days_not_records():
    """MIN_OBS is a statistical-meaningfulness guard, so it counts DAYS. Twenty
    records drawn from four heavily re-swept sessions is not twenty days."""
    for i in range(4):                       # 4 days
        for _ in range(5):                   # 5 sweeps each = 20 records
            jc.record_scan([{"ticker": "AAA", "juice_weekly_pct": 0.5}], day=_day(i))
    detail = jc.capacity_detail("AAA")
    assert detail["obs"] == 4
    assert detail["capacity"] == jc.INSUFFICIENT_HISTORY


# ---------------------------------------------------------------------------
# The discrimination prompt 2 depends on — PINNED HERE (1.5)
# ---------------------------------------------------------------------------
def test_structural_and_transient_shapes_are_distinguishable():
    """The whole point of the metric. Two names, both reading ~0.30-0.40%/wk
    TODAY, that must not be classified alike:

      * STRUCTURAL — a flat 0.30%/wk series. Capacity 0.30: it has never paid
        more, and no amount of waiting changes that.
      * COMPRESSED — 0.85%/wk historically, with the last 15 observations at
        0.40%/wk. Capacity stays 0.85: the name has demonstrated it CAN pay, and
        the current reading is a condition, not the instrument.

    A metric that read the current value instead of the median would collapse
    these two into one, which is exactly the bug the bench has today.
    """
    _write("STRUCT", [0.30] * 60)
    _write("COMPRESSED", [0.85] * 60 + [0.40] * 15)

    assert jc.juice_capacity_wk_pct("STRUCT") == 0.30
    assert jc.juice_capacity_wk_pct("COMPRESSED") == 0.85

    # And the CURRENT reading is the leg that moved — both look weak right now.
    assert jc.capacity_detail("STRUCT")["current_wk_pct"] == 0.30
    assert jc.capacity_detail("COMPRESSED")["current_wk_pct"] == 0.40


# ---------------------------------------------------------------------------
# Dividend combination (1.5)
# ---------------------------------------------------------------------------
def test_dividend_leg_combines_into_the_observation():
    """The motivating ET shape: a thin juice leg plus a fat dividend leg."""
    obs = jc.observation("ET", _day(0), 0.14, 8.84)
    assert obs["achievable_juice_wk_pct"] == 0.14
    assert obs["dividend_wk_pct"] == 0.17     # 8.84 / 52
    assert obs["combined_wk_pct"] == 0.31
    assert obs["dividend_known"] is True


def test_unknown_dividend_yields_combined_equals_juice_with_a_marker():
    obs = jc.observation("AAA", _day(0), 1.10, None)
    assert obs["combined_wk_pct"] == obs["achievable_juice_wk_pct"] == 1.10
    assert obs["dividend_known"] is False
    assert obs["dividend_wk_pct"] is None


def test_combined_leg_goes_through_the_shared_scan_triggers_function():
    """Capacity must not carry a second definition of "combined" — a median and
    a displayed combined yield that disagree would be worse than neither."""
    import scan_triggers
    expected = scan_triggers.combined_weekly_yield(0.14, 8.84)
    obs = jc.observation("ET", _day(0), 0.14, 8.84)
    assert obs["combined_wk_pct"] == expected["combined_weekly_yield_pct"]
    assert obs["dividend_wk_pct"] == expected["dividend_weekly_pct"]


# ---------------------------------------------------------------------------
# Recompute-from-history (1.5)
# ---------------------------------------------------------------------------
def test_capacity_is_recomputed_from_persisted_observations():
    _write("AAA", [0.2, 0.4, 0.6, 0.8, 1.0] * 5)     # 25 days
    first = jc.juice_capacity_wk_pct("AAA")
    stored = jc.series("AAA")

    jc._parsed = None                                # drop every in-process memo
    assert jc.juice_capacity_wk_pct("AAA") == first
    # No aggregate was persisted — only observations.
    assert all("capacity" not in o for o in stored)
    assert jc._load_raw().keys() == {"symbols"}


def test_historical_observations_are_never_mutated_by_a_later_write():
    _write("AAA", [0.5] * 5)
    before = [dict(o) for o in jc.series("AAA")]
    jc.record_scan([{"ticker": "AAA", "juice_weekly_pct": 9.9}], day=_day(99))
    after = jc.series("AAA")
    assert after[:5] == before                        # untouched
    assert after[-1]["combined_wk_pct"] == 9.9


# ---------------------------------------------------------------------------
# Backfill + seed provenance (1.2)
# ---------------------------------------------------------------------------
def _synthetic_bars(n: int = 300, seed: int = 7) -> pd.DataFrame:
    """A price series with enough movement for ATR and realized vol to exist."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": np.full(n, 1_000_000.0),
    }, index=idx)


def test_backfill_replays_bars_into_tagged_observations(monkeypatch):
    import data_handler
    bars = _synthetic_bars()
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: bars)

    out = jc.backfill(["AAA"])
    assert out["ok"] and out["recorded"] > config.CAPACITY_MIN_OBS

    obs = jc.series("AAA")
    # Every replayed point is distinguishable from a live one FOREVER, and none
    # of them claims a dividend leg it could not have recovered.
    assert all(o["source"] == jc.SOURCE_BACKFILL for o in obs)
    assert all(o["dividend_known"] is False for o in obs)
    assert all(o["combined_wk_pct"] == o["achievable_juice_wk_pct"] for o in obs)
    assert all(o["strike_used"] is not None for o in obs)
    detail = jc.capacity_detail("AAA")
    assert detail["by_source"] == {jc.SOURCE_BACKFILL: detail["obs"]}


def test_backfill_reproduces_what_the_live_scan_would_have_printed(monkeypatch):
    """The claim the backfill rests on: because the juice number is computed
    entirely from daily bars (ATR -> strike, realized vol -> sigma, BSM ->
    extrinsic, over spot) with NO chain input, a replay is not an approximation
    of the historical reading — it IS the reading."""
    import account_gate
    import data_handler
    bars = _synthetic_bars()
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: bars)
    jc.backfill(["AAA"])

    # Take the newest replayed point and recompute it the way the live scan does.
    newest = jc.series("AAA")[-1]
    live = account_gate.juice_estimate("AAA", bars)
    assert newest["achievable_juice_wk_pct"] == live["weekly_yield_pct"]
    assert newest["strike_used"] == live["short_strike"]


def test_backfill_never_overwrites_a_live_observation(monkeypatch):
    """Synthesized history yields to a real reading, whichever order they land."""
    import data_handler
    bars = _synthetic_bars()
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: bars)

    live_day = str(bars.index[100])[:10]
    jc.record_scan([{"ticker": "AAA", "juice_weekly_pct": 4.44}], day=live_day)
    jc.backfill(["AAA"], force=True)

    kept = next(o for o in jc.series("AAA") if o["date"] == live_day)
    assert kept["source"] == jc.SOURCE_LIVE and kept["combined_wk_pct"] == 4.44


def test_backfill_skips_a_symbol_that_already_has_history(monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda t, force=False: _synthetic_bars())
    _write("AAA", [0.5] * 3)
    out = jc.backfill(["AAA"])
    assert out["recorded"] == 0 and "AAA" in out["skipped"]
    assert len(jc.series("AAA")) == 3


def test_backfill_survives_a_symbol_with_no_bars(monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: None)
    out = jc.backfill(["AAA"])
    assert out["ok"] and out["recorded"] == 0 and "AAA" in out["skipped"]


def test_seed_recovers_live_observations_from_the_rejection_log(monkeypatch,
                                                               tmp_path):
    """scan_rejection_log has been persisting combined_weekly_yield_pct per
    candidate since schema v21. Those are real readings taken at the time, so
    they are recovered under their own tag rather than the backfill one."""
    import scan_rejection_log as srl
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "rej.json"))
    for i in range(3):
        srl.record_scan([{"ticker": "ET", "verdict": "WATCH", "verdict_reasons": [],
                          "juice_weekly_pct": 0.14, "dividend_weekly_pct": 0.17,
                          "combined_weekly_yield_pct": 0.31}], day=_day(i))

    out = jc.seed_from_scan_rejection_log(["ET"])
    assert out["ok"] and out["recorded"] == 3
    obs = jc.series("ET")
    assert all(o["source"] == jc.SOURCE_SEED for o in obs)
    # The dividend leg survives the round trip — these are not juice-only points.
    assert obs[0]["dividend_known"] is True
    assert obs[0]["combined_wk_pct"] == 0.31


def test_seed_collapses_a_multi_run_day_to_one_observation(monkeypatch, tmp_path):
    """The rejection log appends per scan RUN; the capacity store is per DAY."""
    import scan_rejection_log as srl
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "rej.json"))
    for juice in (0.10, 0.20, 0.30):
        srl.record_scan([{"ticker": "ET", "verdict": "WATCH", "verdict_reasons": [],
                          "juice_weekly_pct": juice}], day="2026-08-24")
    jc.seed_from_scan_rejection_log(["ET"])
    obs = jc.series("ET")
    assert len(obs) == 1 and obs[0]["combined_wk_pct"] == 0.30   # last of the day


# ---------------------------------------------------------------------------
# The floor the readout quotes (Phase 0 §8.2)
# ---------------------------------------------------------------------------
def test_capacity_quotes_the_same_profile_aware_floor_as_the_shadow_floor():
    """One name must never be shown a capacity measured against one bar and a
    shadow verdict measured against another."""
    import income_profile
    import scan_triggers
    _write("ET", [0.31] * config.CAPACITY_MIN_OBS)

    for profile in (income_profile.DIVIDEND_COMPOUNDER, None):
        detail = jc.capacity_detail("ET", profile)
        shadow = scan_triggers.shadow_floor(profile, 0.14, 8.84)
        assert detail["floor_pct"] == shadow["floor_pct"]
        assert detail["floor_basis"] == shadow["basis"]


# ---------------------------------------------------------------------------
# Authority: none (1.4)
# ---------------------------------------------------------------------------
def test_capacity_is_marked_shadow_and_non_blocking():
    _write("AAA", [0.1] * config.CAPACITY_MIN_OBS)
    detail = jc.capacity_detail("AAA")
    # Literals, not config reads: no switch exists that could flip these, and a
    # reader (UI, log, test) is entitled to rely on that.
    assert detail["shadow"] is True and detail["blocking"] is False


def test_capacity_has_no_consumer_outside_display_telemetry_and_tests():
    """The grep-level check prompt 1.4 asks for, as an executable assertion.

    A capacity reference appearing in the gate, the verdict composition, the
    ranking, the bench or the kill switch is the failure this pins. The one
    production consumer is the scan ROW (a display key) plus the nightly sweep
    that emits observations.

    Keyed on the IMPORT, not on the string: a module that merely names capacity
    in a comment or docstring (config.py's constants, scan_triggers' note on the
    shared floor) is not a consumer, and forbidding the word would only teach
    the next author to stop cross-referencing."""
    import ast
    import pathlib
    backend = pathlib.Path(__file__).parent
    allowed = {"juice_capacity.py",            # the module itself
               "metrics/scorecard.py",         # the display row key
               "maintenance.py"}               # the observation emitter
    offenders = []
    for path in sorted(backend.rglob("*.py")):
        rel = path.relative_to(backend).as_posix()
        if rel.startswith("test_") or rel in allowed or "/test_" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                    a.name == "juice_capacity" for a in node.names):
                offenders.append(rel)
            elif isinstance(node, ast.ImportFrom) and node.module == "juice_capacity":
                offenders.append(rel)
    assert offenders == [], f"capacity leaked into: {offenders}"


def test_far_below_floor_capacity_leaves_the_scan_row_verdict_untouched(monkeypatch):
    """Byte-identical scan output with the feature live vs. inert. An ET-shaped
    name — capacity 0.31%/wk against a 0.75% floor — must produce exactly the
    verdict, bench status and ordering it produced before capacity existed."""
    from metrics import scorecard

    bars = _synthetic_bars()
    monkeypatch.setattr("data_handler.get_daily", lambda t, force=False: bars)
    monkeypatch.setattr("data_handler.prefetch", lambda *a, **k: None)

    def _row():
        return scorecard.score_ticker("AAA", bars, "XLK", bars, None,
                                      has_weeklies=True, regime_color="green")

    _write("AAA", [0.31] * 40)                       # capacity far below any floor
    with_capacity = _row()
    assert with_capacity["juice_capacity"]["capacity"] == 0.31

    monkeypatch.setattr(jc, "LOG_PATH", "/nonexistent/capacity.json")
    monkeypatch.setattr(jc, "_parsed", None)
    without = _row()
    assert without["juice_capacity"]["insufficient_history"] is True

    # Everything the scan actually acts on is identical between the two.
    for key in ("verdict", "verdict_reasons", "suitability", "bench", "score",
                "juice_weekly_pct", "net_juice_weekly_pct", "gate_cleared_level",
                "shadow_floor", "structure", "right_spot", "ticker", "sector"):
        assert with_capacity.get(key) == without.get(key), f"{key} moved"
