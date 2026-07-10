"""Entry-context snapshot tests — capture completeness, immutability across a
derived rebuild, the missing-data (null-with-reason) policy, and the >25%-null
data-quality alert. All offline, no provider keys, mocked clocks/frames.

Run: python -m pytest backend/test_entry_context.py -q
"""
import json
import os
import tempfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config             # noqa: E402
import data_handler       # noqa: E402
import entry_context      # noqa: E402
import executor           # noqa: E402
import iv_history         # noqa: E402
import logging_handler as log  # noqa: E402
import screening          # noqa: E402


def _frame(level=100.0, n=260, seed=1):
    """A long, gently trending OHLCV frame (>=200 bars so MA200/RS3M populate)."""
    idx = pd.bdate_range("2023-01-02", periods=n)
    rng = np.random.RandomState(seed)
    prices = level + np.cumsum(rng.normal(0.03, 0.4, n))
    c = pd.Series(prices, index=idx)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c,
                         "Volume": 1e6}, index=idx)


@pytest.fixture()
def warm(tmp_path, monkeypatch):
    """Offline store with a warm price feed: get_daily returns a real frame for
    every symbol, so scorecard / entry_gate / regime all compute populated
    values (no network). IV history is seeded so IV rank ranks."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _frame(seed=abs(hash(s)) % 50 + 1))
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    screening.clear_cache()
    # Seed >= _MIN_POINTS IV points so iv_rank returns a real number.
    monkeypatch.setattr(iv_history, "IV_HISTORY_PATH", str(tmp_path / "iv.json"))
    for i in range(30):
        iv_history.record("NVDA", 20 + i * 0.5, day=f"2025-01-{i + 1:02d}")
    return tmp_path


def _open(ticker="NVDA"):
    """Paper open: buy_leap then the first sell_short."""
    executor.execute({"action": "buy_leap", "ticker": ticker, "strike": 75,
                      "contracts": 5, "execution_price": 2400, "stock_price": 100,
                      "posture": "balanced", "override_reason": "fixture"})
    executor.execute({"action": "sell_short", "ticker": ticker, "strike": 110,
                      "contracts": 5, "premium_per_share": 0.9, "stock_price": 100,
                      "short_expiration": "2025-02-21", "dte": 5})


# ---------------------------------------------------------------------------
# R1 — completeness: every section present; every field value-or-null-with-reason
# ---------------------------------------------------------------------------
def test_snapshot_is_complete_on_open(warm):
    _open("NVDA")
    pos = log.find_position(log.load_state(), "NVDA")
    snap = pos["entry_context"]
    assert snap is not None

    # Versioned + timestamped + session flag.
    assert snap["snapshot_schema_version"] == config.SNAPSHOT_SCHEMA_VERSION
    assert snap["captured_at"].endswith("Z")
    assert snap["market_session"] in ("open", "closed", "unknown")

    # Every R1 section present.
    for section in ("scorecard", "regime", "sector", "stock", "iv", "gates",
                    "execution_intent", "data_quality"):
        assert section in snap, section

    # Scorecard verdict + metric block; regime/sector/stock/iv blocks present.
    assert "verdict" in snap["scorecard"] and "metrics" in snap["scorecard"]
    assert {"status", "breadth", "vix"} <= set(snap["regime"])
    assert {"etf", "rs3m_vs_spy", "breadth"} <= set(snap["sector"])
    assert {"rs3m_vs_spy", "rs3m_vs_sector", "atr_pct", "atr_value", "rsi",
            "price"} <= set(snap["stock"])
    # R2: the snapshot records WHICH rs-vs-sector variant gated the entry (v3
    # additive field) — always the direct rs3m(stock, sector_etf) now, so a future
    # variant change can never be silent.
    assert snap["stock"]["rs3m_vs_sector_method"] == "direct"
    assert config.SNAPSHOT_SCHEMA_VERSION >= 3
    assert {"iv_rank", "iv_percentile"} <= set(snap["iv"])

    # Gates: entry-gate levels 1-4 + account-gate checks + typed override.
    levels = snap["gates"]["entry_gate"]["levels"]
    assert [lv["level"] for lv in levels] == [1, 2, 3, 4]
    assert snap["gates"]["account_gate"]["checks"]  # per-check detail present
    assert snap["gates"]["override"]["reason"] == "fixture"  # typed override logged

    # Execution intent from the payload.
    assert snap["execution_intent"]["leap_strike"] == 75
    assert snap["execution_intent"]["posture"] == "balanced"

    # EVERY tracked field is value-or-null-with-reason (R4 contract).
    missing = {m["field"]: m["missing_reason"] for m in snap["data_quality"]["missing"]}
    for path in entry_context._TRACKED_FIELDS:
        section, key = path.split(".")
        value = snap[section][key]
        assert value is not None or path in missing, f"{path} is null without a reason"

    # Warm feed -> most fields populated -> under the null-alert threshold.
    assert snap["data_quality"]["null_field_fraction"] <= config.SNAPSHOT_NULL_FIELD_ALERT_FRACTION
    assert snap["stock"]["rsi"] is not None and snap["iv"]["iv_rank"] is not None


def test_snapshot_frozen_onto_the_execution(warm):
    _open("NVDA")
    execs = log.load_state()["executions"]
    buy = next(e for e in execs if e["action"] == "buy_leap")
    # The immutable execution IS the source of truth for the snapshot.
    assert buy["entry_context"] is not None
    assert buy["entry_context"]["snapshot_schema_version"] == config.SNAPSHOT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# R2 — immutability: a full derived rebuild leaves the snapshot byte-identical
# ---------------------------------------------------------------------------
def test_recompute_derived_leaves_snapshot_byte_identical(warm):
    _open("NVDA")
    state = log.load_state()
    pos_before = json.dumps(log.find_position(state, "NVDA")["entry_context"], sort_keys=True)
    buy_before = json.dumps(
        next(e for e in state["executions"] if e["action"] == "buy_leap")["entry_context"],
        sort_keys=True)

    # Rebuild the derived ledgers from scratch, twice — idempotent + opaque.
    log.recompute_derived(state)
    log.recompute_derived(state)

    pos_after = json.dumps(log.find_position(state, "NVDA")["entry_context"], sort_keys=True)
    buy_after = json.dumps(
        next(e for e in state["executions"] if e["action"] == "buy_leap")["entry_context"],
        sort_keys=True)
    assert pos_after == pos_before, "recompute_derived mutated the position snapshot"
    assert buy_after == buy_before, "recompute_derived mutated the execution snapshot"


def test_capture_never_raises_and_never_blocks():
    # Even with nothing warmed and a bogus ticker, capture returns a snapshot
    # (best-effort) rather than raising into the execution path.
    snap = entry_context.capture("ZZZZ", payload={"strike": 1})
    assert snap["snapshot_schema_version"] == config.SNAPSHOT_SCHEMA_VERSION
    assert "data_quality" in snap


# ---------------------------------------------------------------------------
# R4 — missing-data policy: stale cache -> null + reason "stale"; >25% null -> alert
# ---------------------------------------------------------------------------
def test_stale_bars_null_the_market_fields_with_reason(warm, monkeypatch):
    import data_cache
    from market_scheduler import BARS, Tier
    # Record a bars fetch far in the past so it is stale beyond its max age.
    old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    data_cache.reset()
    data_cache.put("NVDA", BARS, True, provider="test", tier=Tier.T3, fetched_at=old)

    snap = entry_context.capture("NVDA", payload={"strike": 75})
    assert snap["data_quality"]["bars_stale"] is True
    reasons = {m["field"]: m["missing_reason"] for m in snap["data_quality"]["missing"]}
    # Market-derived scalars are nulled with reason "stale" (not computed off aged bars).
    assert snap["stock"]["rs3m_vs_spy"] is None
    assert reasons["stock.rs3m_vs_spy"] == "stale"
    assert reasons["regime.status"] == "stale"
    data_cache.reset()


def test_over_null_threshold_fires_low_alert_and_still_logs(tmp_path, monkeypatch):
    # Cold store (no warm feed): most tracked fields come back null -> the
    # >25% data-quality alert fires, but the execution is STILL logged.
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: None)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)
    screening.clear_cache()

    res = executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 75,
                            "contracts": 5, "execution_price": 2400, "stock_price": 100,
                            "override_reason": "fixture"})
    assert res["status"] == "filled"  # execution still logged (never blocked)

    state = log.load_state()
    snap = log.find_position(state, "NVDA")["entry_context"]
    assert snap["data_quality"]["over_null_threshold"] is True
    assert snap["data_quality"]["null_field_fraction"] > config.SNAPSHOT_NULL_FIELD_ALERT_FRACTION
    fired = [a for a in state["alerts"]["log"] if a["type"] == "SNAPSHOT_DATA_QUALITY"]
    assert fired and fired[-1]["severity"] == "LOW" and fired[-1]["ticker"] == "NVDA"


# ---------------------------------------------------------------------------
# Exit-time counterpart metrics (R3) — same stock-level set as entry.
# ---------------------------------------------------------------------------
def test_exit_metrics_mirror_entry_stock_block(warm):
    m = entry_context.exit_metrics("NVDA")
    assert {"rs3m_vs_spy", "rs3m_vs_sector", "atr_pct", "atr_value", "rsi",
            "pct_above_ma21", "price", "captured_at"} <= set(m)


def test_summary_is_compact_digest(warm):
    _open("NVDA")
    snap = log.find_position(log.load_state(), "NVDA")["entry_context"]
    s = entry_context.summary(snap)
    assert set(s) == {"verdict", "regime", "iv_rank", "rs3m_vs_spy", "rs3m_vs_sector"}
