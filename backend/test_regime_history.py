"""Persistence + backfill + snapshot/alert integration for the regime engine.

All offline: the store lives under a temp DATA_DIR, and backfill reads a
monkeypatched in-memory frame instead of fetching.
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-regime-"))

import importlib  # noqa: E402

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import config  # noqa: E402

FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regime")


@pytest.fixture()
def rh(tmp_path, monkeypatch):
    """regime_history bound to an isolated DATA_DIR so each test starts empty."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import regime_history
    importlib.reload(regime_history)
    monkeypatch.setattr(regime_history, "REGIME_HISTORY_PATH",
                        str(tmp_path / "regime_history.json"))
    return regime_history


def _trace(published, raw=None):
    return {"status": published, "published_regime": published,
            "raw_condition": raw or published, "dwell_regime": published,
            "lights": {}, "vote": {"green_count": 0}, "dwell": {}, "secondary": {}}


# ---------------------------------------------------------------------------
# record / series / latest / prior_published
# ---------------------------------------------------------------------------
def test_record_is_idempotent_per_day(rh):
    rh.record(_trace("green"), day="2024-03-01")
    rh.record(_trace("yellow"), day="2024-03-01")   # same day -> replaces
    recs = rh.series()
    assert len(recs) == 1 and recs[0]["published_regime"] == "yellow"


def test_series_stays_sorted_and_capped(rh, monkeypatch):
    monkeypatch.setattr(config, "REGIME_HISTORY_DAYS", 3)
    for day in ["2024-03-04", "2024-03-01", "2024-03-05", "2024-03-02", "2024-03-03"]:
        rh.record(_trace("green"), day=day)
    dates = [r["date"] for r in rh.series()]
    assert dates == sorted(dates)                    # kept in order
    assert dates == ["2024-03-03", "2024-03-04", "2024-03-05"]   # newest 3 only


def test_prior_published_excludes_today(rh):
    rh.record(_trace("green"), day="2024-03-01")
    rh.record(_trace("yellow"), day="2024-03-02")
    rh.record(_trace("red"), day="2024-03-03")
    # Full series through the last record...
    assert rh.prior_published() == ["green", "yellow", "red"]
    # ...but excluding "today" drops today's own record (so dwell can't self-count).
    assert rh.prior_published(before="2024-03-03") == ["green", "yellow"]


def test_latest_before(rh):
    rh.record(_trace("green"), day="2024-03-01")
    rh.record(_trace("red"), day="2024-03-02")
    assert rh.latest()["published_regime"] == "red"
    assert rh.latest(before="2024-03-02")["published_regime"] == "green"
    assert rh.latest(before="2024-03-01") is None


# ---------------------------------------------------------------------------
# backfill — derived from cached bars, dwell accumulated across the sequence
# ---------------------------------------------------------------------------
def test_backfill_from_cached_bars(rh, monkeypatch):
    df = pd.read_parquet(os.path.join(FIX_DIR, "sustained_green.parquet"))
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "get_many", lambda syms, force=False: {})

    out = rh.backfill(force=True)
    assert out.get("ok") and out["records"] > 0
    recs = rh.series()
    # A confirmed uptrend backfills to a solid GREEN block, marked backfilled, and
    # dated in order.
    assert all(r["backfilled"] for r in recs)
    assert {r["published_regime"] for r in recs} == {"green"}
    assert [r["date"] for r in recs] == sorted(r["date"] for r in recs)


def test_backfill_noops_when_history_present(rh, monkeypatch):
    rh.record(_trace("green"), day="2024-03-01")
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: None)
    out = rh.backfill(force=False)
    assert out.get("skipped") == "history present"


# ---------------------------------------------------------------------------
# Entry-context snapshot — additive v2 trace; old snapshots still load
# ---------------------------------------------------------------------------
def test_snapshot_regime_section_carries_full_trace():
    import entry_context
    trace = _trace("green", raw="green")
    trace["lights"] = {"close_vs_ma": {"signal": "green"}, "fast_vs_slow": {"signal": "green"},
                       "sar": {"signal": "green"}, "momentum": {"signal": "green"}}
    gate = {"levels": [{"level": 1, "detail": trace}]}
    missing = []
    section = entry_context._regime_section(gate, lambda p, v, r: v, "unavailable")
    # legacy v1 fields still present...
    assert "status" in section and "breadth" in section and "vix" in section
    # ...plus the additive v2 decision trace.
    assert section["published_regime"] == "green"
    assert section["lights"]["sar"]["signal"] == "green"
    assert config.SNAPSHOT_SCHEMA_VERSION == 2


def test_old_v1_snapshot_still_loads():
    import entry_context
    # A pre-v2 snapshot lacks the four-light fields entirely; the digest helper
    # must still read it without error (additive change, no migration).
    v1 = {"scorecard": {"verdict": "GO"}, "regime": {"status": "green"},
          "iv": {"iv_rank": 40}, "stock": {"rs3m_vs_spy": 5.0, "rs3m_vs_sector": 2.0}}
    summary = entry_context.summary(v1)
    assert summary["regime"] == "green" and summary["verdict"] == "GO"


