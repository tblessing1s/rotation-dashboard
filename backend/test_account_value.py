"""Mark-to-market account value (position_manager.account_value) and its daily
snapshot (maintenance.snapshot_account_value) — the History tab's value-over-
time chart. Not deployed capital (cost basis): shares/LEAP at current market
value, plus cash and put collateral, minus the cost to buy back every open
short leg right now."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
import logging_handler as log  # noqa: E402
import maintenance  # noqa: E402
import position_manager as pm  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    # demo_mode stays FALSE here (unlike the account_value() unit tests below):
    # active_state_path() routes to DEMO_STATE_PATH — a path shared across the
    # whole test session — whenever demo_mode is on, which would leak state
    # between these tests despite the STATE_PATH override. Schwab isn't
    # configured in the test env either way, so resolve_operating_cash still
    # falls back to the manual figure — demo_mode isn't needed for that.
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _mute_quote_warmup(monkeypatch):
    # positions_view's batched pre-warm isn't the thing under test — muting it
    # keeps the test from depending on real market data / network reachability.
    import data_handler
    monkeypatch.setattr(data_handler, "latest_quotes", lambda tickers: {})


# ---------------------------------------------------------------------------
# position_manager.account_value
# ---------------------------------------------------------------------------
def test_account_value_sums_shares_cash_and_collateral_minus_short_liability(monkeypatch):
    _mute_quote_warmup(monkeypatch)
    monkeypatch.setattr(pm, "_stock_price", lambda t: 150.0)
    state = {
        "metadata": {"operating_cash": 5000.0},
        "positions": [{
            "ticker": "AAA", "status": "active", "position_type": "SHARES",
            "shares": {"count": 100, "cost_basis_per_share": 120.0},
            "short_calls": [{"strike": 155, "contracts": 1, "current_bid": 2.0,
                             "entry_premium_total": 300.0}],
        }],
    }
    v = pm.account_value(state)
    # 100 sh @ 150 = 15000; cash 5000; − short liability 2.00 * 1 * 100 = 200.
    assert v["shares_value"] == 15000.0
    assert v["operating_cash"] == 5000.0
    assert v["short_liability"] == 200.0
    assert v["put_collateral"] == 0.0
    assert v["leap_value"] == 0.0
    assert v["total"] == 15000.0 + 5000.0 - 200.0


def test_account_value_includes_put_collateral_and_put_liability(monkeypatch):
    _mute_quote_warmup(monkeypatch)
    monkeypatch.setattr(pm, "_stock_price", lambda t: 47.0)
    monkeypatch.setattr(pm, "_put_mark_per_share", lambda t, leg, spot: 1.50)
    state = {
        "metadata": {"operating_cash": 10000.0},
        "positions": [{
            "ticker": "BBB", "status": "active", "position_type": "CASH_SECURED_PUT",
            "shares": {"count": 0},
            "short_puts": [{"strike": 45, "contracts": 2, "collateral": 9000.0}],
        }],
    }
    v = pm.account_value(state)
    assert v["shares_value"] == 0.0
    assert v["put_collateral"] == 9000.0
    # buy-back cost: 1.50/sh * 2 contracts * 100 = 300
    assert v["short_liability"] == 300.0
    assert v["total"] == 10000.0 + 9000.0 - 300.0


def test_account_value_includes_legacy_leap_current_value(monkeypatch):
    """A legacy diagonal's long leg is real, tradeable value — omitting it would
    understate the account by exactly the money still tied up in it."""
    _mute_quote_warmup(monkeypatch)
    monkeypatch.setattr(pm, "_stock_price", lambda t: 140.0)
    state = {
        "metadata": {"operating_cash": 0.0},
        "positions": [{
            "ticker": "CCC", "status": "active", "position_type": "LEAP_PMCC_LEGACY",
            "shares": {"count": 0},
            "leap": {"strike": 90, "contracts": 1, "cost_basis": 6000.0,
                     "current_bid": 7200.0, "expiration": "2027-01-15"},
        }],
    }
    v = pm.account_value(state)
    assert v["leap_value"] == 7200.0
    assert v["total"] == 7200.0


def test_account_value_ignores_closed_positions(monkeypatch):
    _mute_quote_warmup(monkeypatch)
    monkeypatch.setattr(pm, "_stock_price", lambda t: 999.0)
    state = {
        "metadata": {"operating_cash": 0.0},
        "positions": [{
            "ticker": "ZZZ", "status": "closed",
            "shares": {"count": 100, "cost_basis_per_share": 1.0},
        }],
    }
    assert pm.account_value(state)["total"] == 0.0


def test_account_value_missing_quote_drops_to_zero_not_an_error(monkeypatch):
    """A stale/unreachable name must not blank the whole figure — same
    best-effort trade-off enrich_position already makes."""
    _mute_quote_warmup(monkeypatch)
    monkeypatch.setattr(pm, "_stock_price", lambda t: None)
    state = {
        "metadata": {"operating_cash": 250.0},
        "positions": [{
            "ticker": "AAA", "status": "active", "position_type": "SHARES",
            "shares": {"count": 50, "cost_basis_per_share": 10.0},
        }],
    }
    v = pm.account_value(state)
    assert v["shares_value"] == 0.0
    assert v["total"] == 250.0


# ---------------------------------------------------------------------------
# maintenance.snapshot_account_value
# ---------------------------------------------------------------------------
def test_snapshot_account_value_appends_one_point_per_day(isolated_state, monkeypatch):
    _mute_quote_warmup(monkeypatch)
    monkeypatch.setattr(pm, "_stock_price", lambda t: 100.0)
    state = {"metadata": {"operating_cash": 1000.0}, "positions": []}
    log.save_state(state)

    p1 = maintenance.snapshot_account_value(today="2026-06-01")
    assert p1["date"] == "2026-06-01" and p1["total"] == 1000.0
    hist = log.load_state()["account_value_history"]
    assert [h["date"] for h in hist] == ["2026-06-01"]

    p2 = maintenance.snapshot_account_value(today="2026-06-02")
    assert p2["date"] == "2026-06-02"
    hist = log.load_state()["account_value_history"]
    assert [h["date"] for h in hist] == ["2026-06-01", "2026-06-02"]


def test_snapshot_account_value_overwrites_same_day_rather_than_duplicating(isolated_state, monkeypatch):
    """A mid-day restart re-runs the nightly slot — the day gets ONE point, not
    two, exactly like snapshot_leap_deltas."""
    _mute_quote_warmup(monkeypatch)
    log.save_state({"metadata": {"operating_cash": 1000.0}, "positions": []})
    maintenance.snapshot_account_value(today="2026-06-01")
    monkeypatch.setattr(pm, "_stock_price", lambda t: 100.0)
    state = log.load_state()
    state["metadata"]["operating_cash"] = 2500.0
    log.save_state(state)
    maintenance.snapshot_account_value(today="2026-06-01")
    hist = log.load_state()["account_value_history"]
    assert len(hist) == 1
    assert hist[0]["total"] == 2500.0


def test_snapshot_account_value_retains_only_the_newest_n_days(isolated_state, monkeypatch):
    _mute_quote_warmup(monkeypatch)
    monkeypatch.setattr(config, "ACCOUNT_VALUE_HISTORY_DAYS", 3)
    log.save_state({"metadata": {"operating_cash": 0.0}, "positions": []})
    for d in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        maintenance.snapshot_account_value(today=d)
    hist = log.load_state()["account_value_history"]
    assert [h["date"] for h in hist] == ["2026-01-02", "2026-01-03", "2026-01-04"]


def test_snapshot_account_value_skipped_in_demo_mode_via_nightly_refresh(monkeypatch):
    """nightly_refresh() (the real caller) short-circuits entirely in demo
    mode — snapshot_account_value itself is never reached."""
    def _boom():
        raise AssertionError("snapshot_account_value must not run in demo mode")
    monkeypatch.setattr(maintenance, "snapshot_account_value", _boom)
    monkeypatch.setattr(config, "_demo_mode", True)
    result = maintenance.nightly_refresh()
    assert result["skipped"] == "demo mode"
