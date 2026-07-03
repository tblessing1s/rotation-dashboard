"""Position-reconciliation tests — state.json vs Schwab.

Offline, mocked Schwab responses. Covers the OCC symbol parser, the full
classification matrix (incl. the compound assignment scenario), the expiry
carve-out, freeze semantics, the resolution paths, failure isolation, the
paper/live split, and the v6->v7 migration.

Run with: python -m pytest backend/test_reconcile.py -q
"""
import glob
import json
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))
os.environ.setdefault("CFM_ALERTS_SCHEDULER", "0")
os.environ.setdefault("CFM_SKIP_STARTUP_CHECK", "1")

import backups  # noqa: E402
import config  # noqa: E402
import executor  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import reconcile  # noqa: E402
import schwab_api  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DEMO_STATE_PATH", str(tmp_path / "state.demo.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def opt(underlying, strike, expiry, qty, put_call=reconcile.CALL):
    return reconcile._instrument(None, underlying, reconcile.OPTION, put_call, strike, expiry, qty)


def eq(underlying, qty):
    return reconcile._instrument(None, underlying, reconcile.EQUITY, None, None, None, qty)


def _classes(report):
    return sorted(d["classification"] for d in report["diffs"])


# ---------------------------------------------------------------------------
# 1. Symbol parser
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("underlying,expiry,strike,call", [
    ("AAPL", "2026-01-17", 150.0, True),       # standard call
    ("ON", "2026-07-10", 139.5, True),         # short root, fractional strike
    ("SPY", "2027-12-17", 680.0, False),        # LEAP date, put
    ("NVDA", "2026-09-18", 1250.0, True),       # high strike, padding
    ("A", "2026-03-20", 25.0, True),            # 1-char root
])
def test_symbol_roundtrip(underlying, expiry, strike, call):
    sym = schwab_api.occ_option_symbol(underlying, expiry, strike, call=call)
    p = reconcile.parse_option_symbol(sym)
    assert p["underlying"] == underlying
    assert p["expiry"] == expiry
    assert abs(p["strike"] - strike) < 1e-9
    assert p["put_call"] == (reconcile.CALL if call else reconcile.PUT)


@pytest.mark.parametrize("bad", [
    "GARBAGE", "", "AAPL 240920C0025000", "AAPL  240920X00250000",
    "AAPL  241320C00250000",  # month 13
    None,
])
def test_symbol_malformed_raises(bad):
    with pytest.raises(reconcile.OptionSymbolParseError):
        reconcile.parse_option_symbol(bad)


# ---------------------------------------------------------------------------
# 2. Classification matrix
# ---------------------------------------------------------------------------
def test_match_produces_clean():
    exp = [opt("NVDA", 90, "2026-12-18", 5), eq("NVDA", 300)]
    brk = [opt("NVDA", 90, "2026-12-18", 5), eq("NVDA", 300)]
    r = reconcile.reconcile(brk, exp, "t")
    assert r["status"] == reconcile.CLEAN
    assert r["diffs"] == []


def test_missing_at_broker():
    exp = [opt("NVDA", 124, "2099-12-18", -5)]  # future expiry, not carved out
    r = reconcile.reconcile([], exp, "t")
    assert _classes(r) == [reconcile.MISSING_AT_BROKER]
    assert r["status"] == reconcile.DIRTY


def test_unexpected_at_broker_non_short_stock():
    # Long equity with no LEAP against it -> plain UNEXPECTED_AT_BROKER.
    brk = [eq("TSLA", 100)]
    r = reconcile.reconcile(brk, [], "t")
    assert _classes(r) == [reconcile.UNEXPECTED_AT_BROKER]


def test_quantity_mismatch():
    exp = [eq("NVDA", 300)]
    brk = [eq("NVDA", 200)]
    r = reconcile.reconcile(brk, exp, "t")
    d = r["diffs"][0]
    assert d["classification"] == reconcile.QUANTITY_MISMATCH
    assert d["expected_qty"] == 300 and d["broker_qty"] == 200


def test_short_stock_detected_highest_severity():
    exp = [opt("NVDA", 90, "2026-12-18", 5)]      # LEAP long call present
    brk = [opt("NVDA", 90, "2026-12-18", 5), eq("NVDA", -500)]
    r = reconcile.reconcile(brk, exp, "t")
    assert reconcile.SHORT_STOCK_DETECTED in _classes(r)
    d = next(x for x in r["diffs"] if x["classification"] == reconcile.SHORT_STOCK_DETECTED)
    assert d["broker_qty"] == -500 and d["ticker"] == "NVDA"


