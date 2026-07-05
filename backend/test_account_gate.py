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


def test_gate_warns_juice_rich_but_blocks_on_earnings_in_cycle(isolated_state, monkeypatch):
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
    # Rich juice stays a warning; earnings inside the cycle now BLOCKS.
    assert "juice_rich" in g["warnings"] and "juice_rich" not in g["blocking_failures"]
    assert "earnings_in_cycle" in g["blocking_failures"]
    assert g["pass"] is False


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


# ---- bulk Level 5 (evaluate_many) -----------------------------------------------
def test_evaluate_many_empty_list_short_circuits():
    assert account_gate.evaluate_many([]) == {}


def test_evaluate_many_shares_one_state_object_across_tickers(isolated_state, monkeypatch):
    # dividends/earnings each do their own log.load_state() for manual-override
    # lookups (unrelated, pre-existing, cheap) — the optimization this targets
    # is evaluate_many's OWN top-level state, which every per-ticker evaluate()
    # call should reuse rather than re-reading state.json itself.
    import data_handler
    seen_state_ids = []
    real_evaluate = account_gate.evaluate

    def _tracking_evaluate(ticker, contracts=None, leap_cost_per_share=None,
                           weekly_extrinsic_per_share=None, state=None):
        seen_state_ids.append(id(state))
        return real_evaluate(ticker, contracts, leap_cost_per_share,
                             weekly_extrinsic_per_share, state)

    monkeypatch.setattr(account_gate, "evaluate", _tracking_evaluate)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _noisy_frame(seed=hash(s) % 100))
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    _seed_state(operating_cash=100000)

    out = account_gate.evaluate_many(["NVDA", "amd", " msft "], contracts=5)
    assert set(out.keys()) == {"NVDA", "AMD", "MSFT"}
    assert all(r["ticker"] == t for t, r in out.items())
    assert all("checks" in r for r in out.values())
    assert len(seen_state_ids) == 3
    assert len(set(seen_state_ids)) == 1  # every call reused the exact same state object
    assert None not in seen_state_ids


def test_evaluate_many_reflects_shared_book_state_across_tickers(isolated_state, monkeypatch):
    # With MAX_CFM_POSITIONS already open, every candidate should fail
    # position_limit using the SAME shared state (not a stale per-call load).
    import data_handler
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: _noisy_frame())
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    state = _seed_state(operating_cash=200000)
    state["positions"] = [
        {"ticker": "AAPL", "sector": "XLK", "status": "active", "leap": {"contracts": 5, "strike": 100}},
        {"ticker": "GOOGL", "sector": "XLC", "status": "active", "leap": {"contracts": 5, "strike": 100}},
    ]
    log.save_state(state)

    out = account_gate.evaluate_many(["NVDA", "MSFT"], contracts=5)
    for r in out.values():
        assert "position_limit" in r["blocking_failures"]


# ---- GET /api/scan/ready ---------------------------------------------------------
def test_scan_ready_splits_go_rows_by_level5_and_sorts_by_juice(isolated_state, monkeypatch):
    from metrics import scorecard as scorecard_metrics
    import app as app_module

    rows = [
        {"ticker": "AAA", "sector": "XLK", "verdict": "GO", "juice_weekly_pct": 1.0, "earnings_date": None},
        {"ticker": "BBB", "sector": "XLK", "verdict": "GO", "juice_weekly_pct": 3.0, "earnings_date": None},
        {"ticker": "CCC", "sector": "XLK", "verdict": "CAUTION", "juice_weekly_pct": 5.0, "earnings_date": None},
    ]
    monkeypatch.setattr(scorecard_metrics, "scorecard",
                        lambda tickers=None: {"as_of": "2026-07-02T00:00:00Z", "results": rows})

    def _fake_evaluate_many(tickers, contracts=None):
        # AAA blocked (thin juice), BBB passes — CCC is excluded before this
        # is even called since it isn't a GO row.
        assert set(tickers) == {"AAA", "BBB"}
        return {
            "AAA": {"pass": False, "blocking_failures": ["juice_adequacy"]},
            "BBB": {"pass": True, "blocking_failures": []},
        }
    monkeypatch.setattr(account_gate, "evaluate_many", _fake_evaluate_many)

    client = app_module.app.test_client()
    resp = client.get("/api/scan/ready")
    assert resp.status_code == 200
    body = resp.get_json()
    assert [r["ticker"] for r in body["ready"]] == ["BBB"]
    assert [r["ticker"] for r in body["near_misses"]] == ["AAA"]
    assert body["near_misses"][0]["level5"]["blocking_failures"] == ["juice_adequacy"]


def test_scan_ready_sorts_multiple_ready_rows_by_juice_descending(isolated_state, monkeypatch):
    from metrics import scorecard as scorecard_metrics
    import app as app_module

    rows = [
        {"ticker": "LOW", "sector": "XLK", "verdict": "GO", "juice_weekly_pct": 2.0, "earnings_date": None},
        {"ticker": "HIGH", "sector": "XLK", "verdict": "GO", "juice_weekly_pct": 6.0, "earnings_date": None},
    ]
    monkeypatch.setattr(scorecard_metrics, "scorecard",
                        lambda tickers=None: {"as_of": "x", "results": rows})
    monkeypatch.setattr(account_gate, "evaluate_many", lambda tickers, contracts=None: {
        t: {"pass": True, "blocking_failures": []} for t in tickers})

    client = app_module.app.test_client()
    body = client.get("/api/scan/ready").get_json()
    assert [r["ticker"] for r in body["ready"]] == ["HIGH", "LOW"]


def test_earnings_beyond_the_cycle_does_not_block(isolated_state, monkeypatch):
    import data_handler
    import earnings
    df = _rich_df()
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(earnings, "next_earnings",
                        lambda t, refresh=False: {"date": "2026-12-01", "days_until": 150,
                                                  "warning": False})
    _seed_state(operating_cash=100000)
    g = account_gate.evaluate("NVDA", contracts=5,
                              leap_cost_per_share=40.0, weekly_extrinsic_per_share=1.20)
    assert "earnings_in_cycle" not in g["blocking_failures"] and g["pass"] is True
