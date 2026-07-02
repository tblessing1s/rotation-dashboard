"""Level 5 (Account & Juice) gate tests — juice estimate math, each blocking
check with rigged state, warnings, executor enforcement + override logging,
circuit-breaker storage, and the v3 migration."""
import json
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import account_gate  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


def _noisy_frame(base=150.0, n=260, sigma=0.02, seed=7):
    rng = np.random.RandomState(seed)
    close = base * np.exp(np.cumsum(rng.normal(0.0005, sigma, n)))
    idx = pd.bdate_range("2024-01-01", periods=n)
    c = pd.Series(close, index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": 1e6}, index=idx)


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _seed_state(**meta_over):
    state = log.load_state()
    state["metadata"].update({"operating_cash": 40000, "capital_deployed": 0, **meta_over})
    state["positions"] = []
    log.save_state(state)
    return state


# ---- juice estimate ---------------------------------------------------------
def test_juice_estimate_prices_short_and_leap(monkeypatch):
    df = _noisy_frame(sigma=0.02)  # ~30% annualized realized vol
    est = account_gate.juice_estimate("XYZ", df)
    S = float(df["Close"].iloc[-1])
    assert est["weekly_extrinsic_per_share"] > 0
    assert est["leap_strike"] < S  # a 0.90-delta LEAP is well ITM
    assert est["leap_cost_per_share"] > S - est["leap_strike"]  # cost > intrinsic
    assert est["weekly_yield_pct"] == pytest.approx(
        est["weekly_extrinsic_per_share"] / est["leap_cost_per_share"] * 100, abs=0.05)


def test_juice_estimate_missing_data():
    est = account_gate.juice_estimate("XYZ", None)
    assert est["weekly_yield_pct"] is None


def test_weekly_yield_target_is_cycle_floor():
    # 15% over 8 weeks -> 1.88 %/week
    assert account_gate.weekly_yield_target_pct() == pytest.approx(1.88, abs=0.01)


def test_suggested_circuit_breaker_max_of_ma50_and_atr_stop():
    df = _frame([100.0] * 260)  # flat: MA50 = 100, ATR = 2 -> price - 2*ATR = 96
    cb = account_gate.suggested_circuit_breaker("XYZ", df)
    assert cb["price"] == 100.0 and cb["ma50"] == 100.0 and cb["atr_stop"] == 96.0


# ---- the gate ---------------------------------------------------------------
def _rich_df():
    return _noisy_frame(sigma=0.02, base=150.0)


def test_gate_passes_clean_account(isolated_state, monkeypatch):
    import data_handler
    df = _rich_df()
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    _seed_state(operating_cash=100000)
    # Chain says the trade pays 3%/week of LEAP cost.
    g = account_gate.evaluate("NVDA", contracts=5,
                              leap_cost_per_share=40.0, weekly_extrinsic_per_share=1.20)
    assert g["pass"] is True and g["blocking_failures"] == []
    assert g["juice"]["weekly_yield_pct"] == 3.0 and g["juice"]["source"] == "chain"
    assert g["suggested_circuit_breaker"]["price"] is not None


def test_gate_blocks_low_cash(isolated_state, monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _rich_df())
    _seed_state(operating_cash=15000)
    g = account_gate.evaluate("NVDA", contracts=5,
                              leap_cost_per_share=40.0, weekly_extrinsic_per_share=1.20)
    # 5 contracts * $40/sh = $20,000 > $15,000 cash -> free cash goes negative.
    assert "cash_reserve" in g["blocking_failures"]
    cash = next(c for c in g["checks"] if c["id"] == "cash_reserve")
    assert cash["detail"]["free_cash_after"] < 0
    assert cash["detail"]["reserve_required"] > 0  # the computed number is shown


def test_gate_blocks_third_position_and_sector_concentration(isolated_state, monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _rich_df())
    state = _seed_state(operating_cash=200000)
    state["positions"] = [
        {"ticker": "AAPL", "sector": "XLK", "status": "active",
         "leap": {"contracts": 5, "strike": 100}},
        {"ticker": "MSFT", "sector": "XLK", "status": "active",
         "leap": {"contracts": 5, "strike": 200}},
    ]
    log.save_state(state)
    g = account_gate.evaluate("NVDA", contracts=5,
                              leap_cost_per_share=40.0, weekly_extrinsic_per_share=1.20)
    assert "position_limit" in g["blocking_failures"]  # third concurrent position
    assert "sector_concentration" in g["blocking_failures"]  # NVDA is XLK too
    sec = next(c for c in g["checks"] if c["id"] == "sector_concentration")
    assert sec["detail"]["already_held"] == ["AAPL", "MSFT"]


def test_gate_blocks_capital_cap_and_low_juice(isolated_state, monkeypatch):
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _rich_df())
    _seed_state(operating_cash=100000, capital_deployed=30000)
    g = account_gate.evaluate("NVDA", contracts=5,
                              leap_cost_per_share=40.0,  # +20k -> 50k > 38k cap
                              weekly_extrinsic_per_share=0.20)  # 0.5%/wk < 1.88%
    assert "capital_limit" in g["blocking_failures"]
    assert "juice_adequacy" in g["blocking_failures"]
    juice = next(c for c in g["checks"] if c["id"] == "juice_adequacy")
    assert juice["detail"]["weekly_yield_pct"] == 0.5