def test_short_stock_needs_a_leap_else_plain_unexpected():
    # Short equity but NO LEAP in state -> not the assignment signature.
    brk = [eq("NVDA", -500)]
    r = reconcile.reconcile(brk, [], "t")
    assert _classes(r) == [reconcile.UNEXPECTED_AT_BROKER]


def test_compound_assignment_scenario():
    """Short call missing + short stock present -> MISSING_AT_BROKER(assignment_
    suspected) paired with SHORT_STOCK_DETECTED."""
    exp = [opt("NVDA", 90, "2026-12-18", 5),          # LEAP (matches broker)
           opt("NVDA", 124, "2020-01-17", -5)]        # short, expired in the past
    brk = [opt("NVDA", 90, "2026-12-18", 5),          # LEAP still there
           eq("NVDA", -500)]                          # assignment created short stock
    # Close at/above strike on expiry -> assignment suspected.
    r = reconcile.reconcile(brk, exp, "t",
                            close_on_expiry=lambda tk, d: 130.0)
    classes = _classes(r)
    assert reconcile.SHORT_STOCK_DETECTED in classes
    missing = next(x for x in r["diffs"] if x["classification"] == reconcile.MISSING_AT_BROKER)
    assert missing["assignment_suspected"] is True


# ---------------------------------------------------------------------------
# 3. Expiry carve-out
# ---------------------------------------------------------------------------
def test_expiry_below_strike_is_benign():
    exp = [opt("NVDA", 124, "2020-01-17", -5)]
    r = reconcile.reconcile([], exp, "t", close_on_expiry=lambda tk, d: 100.0)
    assert _classes(r) == [reconcile.EXPIRED_WORTHLESS_PENDING]
    assert r["status"] == reconcile.CLEAN  # benign -> not dirty
    # And a one-click suggested resolution is offered.
    assert any(s["kind"] == "resolve_expiry" for s in r["suggested_resolutions"])


def test_expiry_at_or_above_strike_is_assignment_suspected():
    exp = [opt("NVDA", 124, "2020-01-17", -5)]
    r = reconcile.reconcile([], exp, "t", close_on_expiry=lambda tk, d: 124.0)
    d = r["diffs"][0]
    assert d["classification"] == reconcile.MISSING_AT_BROKER
    assert d["assignment_suspected"] is True
    assert r["status"] == reconcile.DIRTY


def test_expiry_missing_ohlcv_never_silently_benign():
    exp = [opt("NVDA", 124, "2020-01-17", -5)]
    r = reconcile.reconcile([], exp, "t", close_on_expiry=lambda tk, d: None)
    assert _classes(r) == [reconcile.MISSING_AT_BROKER]  # falls through, not benign
    assert r["status"] == reconcile.DIRTY


def test_expired_leap_never_benign():
    exp = [opt("NVDA", 90, "2020-01-17", 5)]  # long call (LEAP), expired
    r = reconcile.reconcile([], exp, "t", close_on_expiry=lambda tk, d: 50.0)
    assert _classes(r) == [reconcile.MISSING_AT_BROKER]


# ---------------------------------------------------------------------------
# 4. Freeze semantics
# ---------------------------------------------------------------------------
def _frozen_position(**over):
    p = {
        "ticker": "NVDA", "sector": "XLK", "status": "active",
        "needs_review": True,
        "review": {"summary": "short 124 missing at broker", "diff_ids": ["diff_001"]},
        "leap": {"strike": 90, "contracts": 5, "cost_basis": 12000.0,
                 "extrinsic_at_entry": 2000.0, "current_bid": 20000.0},
        "shares": {"count": 0, "cap": 500},
        "short_calls": [],
    }
    p.update(over)
    return p


def _save(store, position):
    state = log.load_state()
    state["positions"] = [position]
    log.save_state(state)


@pytest.mark.parametrize("payload", [
    {"action": "buy_leap", "ticker": "NVDA", "strike": 90, "contracts": 5,
     "execution_price": 2000, "stock_price": 128},
    {"action": "sell_short", "ticker": "NVDA", "strike": 124, "contracts": 5,
     "premium_per_share": 5.0, "stock_price": 128},
    {"action": "roll_short", "ticker": "NVDA", "from_strike": 124, "to_strike": 126,
     "contracts": 5, "stock_price": 128},
])
def test_frozen_rejects_new_risk_actions(store, payload):
    _save(store, _frozen_position())
    with pytest.raises(executor.PositionFrozenError) as ei:
        executor.execute(payload)
    assert ei.value.ticker == "NVDA"


