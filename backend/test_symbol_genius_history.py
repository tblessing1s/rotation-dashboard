"""Tests for the Symbol Genius flip shadow-log (the dwell prerequisite)."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-sgh-"))

import config  # noqa: E402
import symbol_genius_history as sgh  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sgh, "HISTORY_PATH", str(tmp_path / "sgh.json"))


def test_record_is_idempotent_per_day(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    sgh.record("NVDA", "green", 4, day="2026-01-05")
    sgh.record("NVDA", "yellow", 3, day="2026-01-05")   # same day -> replace
    recs = sgh.series("NVDA")
    assert len(recs) == 1 and recs[0]["color"] == "yellow"


def test_flip_count_counts_color_changes(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for day, color in [("2026-01-05", "green"), ("2026-01-06", "green"),
                       ("2026-01-07", "yellow"), ("2026-01-08", "green")]:
        sgh.record("NVDA", color, 4, day=day)
    # green,green,yellow,green -> 2 changes (green->yellow, yellow->green)
    assert sgh.flip_count("NVDA") == 2


def test_flip_count_skips_null_colors(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for day, color in [("2026-01-05", "green"), ("2026-01-06", None), ("2026-01-07", "green")]:
        sgh.record("AAA", color, None, day=day)
    assert sgh.flip_count("AAA") == 0     # the gap isn't two flips


def test_record_caps_to_window(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SYMBOL_GENIUS_HISTORY_DAYS", 3)
    for i in range(6):
        sgh.record("AAA", "green", 4, day=f"2026-02-0{i+1}")
    assert len(sgh.series("AAA")) == 3


def test_record_many_and_flip_stats(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    sgh.record_many([{"ticker": "AAA", "color": "green", "greens": 4},
                     {"ticker": "BBB", "color": "red", "greens": 1}], day="2026-03-02")
    sgh.record_many([{"ticker": "AAA", "color": "yellow", "greens": 3},
                     {"ticker": "BBB", "color": "red", "greens": 1}], day="2026-03-03")
    stats = sgh.flip_stats()
    assert stats["summary"]["names"] == 2
    assert stats["symbols"]["AAA"]["flips"] == 1     # green->yellow
    assert stats["symbols"]["BBB"]["flips"] == 0     # steady red
    assert stats["summary"]["names_flipped"] == 1
    assert stats["symbols"]["AAA"]["current"] == "yellow"


def test_record_today_computes_from_bars(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import numpy as np
    import pandas as pd
    import data_handler
    idx = pd.bdate_range("2022-06-01", periods=260)
    c = 100 + np.cumsum(np.full(260, 0.4))
    df = pd.DataFrame({"Open": c, "High": c + 0.3, "Low": c - 0.3, "Close": c,
                       "Volume": np.full(260, 1e6)}, index=idx)
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df)
    out = sgh.record_today(["NVDA", "MSFT"], day="2026-04-01")
    assert out["ok"] and out["recorded"] == 2
    assert sgh.series("NVDA")[-1]["color"] == "green"


def test_flips_endpoint(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    sgh.record("AAA", "green", 4, day="2026-05-01")
    import app as flask_app
    body = flask_app.app.test_client().get("/api/symbol-genius/flips").get_json()
    assert "symbols" in body and body["summary"]["names"] == 1