def test_gate_warns_juice_too_rich_and_earnings(isolated_state, monkeypatch):
    import data_handler
    import earnings
    df = _rich_df()
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(earnings, "next_earnings",
                        lambda t, refresh=False: {"date": "2026-07-20", "days_until": 18,
                                                  "warning": False})
    _seed_state(operating_cash=200000)
    est = account_gate.juice_estimate("NVDA", df)
    rich = est["weekly_extrinsic_per_share"] * (config.JUICE_RICH_FACTOR + 1)
    g = account_gate.evaluate("NVDA", contracts=5,
                              leap_cost_per_share=est["leap_cost_per_share"],
                              weekly_extrinsic_per_share=rich)
    # Rich juice and in-cycle earnings warn but do NOT block.
    assert "juice_rich" in g["warnings"] and "earnings_in_cycle" in g["warnings"]
    assert "juice_rich" not in g["blocking_failures"]


# ---- executor enforcement -----------------------------------------------------
def test_executor_blocks_then_overrides_with_logged_reason(isolated_state, monkeypatch):
    import data_handler
    import executor
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _rich_df())
    state = _seed_state(operating_cash=1000)  # rigged: can't afford the entry

    with pytest.raises(ValueError) as ei:
        executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 100,
                          "contracts": 5, "execution_price": 4000, "stock_price": 140})
    assert "cash_reserve" in str(ei.value)

    res = executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 100,
                            "contracts": 5, "execution_price": 4000, "stock_price": 140,
                            "override_reason": "testing the override path",
                            "circuit_breaker_price": 123.45})
    assert res["success"] is True
    ex = res["execution"]
    assert ex["override"]["reason"] == "testing the override path"
    assert "cash_reserve" in ex["override"]["failed_checks"]
    assert ex["circuit_breaker_price"] == 123.45

    pos = log.find_position(log.load_state(), "NVDA")
    assert pos["circuit_breaker"] == {"price": 123.45, "source": "operator",
                                      "set_at": ex["date"][:10]}
    assert "dividend" in pos  # snapshot stored (None fields offline)


def test_executor_defaults_circuit_breaker_when_not_supplied(isolated_state, monkeypatch):
    import data_handler
    import executor
    df = _frame([100.0] * 260)  # MA50 100, ATR 2 -> suggested line = 100
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    _seed_state(operating_cash=500000)
    executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 80,
                      "contracts": 5, "execution_price": 2500, "stock_price": 100,
                      "override_reason": "fixture (juice estimate is low-vol)"})
    pos = log.find_position(log.load_state(), "NVDA")
    assert pos["circuit_breaker"]["price"] == 100.0
    assert pos["circuit_breaker"]["source"] == "default"


def test_non_entry_actions_skip_the_gate(isolated_state, monkeypatch):
    import executor
    # sell_short on a rigged-empty account must not consult the Level 5 gate.
    res = executor.execute({"action": "sell_short", "ticker": "NVDA", "strike": 140,
                            "contracts": 5, "premium_per_share": 1.0, "stock_price": 145})
    assert res["success"] is True