def test_frozen_allows_close_short(store):
    _save(store, _frozen_position(short_calls=[{
        "strike": 124, "contracts": 5, "dte": 3, "expiration": "2026-12-18",
        "entry_premium_total": 2500.0, "entry_extrinsic_per_share": 1.0,
        "current_bid": 0.5}]))
    out = executor.execute({"action": "close_short", "ticker": "NVDA", "strike": 124,
                            "contracts": 5, "close_price_per_share": 0.5, "stock_price": 120})
    assert out["status"] == "filled"


def test_frozen_allows_atomic_exit(store):
    _save(store, _frozen_position())
    out = executor.execute({"action": "close_position_atomic", "ticker": "NVDA",
                            "stock_price": 128, "leap_close_price": 20000})
    assert out["status"] == "filled"


def test_naked_short_guard_still_enforced_during_freeze(store):
    # Frozen, with an open short: a single-leg close_leap must STILL be refused
    # (naked-short guard), independent of the freeze exception.
    _save(store, _frozen_position(short_calls=[{
        "strike": 124, "contracts": 5, "dte": 3, "expiration": "2026-12-18",
        "entry_premium_total": 2500.0, "current_bid": 0.5}]))
    with pytest.raises(ValueError, match="naked short"):
        executor.execute({"action": "close_leap", "ticker": "NVDA", "strike": 90,
                          "contracts": 5, "close_price": 20000, "stock_price": 128})


# ---------------------------------------------------------------------------
# 5. Resolution
# ---------------------------------------------------------------------------
def _report_with(diff):
    return {"as_of": "2026-07-01T08:30:00Z", "status": reconcile.DIRTY,
            "diffs": [diff], "suggested_resolutions": [], "broker_ok": True, "error": None}


def test_resolve_expiry_books_zero_close_and_clears(store):
    pos = {
        "ticker": "NVDA", "status": "active", "needs_review": False, "review": None,
        "leap": {"strike": 90, "contracts": 5, "extrinsic_at_entry": 2000.0},
        "shares": {"count": 0, "cap": 500},
        "short_calls": [{"strike": 124, "contracts": 5, "dte": 0, "expiration": "2026-06-19",
                         "entry_premium_total": 2500.0, "entry_extrinsic_per_share": 1.0,
                         "current_bid": 0.0}],
    }
    state = log.load_state()
    state["positions"] = [pos]
    state["reconciliation"] = {"last": _report_with({
        "id": "diff_001", "classification": reconcile.EXPIRED_WORTHLESS_PENDING,
        "ticker": "NVDA", "strike": 124, "expiry": "2026-06-19", "expected_qty": -5,
        "broker_qty": None, "summary": "expired worthless", "expiry_close": 118.0,
    }), "history": [], "last_success": "2026-07-01T08:30:00Z"}
    log.save_state(state)

    out = executor.resolve_expiry("diff_001")
    assert out["status"] == "resolved"

    state = log.load_state()
    ex = state["executions"][-1]
    assert ex["action"] == "close_short" and ex["reason"] == "expired_worthless"
    assert ex["close_price_per_share"] == 0.0
    assert ex["date"].startswith("2026-06-19")  # timestamped to the expiry date
    # Short removed from the position.
    assert log.find_position(state, "NVDA")["short_calls"] == []
    # Diff marked resolved.
    d = state["reconciliation"]["last"]["diffs"][0]
    assert d["resolution"]["status"] == "resolved"


def test_adjustment_flows_through_recompute(store):
    pos = {
        "ticker": "NVDA", "status": "active", "needs_review": True,
        "review": {"summary": "x", "diff_ids": ["diff_001"]},
        "leap": {"strike": 90, "contracts": 5, "extrinsic_at_entry": 2000.0},
        "shares": {"count": 0, "cap": 500}, "short_calls": [],
    }
    state = log.load_state()
    state["positions"] = [pos]
    state["reconciliation"] = {"last": _report_with({
        "id": "diff_001", "classification": reconcile.QUANTITY_MISMATCH, "ticker": "NVDA",
        "instrument_type": "OPTION", "strike": 90, "expiry": "2026-12-18",
        "expected_qty": 5, "broker_qty": 3, "summary": "leap qty mismatch"}),
        "history": [], "last_success": "2026-07-01T08:30:00Z"}
    log.save_state(state)

    out = executor.execute({
        "action": "adjustment", "ticker": "NVDA", "instrument_type": "OPTION",
        "strike": 90, "quantity_delta": -2, "price": 0.0,
        "reason": "partial LEAP assignment reconciled to broker", "linked_diff_id": "diff_001"})
    assert out["status"] == "adjusted"

    state = log.load_state()
    # Immutable adjustment execution, flagged paper (logged mode).
    ex = state["executions"][-1]
    assert ex["action"] == "adjustment" and ex["live_transmitted"] is False
    # LEAP contracts corrected 5 -> 3, derived payback recomputed off it.
    assert log.find_position(state, "NVDA")["leap"]["contracts"] == 3
    assert "NVDA" in state["extrinsic_payback"]
    # Linked diff resolved -> freeze lifted (no other open diffs).
    assert log.find_position(state, "NVDA")["needs_review"] is False


