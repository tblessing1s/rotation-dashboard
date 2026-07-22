"""Shares-primary migration (schema v20) — offline, fixture-driven coverage.

Exercises the surface the audit (AUDIT_SHARES_PRIMARY_MIGRATION_PHASE0.md) calls
for: the full SHARES lifecycle, burn/payback provably absent for SHARES, the
round-lot SIZE-BLOCK + fragment rule, the ex-div entry gate, the dividend income
ledger, the CALLED_AWAY exit, append-only history corrections + determinism, the
new economic reconcile diffs + persisted acks, and the untouched verdict engine.

Run: python -m pytest backend/test_shares_migration.py -q
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import account_gate  # noqa: E402
import config  # noqa: E402
import executor  # noqa: E402
import exit_reasons  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import position_manager as pm  # noqa: E402
import position_types  # noqa: E402
import reconcile  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    monkeypatch.setattr(executor, "live_enabled", lambda: False)  # paper path
    return tmp_path


def _buy_shares(ticker, qty, price):
    return executor.execute({"action": "buy_shares", "ticker": ticker, "qty": qty,
                             "price_per_share": price, "stock_price": price})


def _sell_short(ticker, strike, contracts, prem, spot, exp="2026-08-21"):
    return executor.execute({"action": "sell_short", "ticker": ticker, "strike": strike,
                             "contracts": contracts, "premium_per_share": prem,
                             "stock_price": spot, "expiration": exp})


# ---- Schema v19 -> v20 -------------------------------------------------------
def test_schema_v19_to_v20_backfills_legacy_and_lot_record():
    v19 = {
        "schema_version": 19,
        "positions": [{"ticker": "AAPL", "leap": {"strike": 100, "contracts": 1},
                       "shares": {"count": 0, "cost_basis_per_share": None}}],
        "executions": [{"id": "exec_001", "action": "buy_leap", "ticker": "AAPL"}],
    }
    before = list(v19["executions"])
    out, changed = migrations.migrate(v19)
    assert changed and out["schema_version"] == 20 == migrations.CURRENT_VERSION
    p = out["positions"][0]
    # Legacy positions are backfilled to LEAP_PMCC_LEGACY (read-only).
    assert p["position_type"] == position_types.LEAP_PMCC_LEGACY
    assert position_types.is_legacy(p) and not position_types.is_shares(p)
    # Shares record gains the append-only lot log; executions untouched.
    assert p["shares"]["acquisition_records"] == []
    assert out["executions"] == before  # ADD only — no rewrite


def test_absent_position_type_degrades_to_legacy():
    # A position with no discriminator (never migrated) must read as LEGACY so
    # burn/payback removal never leaks into it.
    assert position_types.of({}) == position_types.LEAP_PMCC_LEGACY
    assert position_types.of({"position_type": None}) == position_types.LEAP_PMCC_LEGACY


# ---- SHARES lifecycle --------------------------------------------------------
def test_buy_shares_creates_shares_position_lot_aware(store):
    _buy_shares("KO", 200, 60.0)
    _buy_shares("KO", 100, 63.0)  # scale-in -> weighted cost basis
    st = log.load_state()
    p = log.find_position(st, "KO")
    assert p["position_type"] == position_types.SHARES
    assert p["shares"]["count"] == 300
    assert p["shares"]["cost_basis_per_share"] == round((200 * 60 + 100 * 63) / 300, 4)
    assert len(p["shares"]["acquisition_records"]) == 2
    assert p["shares"]["acquisition_records"][0]["execution_id"] == "exec_001"


def test_shares_full_lifecycle_called_away(store):
    _buy_shares("KO", 200, 60.0)
    _sell_short("KO", 62.0, 2, 1.0, 60.0)
    res = executor.execute({"action": "close_shares_assigned", "ticker": "KO",
                            "strike": 62.0, "contracts": 2, "stock_price": 63.0})
    assert res["status"] == "filled"
    # proceeds strike*shares = 62*200; realized vs 60 cost basis = +400.
    assert res["realized_pnl"] == 400.0
    st = log.load_state()
    p = log.find_position(st, "KO")
    assert p["shares"]["count"] == 0 and p["status"] == "closed" and not p["short_calls"]
    ca = next(e for e in st["executions"] if e["action"] == "close_shares_assigned")
    assert ca["exit_reason"] == exit_reasons.ExitReason.CALLED_AWAY
    assert ca["proceeds"] == 12400.0
    # The assigned short's full extrinsic is realized juice (booked, kept premium).
    assert st["theta_ledger"]["totals"]["ytd"] > 0


def test_sell_shares_books_realized_pnl(store):
    _buy_shares("KO", 100, 50.0)
    res = executor.execute({"action": "sell_shares", "ticker": "KO", "qty": 100,
                            "price_per_share": 55.0, "stock_price": 55.0})
    st = log.load_state()
    sell = next(e for e in st["executions"] if e["action"] == "sell_shares")
    assert sell["realized_pnl"] == 500.0
    assert log.find_position(st, "KO")["shares"]["count"] == 0


# ---- Burn / payback provably absent for SHARES -------------------------------
def test_shares_have_no_payback_meter_but_legacy_do(store):
    _buy_shares("KO", 100, 60.0)                    # SHARES
    executor.execute({"action": "buy_leap", "ticker": "MSFT", "strike": 300,
                      "contracts": 1, "execution_price": 5000, "stock_price": 350,
                      "extrinsic_captured": 500,
                      "override_reason": "test fixture — legacy LEAP"})   # LEGACY
    st = log.load_state()
    pb = st["extrinsic_payback"]
    assert "KO" not in pb          # SHARES: no phantom zero-denominator hurdle
    assert "MSFT" in pb            # legacy LEAP keeps its payback meter


# ---- Covered-lot floor + fragment (HARD_CFM_RULE) ----------------------------
def test_covered_lots_fragment_never_rounds_up():
    assert pm.covered_lots(150) == {"shares": 150, "coverable_lots": 1,
                                    "fragment_shares": 50, "has_fragment": True}
    assert pm.covered_lots(200) == {"shares": 200, "coverable_lots": 2,
                                    "fragment_shares": 0, "has_fragment": False}
    assert pm.covered_lots(99)["coverable_lots"] == 0  # a sub-lot is never coverable


def test_delta_coverage_shares_flags_naked_short(store):
    # 100 shares (1 coverable lot) but 2 short contracts -> naked/inverted.
    pos = {"position_type": position_types.SHARES, "ticker": "KO",
           "shares": {"count": 100}, "short_calls": [{"strike": 62, "contracts": 2}]}
    cov = pm.delta_coverage(pos, price=60.0)
    assert cov["coverable_lots"] == 1 and cov["short_contracts"] == 2
    assert cov["naked_short"] is True and cov["inverted"] is True
    assert cov["floor_breach"] is False  # delta 1.0 — LEAP floor unreachable
    # Fully covered: 2 lots, 2 shorts -> not naked.
    ok = pm.delta_coverage({"position_type": position_types.SHARES, "shares": {"count": 200},
                            "short_calls": [{"strike": 62, "contracts": 2}]}, price=60.0)
    assert ok["naked_short"] is False


# ---- Round-lot SIZE-BLOCK ----------------------------------------------------
def _est(spot):
    return {"ticker": "X", "stock_price": spot, "weekly_extrinsic_per_share": 1.0,
            "leap_strike": spot * 0.9, "leap_cost_per_share": spot * 0.5,
            "weekly_yield_pct": 2.0, "source": "estimate"}


def test_round_lot_size_block(store, monkeypatch):
    monkeypatch.setattr(config, "PER_POSITION_CAP_USD", 15000.0)
    monkeypatch.setattr(account_gate, "juice_estimate", lambda t, df=None: _est(200.0))
    g = account_gate.evaluate("HIGH", contracts=1, leap_cost_per_share=100.0,
                              weekly_extrinsic_per_share=3.0,
                              position_type=position_types.SHARES)
    chk = next(c for c in g["checks"] if c["id"] == "round_lot_size")
    assert chk["pass"] is False and chk["detail"]["size_blocked"] is True  # 200*100 > 15000
    assert "round_lot_size" in g["blocking_failures"]


def test_round_lot_size_block_passes_cheap_lot(store, monkeypatch):
    monkeypatch.setattr(config, "PER_POSITION_CAP_USD", 15000.0)
    monkeypatch.setattr(account_gate, "juice_estimate", lambda t, df=None: _est(60.0))
    g = account_gate.evaluate("CHEAP", contracts=1, leap_cost_per_share=30.0,
                              weekly_extrinsic_per_share=1.0,
                              position_type=position_types.SHARES)
    chk = next(c for c in g["checks"] if c["id"] == "round_lot_size")
    assert chk["pass"] is True  # 60*100 = 6000 <= 15000
    # A legacy (default) caller gets no round_lot_size check at all.
    g2 = account_gate.evaluate("CHEAP", contracts=1, leap_cost_per_share=30.0,
                               weekly_extrinsic_per_share=1.0)
    assert not any(c["id"] == "round_lot_size" for c in g2["checks"])


# ---- Ex-dividend entry gate --------------------------------------------------
def test_ex_div_in_cycle_warns(store, monkeypatch):
    import dividends
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=10)).isoformat()
    monkeypatch.setattr(dividends, "next_dividend", lambda t: {"ex_date": soon, "amount": 0.5})
    monkeypatch.setattr(account_gate, "juice_estimate", lambda t, df=None: _est(60.0))
    g = account_gate.evaluate("KO", contracts=1, leap_cost_per_share=30.0,
                              weekly_extrinsic_per_share=1.0)
    chk = next(c for c in g["checks"] if c["id"] == "ex_div_in_cycle")
    assert chk["pass"] is False and not chk["blocking"]      # WARN, not a hard block
    assert "ex_div_in_cycle" in g["warnings"]


def test_ex_div_outside_cycle_passes(store, monkeypatch):
    import dividends
    from datetime import date, timedelta
    far = (date.today() + timedelta(days=200)).isoformat()
    monkeypatch.setattr(dividends, "next_dividend", lambda t: {"ex_date": far, "amount": 0.5})
    monkeypatch.setattr(account_gate, "juice_estimate", lambda t, df=None: _est(60.0))
    g = account_gate.evaluate("KO", contracts=1, leap_cost_per_share=30.0,
                              weekly_extrinsic_per_share=1.0)
    assert next(c for c in g["checks"] if c["id"] == "ex_div_in_cycle")["pass"] is True


# ---- Dividend income ledger --------------------------------------------------
def test_dividend_income_is_its_own_ledger_not_juice(store):
    _buy_shares("KO", 200, 60.0)
    executor.execute({"action": "dividend_income", "ticker": "KO", "per_share": 0.48,
                      "pay_date": "2026-07-15"})
    st = log.load_state()
    dl = st["dividend_ledger"]
    assert dl["by_ticker"]["KO"] == 96.0 and dl["total"] == 96.0
    assert dl["by_month"]["2026-07"] == 96.0
    # Dividend income never contaminates the juice/theta ledger.
    assert st["theta_ledger"]["totals"]["ytd"] == 0


# ---- CALLED_AWAY exit reason -------------------------------------------------
def test_called_away_is_note_free_and_valid():
    ca = exit_reasons.ExitReason.CALLED_AWAY
    assert exit_reasons.is_close_time(ca) and exit_reasons.is_valid(ca)
    assert not exit_reasons.requires_note(ca)
    assert ca in exit_reasons.AUTOMATED and ca in exit_reasons.OPERATOR_SELECTABLE


# ---- Assignment mechanics flip for shares ------------------------------------
def test_assignment_note_shares_vs_legacy():
    sc = {"strike": 60, "contracts": 1, "dte": 3, "current_bid": 0.01,
          "entry_premium_total": 100.0, "expiration": "2026-08-21"}
    shares_note = pm.enrich_short(sc, 62.0, None, position_type=position_types.SHARES)
    legacy_note = pm.enrich_short(sc, 62.0, None, position_type=position_types.LEAP_PMCC_LEGACY)
    assert shares_note["assignment_risk"] and legacy_note["assignment_risk"]
    assert "REAL SHARES" in shares_note["assignment_risk"]["note"]
    assert "never exercise the LEAP" in legacy_note["assignment_risk"]["note"]


# ---- Append-only history correction + determinism ----------------------------
def test_history_edit_is_append_only_and_deterministic(store):
    _buy_shares("KO", 200, 60.0)
    _sell_short("KO", 62.0, 2, 1.0, 60.0)
    close = executor.execute({"action": "close_short", "ticker": "KO", "strike": 62.0,
                              "contracts": 2, "close_price_per_share": 0.20,
                              "stock_price": 60.0})
    cid = close["execution_id"]
    st_before = log.load_state()
    ytd_before = st_before["theta_ledger"]["totals"]["ytd"]

    executor.save_transactions([{"id": cid, "price": 0.10}], ticker="KO")
    st = log.load_state()
    # Original immutable; a txn_correction was appended.
    orig = next(e for e in st["executions"] if e["id"] == cid)
    assert orig["close_price_per_share"] == 0.20
    corr = next(e for e in st["executions"]
                if e.get("action") == "txn_correction" and e.get("corrects") == cid)
    assert corr["changes"]["close_price_per_share"] == 0.10
    # The corrected view drives the ledger (paid less back -> more net juice).
    assert st["theta_ledger"]["totals"]["ytd"] > ytd_before

    # Determinism: replay the derived ledgers from genesis -> identical.
    replay = {**st}
    log.recompute_derived(replay)
    assert replay["theta_ledger"] == st["theta_ledger"]
    assert replay["dividend_ledger"] == st["dividend_ledger"]


# ---- Reconcile: economic diffs + persisted acks ------------------------------
def _inst(cost_basis=None):
    return reconcile._instrument("KO", "KO", reconcile.EQUITY, None, None, None,
                                 200, cost_basis=cost_basis)


def test_economic_cost_basis_mismatch_when_qty_matches():
    exp = [reconcile._instrument("KO", "KO", reconcile.EQUITY, None, None, None, 200,
                                 cost_basis=12000.0)]
    brk = [reconcile._instrument("KO", "KO", reconcile.EQUITY, None, None, None, 200,
                                 cost_basis=12500.0)]
    rep = reconcile.reconcile(brk, exp, as_of="2026-07-21T00:00:00Z")
    d = next(d for d in rep["diffs"])
    assert d["classification"] == reconcile.COST_BASIS_MISMATCH
    assert d["expected_value"] == 12000.0 and d["broker_value"] == 12500.0
    # No economic field on the broker side -> no fabricated divergence.
    brk2 = [reconcile._instrument("KO", "KO", reconcile.EQUITY, None, None, None, 200)]
    assert reconcile.reconcile(brk2, exp, as_of="x")["diffs"] == []


def test_stable_id_persists_ack_across_runs(store):
    exp = [reconcile._instrument("KO", "KO", reconcile.EQUITY, None, None, None, 200,
                                 cost_basis=12000.0)]
    brk = [reconcile._instrument("KO", "KO", reconcile.EQUITY, None, None, None, 200,
                                 cost_basis=12500.0)]
    st = log.load_state()
    rep1 = reconcile.reconcile(brk, exp, as_of="2026-07-21T00:00:00Z")
    reconcile._persist_report(st, rep1)
    d = rep1["diffs"][0]
    reconcile.ack_diff(st, d["id"], "broker basis is correct; will correct state")
    # A fresh run re-ids diffs, but the ack (keyed to stable_id) is re-applied.
    rep2 = reconcile.reconcile(brk, exp, as_of="2026-07-22T00:00:00Z")
    reconcile._persist_report(st, rep2)
    d2 = st["reconciliation"]["last"]["diffs"][0]
    assert d2["resolution"]["status"] == "acknowledged"
    assert d2["resolution"].get("carried") is True


# ---- The verdict engine is provably untouched --------------------------------
def test_verdict_engine_unchanged_by_migration():
    import scan_verdict
    # XLK July-6 regression: TOPPING x ACCUMULATING -> BLOCKED, keyed off none of
    # the migrated surfaces. compose_verdict has no position_type/burn/leap term.
    v = scan_verdict.compose_verdict("GREEN", "GREEN", "TOPPING", "ACCUMULATING")
    assert v["verdict"] == "BLOCKED"
