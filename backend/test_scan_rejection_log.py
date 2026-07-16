"""Scan rejection-reason log — binding-constraint extraction, append-only per-day
records, and the calibration summary. Pure storage under a temp DATA_DIR."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-rejlog-test-"))

import scan_rejection_log as srl  # noqa: E402


def _row(ticker, verdict, reasons, **extra):
    return {"ticker": ticker, "verdict": verdict, "verdict_reasons": reasons, **extra}


# ---------------------------------------------------------------------------
# Binding constraint = a READ of the first genuine (worst) reason
# ---------------------------------------------------------------------------
def test_binding_constraint_is_first_reason():
    assert srl.binding_constraint(_row("A", "WATCH", ["symbol:WATCH"])) == "symbol:WATCH"
    assert srl.binding_constraint(_row("B", "BLOCKED", ["structure:BLOCKED", "regime:BLOCKED"])) \
        == "structure:BLOCKED"


def test_binding_constraint_none_for_ready():
    assert srl.binding_constraint(_row("A", "READY", [])) is None


def test_binding_constraint_skips_rs_annotation():
    # The informational RS TURNING annotation is appended after the real inputs and
    # must not be reported as the binding constraint.
    row = _row("A", "WATCH", ["symbol:WATCH", "rs:TURNING (vs sector recovering — watch)"])
    assert srl.binding_constraint(row) == "symbol:WATCH"


# ---------------------------------------------------------------------------
# record_scan — append-only, idempotent per day
# ---------------------------------------------------------------------------
def test_record_scan_persists_calibration_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    rows = [_row("NVDA", "WATCH", ["symbol:WATCH"], score=6.1,
                 score_parts={"inst_flow": 0.6}, rs_state="TURNING", rs_level=-5.0,
                 rs_slope=0.9, net_juice_weekly_pct=2.1, base_stage="EARLY_ADVANCE",
                 inst_flow="EARLY_INTEREST", sym="yellow", sector_rs1m=1.2, iv_rank=40.0)]
    out = srl.record_scan(rows, day="2026-07-16")
    assert out["ok"] and out["recorded"] == 1
    rec = srl.series("NVDA")[-1]
    assert rec["binding_constraint"] == "symbol:WATCH"
    assert rec["score"] == 6.1 and rec["rs_state"] == "TURNING" and rec["rs_level"] == -5.0
    assert rec["base_stage"] == "EARLY_ADVANCE" and rec["sector_rs1m"] == 1.2


def test_record_scan_idempotent_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    srl.record_scan([_row("AAA", "BLOCKED", ["structure:BLOCKED"], score=1.0)], day="2026-07-16")
    srl.record_scan([_row("AAA", "READY", [], score=8.0)], day="2026-07-16")   # same day, rewrites
    srl.record_scan([_row("AAA", "WATCH", ["symbol:WATCH"], score=5.0)], day="2026-07-17")
    recs = srl.series("AAA")
    assert len(recs) == 2                              # two distinct days, not three
    assert recs[0]["verdict"] == "READY"              # last write of 07-16 won
    assert recs[1]["date"] == "2026-07-17"


def test_record_scan_skips_rows_without_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    out = srl.record_scan([{"verdict": "READY"}, _row("BBB", "READY", [])], day="2026-07-16")
    assert out["recorded"] == 1


# ---------------------------------------------------------------------------
# summary — the "is the gate too strict" rollup
# ---------------------------------------------------------------------------
def test_summary_counts_binding_and_ready_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    srl.record_scan([
        _row("A", "READY", []),
        _row("B", "WATCH", ["symbol:WATCH"]),
        _row("C", "BLOCKED", ["structure:BLOCKED"]),
        _row("D", "WATCH", ["symbol:WATCH"]),
    ], day="2026-07-16")
    s = srl.summary()
    assert s["records"] == 4
    assert s["verdict_counts"]["WATCH"] == 2
    assert s["binding_counts"]["symbol:WATCH"] == 2
    assert s["ready_rate"] == 25.0