def test_adjustment_requires_typed_reason(store):
    _save(store, _frozen_position())
    with pytest.raises(ValueError, match="reason"):
        executor.execute({"action": "adjustment", "ticker": "NVDA",
                          "instrument_type": "EQUITY", "quantity_delta": 500, "reason": ""})


def test_acknowledge_requires_typed_reason_then_lifts_freeze(store):
    state = log.load_state()
    state["positions"] = [_frozen_position()]
    state["reconciliation"] = {"last": _report_with({
        "id": "diff_001", "classification": reconcile.UNEXPECTED_AT_BROKER, "ticker": "NVDA",
        "strike": None, "expiry": None, "expected_qty": None, "broker_qty": 100,
        "summary": "corp-action replacement symbol"}), "history": [], "last_success": "x"}
    log.save_state(state)

    with pytest.raises(ValueError, match="ack_reason"):
        executor.acknowledge_diff("diff_001", "")

    executor.acknowledge_diff("diff_001", "corporate action, handled in the linked account")
    state = log.load_state()
    d = state["reconciliation"]["last"]["diffs"][0]
    assert d["resolution"]["status"] == "acknowledged"
    assert log.find_position(state, "NVDA")["needs_review"] is False


# ---------------------------------------------------------------------------
# 6. Failure isolation
# ---------------------------------------------------------------------------
def test_fetch_failure_generates_no_diffs_and_keeps_stale_clock(store, monkeypatch):
    state = log.load_state()
    state["positions"] = [_frozen_position(needs_review=False, review=None)]
    state["reconciliation"] = {"last": None, "history": [], "last_success": "2026-01-01T00:00:00Z"}
    log.save_state(state)

    def boom():
        raise schwab_api.SchwabError("token expired")
    monkeypatch.setattr(reconcile, "data_handler_client_accounts", boom)

    report = reconcile.run_reconciliation()
    assert report["broker_ok"] is False
    assert report["diffs"] == []
    assert report["status"] == "FAILED"

    state = log.load_state()
    # A failed run must NOT freeze anything and must NOT advance last_success.
    assert log.find_position(state, "NVDA")["needs_review"] is False
    assert state["reconciliation"]["last_success"] == "2026-01-01T00:00:00Z"


def test_zero_positions_is_valid_not_suppressed(store, monkeypatch):
    # A live LEAP in state, broker returns a valid all-cash (zero positions)
    # response -> the position is classified MISSING, not silently dropped.
    state = log.load_state()
    state["positions"] = [{
        "ticker": "NVDA", "status": "active", "needs_review": False,
        "leap": {"strike": 90, "contracts": 5, "expiration": "2026-12-18"},
        "shares": {"count": 0}, "short_calls": []}]
    state["executions"] = [{"id": "exec_001", "ticker": "NVDA", "action": "buy_leap",
                            "live_transmitted": True}]
    log.save_state(state)

    monkeypatch.setattr(reconcile, "data_handler_client_accounts",
                        lambda: [{"securitiesAccount": {"positions": []}}])
    report = reconcile.run_reconciliation()
    assert report["broker_ok"] is True
    assert _classes(report) == [reconcile.MISSING_AT_BROKER]