# ---------------------------------------------------------------------------
# Alert — fires once per PUBLISHED transition, never on raw flaps
# ---------------------------------------------------------------------------
def test_regime_change_alert_fires_on_published_transition(monkeypatch):
    import alerts
    import regime_history
    import screening
    monkeypatch.setattr(screening, "regime",
                        lambda: {"published_regime": "red", "raw_condition": "red",
                                 "lights": {}, "secondary": {}})
    monkeypatch.setattr(regime_history, "latest",
                        lambda before=None: {"date": "2024-03-01", "published_regime": "green"})
    out = alerts.check_regime_change({})
    assert len(out) == 1
    a = out[0]
    assert a["type"] == "REGIME_CHANGE"
    assert a["data"]["from"] == "green" and a["data"]["to"] == "red"
    assert a["fingerprint"].endswith("green->red")


def test_regime_change_alert_silent_when_unchanged(monkeypatch):
    import alerts
    import regime_history
    import screening
    monkeypatch.setattr(screening, "regime",
                        lambda: {"published_regime": "green", "raw_condition": "green",
                                 "lights": {}, "secondary": {}})
    monkeypatch.setattr(regime_history, "latest",
                        lambda before=None: {"date": "2024-03-01", "published_regime": "green"})
    assert alerts.check_regime_change({}) == []


def test_regime_change_alert_ignores_raw_flap(monkeypatch):
    import alerts
    import regime_history
    import screening
    # Published held green by the dwell even though the raw vote flipped to yellow:
    # no published transition -> no alert (raw flaps must not fire).
    monkeypatch.setattr(screening, "regime",
                        lambda: {"published_regime": "green", "raw_condition": "yellow",
                                 "lights": {}, "secondary": {}})
    monkeypatch.setattr(regime_history, "latest",
                        lambda before=None: {"date": "2024-03-01", "published_regime": "green"})
    assert alerts.check_regime_change({}) == []


def test_regime_change_alert_cold_start_silent(monkeypatch):
    import alerts
    import regime_history
    import screening
    monkeypatch.setattr(screening, "regime",
                        lambda: {"published_regime": "green", "raw_condition": "green",
                                 "lights": {}, "secondary": {}})
    monkeypatch.setattr(regime_history, "latest", lambda before=None: None)
    assert alerts.check_regime_change({}) == []


# ---------------------------------------------------------------------------
# Calibration loader — comparison-only recompute under alternative params
# ---------------------------------------------------------------------------
def test_calibration_regime_series_and_param_compare(monkeypatch):
    import calibration
    import data_handler
    df = pd.read_parquet(os.path.join(FIX_DIR, "distribution_rollover.parquet"))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "get_many", lambda syms, force=False: {})

    rows = calibration.regime_series(step=1)
    assert rows and {"green", "yellow", "red"} <= {r["published_regime"] for r in rows}

    cmp = calibration.regime_param_compare({"defaults": {}, "no_dwell": {"dwell_days": 1}})
    for label, s in cmp.items():
        # The dwell can only remove flaps, so the published series is never LESS
        # steady than the raw vote.
        assert s["published_transitions"] <= s["raw_transitions"]
    # The default 3-day dwell is at least as steady as the 1-day (near-raw) set.
    assert cmp["defaults"]["published_transitions"] <= cmp["no_dwell"]["published_transitions"]


def test_calibration_regime_vs_cycles_buckets_by_entry_regime(monkeypatch):
    import calibration
    import data_handler
    df = pd.read_parquet(os.path.join(FIX_DIR, "distribution_rollover.parquet"))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "get_many", lambda syms, force=False: {})
    # A closed cycle whose entry date lands in the fixture's history.
    entry_day = str(df.index[120].date())
    state = {"cycles": [{"entry_date": entry_day, "net_return_pct": 12.0}]}
    out = calibration.regime_vs_cycles(state)
    total = sum(v["n"] for v in out["defaults"].values())
    assert total == 1


# ---------------------------------------------------------------------------
# Strike-policy documented constants (scoped follow-up — table not rewritten)
# ---------------------------------------------------------------------------
def test_documented_strike_atr_multiples_present():
    # The documented GREEN/YELLOW ATR multiples exist as HARD_CFM_RULE constants;
    # reconciling the live STRIKE_TABLE to them is the scoped follow-up. The regime
    # plumbing is already correct: suggest_strike consumes the published regime.
    assert config.STRIKE_ATR_MULT_GREEN == 1.5
    assert config.STRIKE_ATR_MULT_YELLOW == 2.0


def test_strike_policy_consumes_published_regime():
    import strike_policy
    green = strike_policy.suggest_strike(100.0, 2.0, "green", posture="conservative")
    red = strike_policy.suggest_strike(100.0, 2.0, "red", posture="conservative")
    # A red tape selects a strike no shallower than a green one (deeper protection).
    assert red["strike"] <= green["strike"]
    assert green["regime"] == "green" and red["regime"] == "red"
