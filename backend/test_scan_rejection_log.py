"""Scan rejection-reason log — binding-constraint extraction, append-per-scan-run
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
    assert srl.binding_constraint(_row("A", "ELIGIBLE", [])) is None


def test_binding_constraint_skips_rs_annotation():
    # The informational RS TURNING annotation is appended after the real inputs and
    # must not be reported as the binding constraint.
    row = _row("A", "WATCH", ["symbol:WATCH", "rs:TURNING (vs sector recovering — watch)"])
    assert srl.binding_constraint(row) == "symbol:WATCH"


# ---------------------------------------------------------------------------
# record_scan — append-only per scan RUN, idempotent within one run
# ---------------------------------------------------------------------------
def test_record_scan_persists_calibration_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    rows = [_row("NVDA", "WATCH", ["symbol:WATCH"], score=6.1,
                 score_parts={"inst_flow": 0.6},
                 net_juice_weekly_pct=2.1, base_stage="EARLY_ADVANCE",
                 inst_flow="EARLY_INTEREST", sym="yellow", sector_rs1m=1.2, iv_rank=40.0)]
    out = srl.record_scan(rows, day="2026-07-16")
    assert out["ok"] and out["recorded"] == 1
    rec = srl.series("NVDA")[-1]
    assert rec["binding_constraint"] == "symbol:WATCH"
    assert rec["base_stage"] == "EARLY_ADVANCE" and rec["sector_rs1m"] == 1.2
    # Schema 4 dropped the vs-SECTOR two-speed RS shadow
    # (docs/decision-2026-08-21-remove-sector-rs.md). `sector_rs1m` above is a
    # DIFFERENT figure — the sector ETF's own strength vs SPY — and stays.
    assert rec["schema"] == 4
    for gone in ("rs_state", "rs_level", "rs_slope"):
        assert gone not in rec


def test_record_scan_idempotent_within_one_run(tmp_path, monkeypatch):
    """Re-writing the SAME scan_id replaces that run's point — a retry of one
    partially-failed sweep must not double-count."""
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    srl.record_scan([_row("AAA", "BLOCKED", ["structure:BLOCKED"], score=1.0)],
                    day="2026-07-16", scan_id="run-1")
    srl.record_scan([_row("AAA", "ELIGIBLE", [], score=8.0)],
                    day="2026-07-16", scan_id="run-1")   # same RUN — replaces
    recs = srl.series("AAA")
    assert len(recs) == 1
    assert recs[0]["verdict"] == "ELIGIBLE" and recs[0]["scan_id"] == "run-1"


def test_record_scan_appends_per_run_and_never_rewrites(tmp_path, monkeypatch):
    """Schema 2: every scan RUN appends its own record and the log grows
    monotonically — a re-run on the same day never rewrites the earlier one."""
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    srl.record_scan([_row("AAA", "BLOCKED", ["structure:BLOCKED"], score=1.0)],
                    day="2026-07-16", scan_id="run-1")
    srl.record_scan([_row("AAA", "ELIGIBLE", [], score=8.0)],
                    day="2026-07-16", scan_id="run-2")   # same day, DIFFERENT run
    srl.record_scan([_row("AAA", "WATCH", ["symbol:WATCH"], score=5.0)],
                    day="2026-07-17", scan_id="run-3")
    recs = srl.series("AAA")
    assert len(recs) == 3                                     # nothing was rewritten
    assert [r["verdict"] for r in recs] == ["BLOCKED", "ELIGIBLE", "WATCH"]
    assert [r["scan_id"] for r in recs] == ["run-1", "run-2", "run-3"]
    assert all(r["schema"] == srl.SCHEMA_VERSION for r in recs)
    # An omitted scan_id is still its own run (microsecond identity), never a
    # silent overwrite of the last one.
    srl.record_scan([_row("AAA", "ELIGIBLE", [])], day="2026-07-17")
    assert len(srl.series("AAA")) == 4


def test_retention_trims_by_date_not_record_count(tmp_path, monkeypatch):
    """A day may now carry several runs, so retention counts DISTINCT DATES —
    a count-based trim would silently shorten the retained window."""
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    for day in ("2026-07-14", "2026-07-15", "2026-07-16"):
        for run in ("a", "b"):
            srl.record_scan([_row("AAA", "ELIGIBLE", [])], day=day, scan_id=f"{day}-{run}")
    srl.record_scan([_row("AAA", "ELIGIBLE", [])], day="2026-07-17",
                    scan_id="2026-07-17-a", max_days=2)
    recs = srl.series("AAA")
    assert sorted({r["date"] for r in recs}) == ["2026-07-16", "2026-07-17"]
    assert len(recs) == 3                                # 2 runs on the 16th + 1 on the 17th


def test_latest_before_still_returns_the_newest_prior_day(tmp_path, monkeypatch):
    """The transition diff reads yesterday's state; multiple same-day runs must
    not change which record that is."""
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    srl.record_scan([_row("AAA", "BLOCKED", ["structure:BLOCKED"])],
                    day="2026-07-16", scan_id="run-1")
    srl.record_scan([_row("AAA", "WATCH", ["symbol:WATCH"])],
                    day="2026-07-16", scan_id="run-2")
    srl.record_scan([_row("AAA", "ELIGIBLE", [])], day="2026-07-17", scan_id="run-3")
    prior = srl.latest_before("2026-07-17")
    assert prior["AAA"]["verdict"] == "WATCH"          # last run OF the prior day


def test_record_scan_skips_rows_without_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    out = srl.record_scan([{"verdict": "ELIGIBLE"}, _row("BBB", "ELIGIBLE", [])], day="2026-07-16")
    assert out["recorded"] == 1


# ---------------------------------------------------------------------------
# summary — the "is the gate too strict" rollup
# ---------------------------------------------------------------------------
def test_summary_counts_binding_and_eligible_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(srl, "LOG_PATH", str(tmp_path / "log.json"))
    srl.record_scan([
        _row("A", "ELIGIBLE", []),
        _row("B", "WATCH", ["symbol:WATCH"]),
        _row("C", "BLOCKED", ["structure:BLOCKED"]),
        _row("D", "WATCH", ["symbol:WATCH"]),
    ], day="2026-07-16")
    s = srl.summary()
    assert s["records"] == 4
    assert s["verdict_counts"]["WATCH"] == 2
    assert s["binding_counts"]["symbol:WATCH"] == 2
    assert s["eligible_rate"] == 25.0