# ---- migration v3 --------------------------------------------------------------
def test_v2_state_migrates_positions_to_v3(isolated_state):
    v2 = {
        "schema_version": 2,
        "metadata": {"last_updated": "2026-06-01T00:00:00Z"},
        "positions": [{"ticker": "NVDA", "status": "active"}],
        "executions": [], "theta_ledger": {"weeks": [], "totals": {}},
        "extrinsic_payback": {}, "pending_orders": {},
        "alerts": migrations.default_alert_state(),
    }
    with open(config.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(v2, fh)
    state = log.load_state()
    assert state["schema_version"] == migrations.CURRENT_VERSION >= 3
    assert state["positions"][0]["circuit_breaker"] is None
    assert state["positions"][0]["dividend"] is None


# ---- live Schwab cash balance ---------------------------------------------------
def test_account_cash_parses_field_priority():
    import schwab_api
    # cashAvailableForTrading wins over cashBalance when both are present.
    node = {"securitiesAccount": {"currentBalances": {
        "cashAvailableForTrading": 1000.5, "cashBalance": 2000.0}}}
    assert schwab_api._account_cash(node) == 1000.5
    # Falls back down the list when the preferred field is absent.
    node2 = {"securitiesAccount": {"currentBalances": {"cashBalance": 500.25}}}
    assert schwab_api._account_cash(node2) == 500.25
    # No recognizable field, or junk types -> None (never raises).
    assert schwab_api._account_cash({"securitiesAccount": {"currentBalances": {}}}) is None
    assert schwab_api._account_cash({}) is None
    node3 = {"securitiesAccount": {"currentBalances": {"cashAvailableForTrading": "n/a"}}}
    assert schwab_api._account_cash(node3) is None  # unparseable -> skip to next field, none left


def test_cash_balance_caches_and_raises_on_empty_or_missing_field(monkeypatch):
    import schwab_api
    monkeypatch.setattr(schwab_api, "_accounts_cache", None)
    calls = []

    class _Client(schwab_api.SchwabClient):
        def get_accounts(self, positions=True):
            calls.append(1)
            return [{"securitiesAccount": {"currentBalances": {"cashAvailableForTrading": 777.0}}}]

    c = _Client()
    assert c.cash_balance() == 777.0
    assert c.cash_balance() == 777.0  # second call within TTL reuses the cache
    assert len(calls) == 1
    assert c.cash_balance(force=True) == 777.0
    assert len(calls) == 2  # force bypasses the cache

    class _EmptyClient(schwab_api.SchwabClient):
        def get_accounts(self, positions=True):
            return []

    with pytest.raises(schwab_api.SchwabError):
        _EmptyClient().cash_balance(force=True)

    class _NoFieldClient(schwab_api.SchwabClient):
        def get_accounts(self, positions=True):
            return [{"securitiesAccount": {"currentBalances": {}}}]

    with pytest.raises(schwab_api.SchwabError):
        _NoFieldClient().cash_balance(force=True)


def test_resolve_operating_cash_demo_and_unconfigured_use_manual(isolated_state, monkeypatch):
    import schwab_api
    state = _seed_state(operating_cash=12345.0)
    monkeypatch.setattr(schwab_api, "configured", lambda: False)
    info = account_gate.resolve_operating_cash(state)
    assert info == {"amount": 12345.0, "source": "manual", "error": None}

    monkeypatch.setattr(config, "_demo_mode", True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)  # demo wins even if "configured"
    info = account_gate.resolve_operating_cash(state)
    assert info["source"] == "manual" and info["amount"] == 12345.0


class _FakeCashClient:
    def __init__(self, cash):
        self._cash = cash

    def cash_balance(self, force=False):
        return self._cash


def test_resolve_operating_cash_live_success_persists_and_overrides_manual(isolated_state, monkeypatch):
    import data_handler
    import schwab_api
    state = _seed_state(operating_cash=12345.0)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FakeCashClient(9876.54))

    info = account_gate.resolve_operating_cash(state)
    assert info == {"amount": 9876.54, "source": "schwab", "error": None}
    # Persisted back to state.metadata so every other reader sees the fresh value.
    assert log.load_state()["metadata"]["operating_cash"] == 9876.54


def test_resolve_operating_cash_live_failure_degrades_to_manual(isolated_state, monkeypatch):
    import data_handler
    import schwab_api

    class _FailingClient:
        def cash_balance(self, force=False):
            raise schwab_api.SchwabError("token expired")

    state = _seed_state(operating_cash=5000.0)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FailingClient())

    info = account_gate.resolve_operating_cash(state)
    assert info["amount"] == 5000.0 and info["source"] == "manual"
    assert "token expired" in info["error"]
    # A failed live read must not clobber the manual value on disk.
    assert log.load_state()["metadata"]["operating_cash"] == 5000.0


def test_evaluate_cash_reserve_check_reports_live_source(isolated_state, monkeypatch):
    import data_handler
    import schwab_api
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _noisy_frame())
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FakeCashClient(100000.0))
    _seed_state(operating_cash=1.0)  # manual value would fail the reserve check

    g = account_gate.evaluate("NVDA", contracts=5,
                              leap_cost_per_share=40.0, weekly_extrinsic_per_share=1.20)
    cash = next(c for c in g["checks"] if c["id"] == "cash_reserve")
    assert cash["detail"]["operating_cash_source"] == "schwab"
    assert cash["detail"]["operating_cash"] == 100000.0
    assert cash["pass"] is True  # the live balance, not the stale manual $1, decides it


# ---- capital_summary / portfolio_view live-cash wiring -------------------------
def test_capital_summary_uses_live_balance_when_configured(isolated_state, monkeypatch):
    import data_handler
    import position_manager as pm
    import schwab_api
    state = _seed_state(operating_cash=1000.0, reserve_required=500.0)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FakeCashClient(42000.0))

    summary = pm.capital_summary(log.load_state())
    assert summary["operating_cash"] == 42000.0
    assert summary["operating_cash_source"] == "schwab"
    assert summary["reserve_ok"] is True


def test_portfolio_view_uses_live_balance_when_configured(isolated_state, monkeypatch):
    import data_handler
    import portfolio_risk as pr
    import schwab_api
    _seed_state(operating_cash=1000.0)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: _FakeCashClient(55000.0))

    view = pr.portfolio_view(log.load_state())
    assert view["capital"]["operating_cash"] == 55000.0
    assert view["capital"]["operating_cash_source"] == "schwab"
