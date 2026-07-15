"""LEAP cost-basis mis-scale guard + one-click repair.

A LEAP whose cost_basis is stored PER SHARE (e.g. 53.05 where 5305 was meant)
makes the intrinsic-vs-cost orange read absurdly (e.g. 8,584%). These tests pin
the detection shape and the surgical ×100 repair (shorts untouched).
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402
import position_manager as pm  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


# ---- detection ---------------------------------------------------------------
def test_suspect_via_entry_intrinsic_no_quote():
    # Paid 53.05/contract for a 137.5 call bought at 185 — impossible: intrinsic
    # at entry alone is (185-137.5)*100 = 4750. Flagged with no live quote.
    leg = {"strike": 137.5, "contracts": 1, "cost_basis": 53.05, "entry_stock_price": 185}
    assert pm.leap_cost_suspect(leg, None) is True


def test_suspect_via_live_intrinsic():
    # No entry price, but the live intrinsic dwarfs the recorded cost by ~100×.
    leg = {"strike": 135, "contracts": 1, "cost_basis": 56.80}
    assert pm.leap_cost_suspect(leg, stock_price=229) is True


def test_healthy_leap_not_flagged():
    # Correctly stored per-contract-total cost — not flagged, even deep ITM.
    leg = {"strike": 137.5, "contracts": 1, "cost_basis": 5305, "entry_stock_price": 185}
    assert pm.leap_cost_suspect(leg, stock_price=232) is False


def test_appreciated_cheap_leap_not_flagged():
    # A genuinely cheap LEAP that appreciated ~8× is NOT a mis-scale.
    leg = {"strike": 100, "contracts": 1, "cost_basis": 800}  # $8/sh
    assert pm.leap_cost_suspect(leg, stock_price=163) is False  # intrinsic 6300 < 800*20


# ---- repair ------------------------------------------------------------------
def _seed_position(monkeypatch, stock=None):
    # Two per-share-scaled LEAP legs + one short, mirroring the reported XLK card.
    monkeypatch.setattr(pm, "_stock_price", lambda t: stock)
    state = log.load_state()
    state["positions"] = [{
        "ticker": "XLK", "status": "open",
        "leap_legs": [
            {"strike": 137.5, "contracts": 1, "cost_basis": 53.05, "current_bid": 53.05,
             "extrinsic_at_entry": 5.05, "extrinsic": 5.05, "entry_stock_price": 185,
             "expiration": "2027-01-15"},
            {"strike": 135, "contracts": 1, "cost_basis": 56.80, "current_bid": 56.80,
             "extrinsic_at_entry": 6.80, "extrinsic": 6.80, "entry_stock_price": 191.8,
             "expiration": "2027-01-15"},
        ],
        "short_calls": [
            {"strike": 179, "contracts": 1, "expiration": "2026-07-17", "open_date": "2026-07-09",
             "entry_premium_total": 600.0, "entry_extrinsic_per_share": 1.57,
             "entry_stock_price": 183, "current_bid": 5.0},
        ],
    }]
    log.save_state(state)


def test_repair_multiplies_cost_and_preserves_shorts(isolated_state, monkeypatch):
    _seed_position(monkeypatch, stock=232)
    r = executor.repair_leap_cost_scale("XLK")
    assert r["status"] == "repaired"
    assert len(r["fixed"]) == 2

    p = log.find_position(log.load_state(), "XLK")
    legs = {l["strike"]: l for l in log.leap_legs(p)}
    assert legs[137.5]["cost_basis"] == 5305.0
    assert legs[135]["cost_basis"] == 5680.0
    # Extrinsic recomputed from corrected cost + entry price (cost − intrinsic×100).
    # 5305 − (185-137.5)*100 = 5305 − 4750 = 555 per contract.
    assert legs[137.5]["extrinsic_at_entry"] == pytest.approx(555, abs=1)

    # Short leg is untouched — DTE/decay history preserved.
    sc = p["short_calls"][0]
    assert sc["open_date"] == "2026-07-09"
    assert sc["entry_premium_total"] == 600.0

    # The orange % is now sane: intrinsic (9430+9700-ish) / cost (10985) ~ under 200%.
    view = pm.enrich_position(p) if hasattr(pm, "enrich_position") else None
    if view is not None:
        assert view["leap_totals"]["cost_basis"] == pytest.approx(10985, abs=1)
        assert view["leap_totals"]["cost_basis_suspect"] is False


def test_repair_is_noop_when_healthy(isolated_state, monkeypatch):
    monkeypatch.setattr(pm, "_stock_price", lambda t: 232)
    state = log.load_state()
    state["positions"] = [{
        "ticker": "XLK", "status": "open",
        "leap_legs": [{"strike": 137.5, "contracts": 1, "cost_basis": 5305, "current_bid": 5305,
                       "extrinsic_at_entry": 555, "entry_stock_price": 185, "expiration": "2027-01-15"}],
        "short_calls": [],
    }]
    log.save_state(state)
    r = executor.repair_leap_cost_scale("XLK")
    assert r["status"] == "noop"
    assert r["fixed"] == []
    # Unchanged.
    p = log.find_position(log.load_state(), "XLK")
    assert log.leap_legs(p)[0]["cost_basis"] == 5305
