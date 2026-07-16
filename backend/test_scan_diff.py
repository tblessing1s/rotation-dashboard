"""Tests for the daily scan transition diff (pure) — the pipeline events."""
from __future__ import annotations

import scan_diff as sd


def _rec(ticker, **kw):
    return {"ticker": ticker, "verdict": "WATCH", "bench": False,
            "base_stage": "EARLY_ADVANCE", "inst_flow": "ACCUMULATING",
            "rs_state": "RISING", "sector": "XLK", **kw}


def test_bench_to_ready_is_the_headline():
    prev = {"NVDA": _rec("NVDA", verdict="WATCH", bench=True)}
    today = {"NVDA": _rec("NVDA", verdict="READY", bench=False)}
    evs = sd.diff(prev, today)
    assert [e["type"] for e in evs] == [sd.BENCH_READY]
    assert evs[0]["ticker"] == "NVDA"


def test_fresh_ready_when_not_previously_bench():
    prev = {"AAPL": _rec("AAPL", verdict="WATCH", bench=False)}
    today = {"AAPL": _rec("AAPL", verdict="READY")}
    evs = sd.diff(prev, today)
    assert [e["type"] for e in evs] == [sd.NEW_READY]


def test_watch_to_bench_is_pipeline_progress():
    prev = {"NVDA": _rec("NVDA", verdict="WATCH", bench=False)}       # WATCH intake
    today = {"NVDA": _rec("NVDA", verdict="WATCH", bench=True,        # advanced to bench
                          path_to_ready="pull back within 1 ATR of MA21", eligible_days=6)}
    evs = sd.diff(prev, today)
    assert [e["type"] for e in evs] == [sd.WATCH_BENCH]
    assert evs[0]["data"]["eligible_days"] == 6


def test_bench_to_ready_beats_watch_bench():
    # A bench name that went straight to READY fires BENCH_READY, not WATCH_BENCH.
    prev = {"NVDA": _rec("NVDA", verdict="WATCH", bench=True)}
    today = {"NVDA": _rec("NVDA", verdict="READY", bench=False)}
    assert [e["type"] for e in sd.diff(prev, today)] == [sd.BENCH_READY]


def test_no_event_when_already_ready():
    prev = {"AAPL": _rec("AAPL", verdict="READY")}
    today = {"AAPL": _rec("AAPL", verdict="READY")}
    assert sd.diff(prev, today) == []


def test_degradations_fire_only_for_watched_names():
    prev = {"X": _rec("X", verdict="WATCH", base_stage="EARLY_ADVANCE",
                      inst_flow="ACCUMULATING", rs_state="RISING")}
    today = {"X": _rec("X", verdict="WATCH", base_stage="TOPPING",
                       inst_flow="DISTRIBUTING", rs_state="FALLING")}
    types = [e["type"] for e in sd.diff(prev, today)]
    assert types.count(sd.DEGRADED) == 3          # base, inst, rs all rolled over

    # A BLOCKED name yesterday is not "watched" — no degrade spam.
    prevb = {"X": _rec("X", verdict="BLOCKED", base_stage="DECLINING",
                       inst_flow="ACCUMULATING", rs_state="RISING")}
    todayb = {"X": _rec("X", verdict="BLOCKED", base_stage="DECLINING",
                        inst_flow="DISTRIBUTING", rs_state="FALLING")}
    assert sd.diff(prevb, todayb) == []


def test_pipeline_entrant_new_basing_early_interest():
    prev = {"Z": _rec("Z", base_stage="INSUFFICIENT_DATA", inst_flow="NO_INTEREST")}
    today = {"Z": _rec("Z", verdict="WATCH", base_stage="BASING", inst_flow="EARLY_INTEREST")}
    assert any(e["type"] == sd.PIPELINE_ENTRANT for e in sd.diff(prev, today))
    # Not re-fired when it was already an entrant yesterday.
    prev2 = {"Z": _rec("Z", base_stage="BASING", inst_flow="EARLY_INTEREST")}
    assert not any(e["type"] == sd.PIPELINE_ENTRANT for e in sd.diff(prev2, today))


def test_unseen_symbol_can_only_fire_ready_or_entrant():
    today = {"NEW": _rec("NEW", verdict="READY")}
    evs = sd.diff({}, today)
    assert [e["type"] for e in evs] == [sd.NEW_READY]   # nothing to degrade from


def test_sector_slot_opens_with_a_waiting_name():
    today = {"NVDA": _rec("NVDA", verdict="READY", sector="XLK"),
             "JPM": _rec("JPM", verdict="BLOCKED", sector="XLF")}
    evs = sd.diff({}, today, prev_occupied=["XLK", "XLF"], occupied_now=["XLF"])
    slot = [e for e in evs if e["type"] == sd.SECTOR_SLOT_OPEN]
    assert len(slot) == 1 and slot[0]["data"]["sector"] == "XLK"
    assert "NVDA" in slot[0]["data"]["candidates"]


def test_sector_slot_no_event_without_a_waiting_name():
    today = {"JPM": _rec("JPM", verdict="BLOCKED", sector="XLF")}
    evs = sd.diff({}, today, prev_occupied=["XLF"], occupied_now=[])
    assert not any(e["type"] == sd.SECTOR_SLOT_OPEN for e in evs)