# ---------------------------------------------------------------------------
# 7. Paper / live split of the expected-view
# ---------------------------------------------------------------------------
def test_expected_view_excludes_paper_and_unknown_includes_live():
    state = {
        "positions": [
            {"ticker": "LIVE", "status": "active",
             "leap": {"strike": 90, "contracts": 5, "expiration": "2026-12-18"},
             "shares": {"count": 0}, "short_calls": []},
            {"ticker": "PAPER", "status": "active",
             "leap": {"strike": 50, "contracts": 5, "expiration": "2026-12-18"},
             "shares": {"count": 0}, "short_calls": []},
            {"ticker": "UNK", "status": "active",
             "leap": {"strike": 20, "contracts": 5, "expiration": "2026-12-18"},
             "shares": {"count": 0}, "short_calls": []},
        ],
        "executions": [
            {"ticker": "LIVE", "action": "buy_leap", "live_transmitted": True},
            {"ticker": "PAPER", "action": "buy_leap", "live_transmitted": False},
            {"ticker": "UNK", "action": "buy_leap", "live_transmitted": None},
        ],
    }
    view, excluded = reconcile.expected_view_from_state(state, live_only=True)
    tickers = {i["underlying"] for i in view}
    assert tickers == {"LIVE"}
    reasons = {e["ticker"]: e["reason"] for e in excluded}
    assert reasons == {"PAPER": "paper", "UNK": "unknown_live_status"}


def test_expected_view_all_when_not_live_only():
    state = {
        "positions": [{"ticker": "PAPER", "status": "active",
                       "leap": {"strike": 50, "contracts": 5, "expiration": "2026-12-18"},
                       "shares": {"count": 0}, "short_calls": []}],
        "executions": [{"ticker": "PAPER", "action": "buy_leap", "live_transmitted": False}],
    }
    view, excluded = reconcile.expected_view_from_state(state, live_only=False)
    assert {i["underlying"] for i in view} == {"PAPER"}
    assert excluded == []


# ---------------------------------------------------------------------------
# 7b. Alert conditions
# ---------------------------------------------------------------------------
def _state_with_report(diffs, positions=None, last_success="2026-07-01T08:30:00Z"):
    return {
        "metadata": {}, "positions": positions or [],
        "executions": [], "alerts": migrations.default_alert_state(),
        "reconciliation": {
            "last": {"as_of": "2026-07-01T08:30:00Z", "status": reconcile.DIRTY,
                     "diffs": diffs, "broker_ok": True, "error": None},
            "history": [], "last_success": last_success},
    }


def test_alert_short_stock_detected_copy_and_severity():
    import alerts
    st = _state_with_report([{
        "id": "diff_001", "classification": reconcile.SHORT_STOCK_DETECTED, "ticker": "NVDA",
        "broker_qty": -500, "summary": "short stock", "strike": None, "expiry": None}])
    out = alerts.check_short_stock_detected(st)
    assert len(out) == 1
    a = out[0]
    assert a["severity"] == "CRITICAL"
    assert "Do NOT exercise the LEAP" in a["action"]
    assert a["fingerprint"] == "SHORT_STOCK_DETECTED|NVDA|diff_001"


def test_alert_reconcile_dirty_and_short_stock_both_fire():
    import alerts
    st = _state_with_report([{
        "id": "diff_001", "classification": reconcile.SHORT_STOCK_DETECTED, "ticker": "NVDA",
        "broker_qty": -500, "summary": "short stock against LEAP", "strike": None, "expiry": None}])
    dirty = alerts.check_reconcile_dirty(st)
    short = alerts.check_short_stock_detected(st)
    # Distinct fingerprints -> escalation isn't swallowed by dedup.
    assert dirty and short
    assert dirty[0]["type"] == "RECONCILE_DIRTY"
    assert dirty[0]["fingerprint"] != short[0]["fingerprint"]


def test_alert_reconcile_dirty_ignores_benign_and_resolved():
    import alerts
    st = _state_with_report([
        {"id": "d1", "classification": reconcile.EXPIRED_WORTHLESS_PENDING, "ticker": "NVDA",
         "summary": "worthless", "strike": 200, "expiry": "2020-01-01"},
        {"id": "d2", "classification": reconcile.MISSING_AT_BROKER, "ticker": "AMD",
         "summary": "gone", "strike": 120, "expiry": "2099-01-01",
         "resolution": {"status": "resolved"}},
    ])
    assert alerts.check_reconcile_dirty(st) == []


def test_alert_reconcile_stale_fires_when_no_recent_success(store, monkeypatch):
    import alerts
    import schwab_api as sw
    monkeypatch.setattr(sw, "configured", lambda: True)
    st = {"metadata": {}, "positions": [_frozen_position(needs_review=False)],
          "reconciliation": {"last": None, "history": [], "last_success": None}}
    out = alerts.check_reconcile_stale(st)
    assert len(out) == 1 and out[0]["type"] == "RECONCILE_STALE"


