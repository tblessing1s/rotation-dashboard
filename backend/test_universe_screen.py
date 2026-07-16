"""Tests for the offline universe-intake screen + the candidate store."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import candidate_universe as cu
import universe_screen as us


def _bars(closes, volume=1_000_000.0):
    closes = np.asarray(closes, float)
    n = len(closes)
    idx = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({"Open": closes, "High": closes + 1, "Low": closes - 1,
                         "Close": closes, "Volume": np.full(n, volume)}, index=idx)


def _steady_up(n=120, start=100.0, gain=0.1):
    # A rising advance WITH real day-to-day wobble (up and down days) so RSI is
    # computable and lands mid-band (~61) and Perf Quarter clears +15%.
    wobble = np.tile([1.5, -1.05], n // 2 + 1)[:n]
    return start + np.cumsum(np.full(n, gain) + wobble)


def test_avg_volume_pure_and_none_short():
    assert us.avg_volume(_bars([1] * 60, volume=600_000)) == pytest.approx(600_000)
    assert us.avg_volume(_bars([1] * 10)) is None       # < window


def test_perf_quarter_and_rsi_computable_offline():
    df = _bars(_steady_up(120), volume=600_000)
    res = us.evaluate(df, has_weeklies=True)
    assert res["criteria"]["perf_quarter"]["computable"] is True
    assert res["criteria"]["rsi"]["computable"] is True
    assert res["criteria"]["avg_volume"]["value"] == pytest.approx(600_000)


def test_market_cap_is_descoped_never_blocks():
    df = _bars(_steady_up(120), volume=600_000)
    res = us.evaluate(df, has_weeklies=True)
    mc = res["criteria"]["market_cap"]
    assert mc["descoped"] is True and mc["pass"] is None and mc["computable"] is False


def test_known_no_weeklies_excludes():
    df = _bars(_steady_up(120), volume=600_000)
    passed = us.evaluate(df, has_weeklies=True)["pass"]
    assert us.evaluate(df, has_weeklies=False)["pass"] is False
    # Optionability unknown offline (None) never turns a pass into a fail.
    assert us.evaluate(df, has_weeklies=None)["pass"] == passed


def test_thin_volume_fails_and_is_reported():
    df = _bars(_steady_up(120), volume=100_000)      # below the 500K floor
    res = us.evaluate(df, has_weeklies=True)
    assert res["pass"] is False
    assert "avg_volume" in us.failing_criteria(res)


def test_screen_over_frames_returns_candidates():
    frames = {
        "AAA": _bars(_steady_up(120), volume=600_000),   # passes
        "BBB": _bars(_steady_up(120), volume=100_000),   # thin volume -> fails
    }
    out = us.screen(["AAA", "BBB"], frames, weeklies={"AAA": True, "BBB": True})
    assert "AAA" in out["passed"] and "BBB" not in out["passed"]


# ---------------------------------------------------------------------------
# Candidate store — change log + diversity + weekly gate.
# ---------------------------------------------------------------------------
def test_record_diff_changelog_and_diversity(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "STORE_PATH", str(tmp_path / "cand.json"))
    sector_of = {"AAA": "XLK", "BBB": "XLK", "CCC": "XLF"}.get

    r1 = cu.record({"passed": ["AAA", "BBB"], "results": {}}, sector_of, day="2026-01-09")
    assert r1["added"] == 2 and r1["dropped"] == 0
    assert cu.current() == ["AAA", "BBB"]
    assert cu.report()["diversity"] == {"XLK": 2}

    # Next week: BBB drops (with its failing criterion), CCC added.
    results = {"BBB": {"criteria": {"rsi": {"computable": True, "pass": False}}}}
    r2 = cu.record({"passed": ["AAA", "CCC"], "results": results}, sector_of, day="2026-01-16")
    assert r2["added"] == 1 and r2["dropped"] == 1
    log = cu.report()["changelog"]
    dropped = [e for e in log if e["action"] == "dropped" and e["ticker"] == "BBB"]
    assert dropped and "rsi" in dropped[0]["criterion"]
    assert cu.report()["diversity"] == {"XLK": 1, "XLF": 1}


def test_weekly_due_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "STORE_PATH", str(tmp_path / "cand.json"))
    assert cu.weekly_due("2026-01-14") is False        # Wednesday — wait
    assert cu.weekly_due("2026-01-16") is True          # Friday, nothing recorded yet
    cu.record({"passed": ["AAA"], "results": {}}, {}.get, day="2026-01-16")
    assert cu.weekly_due("2026-01-17") is False         # already ran this ISO week
    assert cu.weekly_due("2026-01-23") is True          # next Friday


def test_active_universe_gated_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "STORE_PATH", str(tmp_path / "cand.json"))
    cu.record({"passed": ["AAA"], "results": {}}, {}.get, day="2026-01-16")
    monkeypatch.setattr(cu.config, "UNIVERSE_SCREEN_ENABLED", False)
    assert cu.active_universe(["FALLBACK"]) == ["FALLBACK"]     # shadow by default
    monkeypatch.setattr(cu.config, "UNIVERSE_SCREEN_ENABLED", True)
    assert cu.active_universe(["FALLBACK"]) == ["AAA"]          # promoted when enabled