def test_alert_reconcile_stale_quiet_when_recent(store, monkeypatch):
    import alerts
    import schwab_api as sw
    monkeypatch.setattr(sw, "configured", lambda: True)
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = {"metadata": {}, "positions": [_frozen_position(needs_review=False)],
          "reconciliation": {"last": None, "history": [], "last_success": fresh}}
    assert alerts.check_reconcile_stale(st) == []


def test_alert_reconcile_stale_quiet_when_no_open_positions(store, monkeypatch):
    import alerts
    import schwab_api as sw
    monkeypatch.setattr(sw, "configured", lambda: True)
    st = {"metadata": {}, "positions": [], "reconciliation": {"last_success": None}}
    assert alerts.check_reconcile_stale(st) == []


# ---------------------------------------------------------------------------
# 8. Migration v6 -> v7
# ---------------------------------------------------------------------------
def _v6_state() -> dict:
    return {
        "schema_version": 6,
        "metadata": {"last_updated": "2024-01-01T00:00:00Z", "capital_deployed": 0},
        "positions": [{"ticker": "NVDA", "status": "active", "leap": {"strike": 90, "contracts": 5}}],
        "executions": [
            {"id": "exec_001", "ticker": "NVDA", "action": "buy_leap", "mode": "live"},
            {"id": "exec_002", "ticker": "NVDA", "action": "sell_short", "mode": "logged"},
            {"id": "exec_003", "ticker": "NVDA", "action": "buy_leap"},  # no mode -> unknown
        ],
        "theta_ledger": {"weeks": [], "totals": {}},
        "extrinsic_payback": {}, "roll_ledger": {"rolls": [], "by_ticker": {}},
        "cycles": [], "pending_orders": {}, "alerts": migrations.default_alert_state(),
    }


def test_v6_to_v7_migration_additive_with_snapshot(store):
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(_v6_state(), fh, indent=2)

    state = log.load_state()  # triggers v6 -> v7
    assert state["schema_version"] == migrations.CURRENT_VERSION == 7

    # Additive structures present.
    assert state["reconciliation"] == {"last": None, "history": [], "last_success": None} \
        or "last" in state["reconciliation"]
    p = log.find_position(state, "NVDA")
    assert p["needs_review"] is False and p["review"] is None
    # live_transmitted backfilled from mode; unknown -> None.
    flags = {e["id"]: e["live_transmitted"] for e in state["executions"]}
    assert flags == {"exec_001": True, "exec_002": False, "exec_003": None}

    # Pre-migration snapshot captured the v6 bytes.
    snaps = glob.glob(os.path.join(backups.backups_dir(),
                                   f"pre-migration-v6-to-v{migrations.CURRENT_VERSION}-*.json"))
    assert len(snaps) == 1
    assert json.load(open(snaps[0], encoding="utf-8"))["schema_version"] == 6


# ---------------------------------------------------------------------------
# 9. API layer (Flask)
# ---------------------------------------------------------------------------
def test_api_execute_frozen_returns_409(store):
    _save(store, _frozen_position())
    import app as app_module
    client = app_module.app.test_client()
    resp = client.post("/api/execute", json={
        "action": "buy_leap", "ticker": "NVDA", "strike": 90, "contracts": 5,
        "execution_price": 2000, "stock_price": 128})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["frozen"] is True and body["ticker"] == "NVDA"


def test_api_reconcile_get_and_post(store, monkeypatch):
    import app as app_module
    import alerts
    state = log.load_state()
    state["positions"] = [{
        "ticker": "NVDA", "status": "active", "needs_review": False,
        "leap": {"strike": 90, "contracts": 5, "expiration": "2026-12-18"},
        "shares": {"count": 0}, "short_calls": []}]
    state["executions"] = [{"id": "e1", "ticker": "NVDA", "action": "buy_leap",
                            "live_transmitted": True}]
    log.save_state(state)

    monkeypatch.setattr(reconcile, "data_handler_client_accounts",
                        lambda: [{"securitiesAccount": {"positions": []}}])
    monkeypatch.setattr(alerts, "run", lambda *a, **k: None)

    client = app_module.app.test_client()
    posted = client.post("/api/reconcile").get_json()
    assert posted["broker_ok"] is True and posted["status"] == reconcile.DIRTY

    got = client.get("/api/reconcile").get_json()
    assert got["last"]["status"] == reconcile.DIRTY
    # The NVDA LEAP is now frozen (missing at broker).
    assert log.find_position(log.load_state(), "NVDA")["needs_review"] is True
