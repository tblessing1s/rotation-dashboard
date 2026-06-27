"""CFM backend tests — indicator math, sector parsing, and the execute/ledger
flow. Run offline (no provider keys) with: python -m pytest backend -q
"""
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

# Point state/cache at a temp dir before importing config-bound modules.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
import indicators as ind  # noqa: E402
import sector_data  # noqa: E402


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


# ---- sector data -----------------------------------------------------------
def test_sectors_parse():
    etfs = sector_data.sector_etfs()
    assert "XLK" in etfs and len(etfs) == 11
    assert "NVDA" in sector_data.constituents("XLK")
    assert sector_data.sector_for("NVDA") == "XLK"
    assert sector_data.sector_for("XLK") == "XLK"  # ETFs map to themselves


# ---- indicators ------------------------------------------------------------
def test_sma_and_pct_from_ma():
    df = _frame(list(range(1, 60)))
    assert ind.sma(df, 21) == pytest.approx(df["Close"].tail(21).mean())
    assert ind.pct_from_ma(df, 21) > 0  # rising series sits above its MA


def test_rsi_bounds():
    df = _frame(100 + np.cumsum(np.random.RandomState(0).normal(0, 1, 80)))
    r = ind.rsi(df)
    assert 0 <= r <= 100


def test_atr_positive():
    df = _frame(100 + np.cumsum(np.random.RandomState(1).normal(0, 1, 60)))
    assert ind.atr(df, 9) > 0
    assert ind.atr_pct(df, 9) > 0


def test_rs3m_outperformer_positive():
    n = 100
    bench = _frame([100] * n)
    strong = _frame([100 + i for i in range(n)])  # symbol climbs vs flat bench
    assert ind.rs3m(strong, bench) > 0


def test_short_strike_spacing():
    # price 150, ATR 4, 1.5x -> 150 - 6 = 144
    assert ind.short_strike(150.0, 4.0) == 144.0


def test_calculate_extrinsic_midpoint_minus_intrinsic():
    # underlying 145, strike 140 -> intrinsic 5; mid (8+9)/2=8.5 -> extrinsic 3.5
    assert ind.calculate_extrinsic(8.0, 9.0, 140.0, 145.0) == pytest.approx(3.5)
    # OTM call: intrinsic 0 -> extrinsic = mid
    assert ind.calculate_extrinsic(1.0, 1.5, 150.0, 145.0) == pytest.approx(1.25)
    # missing quote -> None; deep ITM under intrinsic clamps to 0
    assert ind.calculate_extrinsic(None, 2.0, 140.0, 145.0) is None
    assert ind.calculate_extrinsic(4.0, 4.5, 140.0, 145.0) == 0.0


def test_find_leap_strike_picks_dte_then_delta():
    contracts = [
        {"strike": 120.0, "dte": 178, "delta": 0.95, "bid": 27.0, "ask": 27.4, "mark": 27.2},
        {"strike": 130.0, "dte": 178, "delta": 0.90, "bid": 18.0, "ask": 18.4, "mark": 18.2},
        {"strike": 140.0, "dte": 178, "delta": 0.70, "bid": 10.0, "ask": 10.4, "mark": 10.2},
        # a closer-to-money but wrong-DTE expiration must be ignored for DTE choice
        {"strike": 130.0, "dte": 30, "delta": 0.90, "bid": 16.0, "ask": 16.4, "mark": 16.2},
    ]
    leap = ind.find_leap_strike(contracts, 145.0)
    assert leap["strike"] == 130.0 and leap["dte"] == 178
    assert leap["intrinsic"] == pytest.approx(15.0)  # 145 - 130
    assert leap["extrinsic"] == pytest.approx(3.2)   # 18.2 - 15.0


def test_find_leap_strike_delta_fallback_when_greeks_missing():
    contracts = [
        {"strike": 110.0, "dte": 180, "delta": None, "bid": 36.0, "ask": 36.4},
        {"strike": 130.0, "dte": 180, "delta": None, "bid": 18.0, "ask": 18.4},
        {"strike": 145.0, "dte": 180, "delta": None, "bid": 6.0, "ask": 6.4},
    ]
    leap = ind.find_leap_strike(contracts, 145.0)
    # proxy ~= 145*(1-0.1)=130.5 -> nearest strike is 130
    assert leap["strike"] == 130.0


def test_get_nearby_strikes_flags_suggested():
    contracts = [
        {"strike": 68.0, "dte": 5, "bid": 5.0, "ask": 5.2},
        {"strike": 69.0, "dte": 5, "bid": 4.2, "ask": 4.4},
        {"strike": 70.0, "dte": 5, "bid": 3.4, "ask": 3.6},
        {"strike": 71.0, "dte": 5, "bid": 2.6, "ask": 2.8},
    ]
    rows = ind.get_nearby_strikes(contracts, 69.4, 72.0, count=3)
    assert [r["strike"] for r in rows] == [68.0, 69.0, 70.0]  # sorted ascending
    suggested = [r for r in rows if r["suggested"]]
    assert len(suggested) == 1 and suggested[0]["strike"] == 69.0  # closest to 69.4


def test_hist_vol_is_positive_annualized_pct():
    df = _frame(100 + np.cumsum(np.random.RandomState(7).normal(0, 1, 60)))
    hv = ind.hist_vol(df, 20)
    assert hv is not None and hv > 0
    assert ind.hist_vol(_frame([100, 101, 102]), 20) is None  # too little history


def test_detect_action_follows_position_state():
    import option_chain as oc
    assert oc._detect_action(has_leap=False, open_shorts=[])[0] == "buy_leap"
    assert oc._detect_action(has_leap=True, open_shorts=[])[0] == "sell_short"
    assert oc._detect_action(has_leap=True, open_shorts=[{"strike": 50}])[0] == "close_short"
    # Management-only (RED) — entries are off the table. Close the short first
    # if one is open, otherwise sell the LEAP to exit the long.
    assert oc._detect_action(True, [{"strike": 50}], management_only=True)[0] == "close_short"
    assert oc._detect_action(True, [], management_only=True)[0] == "close_leap"


def test_red_regime_blocks_entries_but_allows_managing_open_positions(monkeypatch):
    import option_chain as oc
    import data_handler
    import logging_handler as log
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "red"})

    # RED + nothing to manage -> blocked (no chain fetch, no entries possible).
    monkeypatch.setattr(log, "load_state", lambda: {"extrinsic_payback": {}})
    monkeypatch.setattr(log, "find_position", lambda s, t: None)
    with pytest.raises(oc.RegimeBlocked):
        oc.option_chain("ON")

    # RED + an open short -> management-only mode so the user can exit.
    df = _frame([100.0] * 60)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 100.0, "source": "test"})
    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {
        "status": "SUCCESS", "underlyingPrice": 100.0,
        "callExpDateMap": {"2026-07-02:5": {"100.0": [
            {"symbol": "X", "strikePrice": 100.0, "daysToExpiration": 5,
             "bid": 1.4, "ask": 1.8, "mark": 1.6, "delta": 0.5, "volatility": 40.0, "openInterest": 5}]}}})
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "ticker": "ON", "leap": {"strike": 80.0, "contracts": 5},
        "short_calls": [{"strike": 100.0, "contracts": 5, "dte": 3, "entry_extrinsic_per_share": 1.2}]})
    monkeypatch.setattr(log, "load_state", lambda: {"extrinsic_payback": {"ON": {"remaining_to_payback": 1000.0}}})

    out = oc.option_chain("ON")
    assert out["management_only"] is True
    assert out["suggested_action"] == "close_short"
    assert out["position"]["open_short"]["current_mark"] == 1.6


def test_existing_leap_matches_stored_expiration(monkeypatch):
    # Two contracts share strike 80 at different expirations; the held LEAP's
    # stored expiration must disambiguate which one values the position.
    import option_chain as oc
    import data_handler
    import logging_handler as log
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "green"})
    df = _frame([100.0] * 60)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 100.0, "source": "test"})
    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {
        "status": "SUCCESS", "underlyingPrice": 100.0,
        "callExpDateMap": {
            "2026-09-18:90": {"80.0": [{"symbol": "A", "strikePrice": 80.0, "daysToExpiration": 90,
                                        "bid": 21.0, "ask": 21.4, "mark": 21.2, "delta": 0.9}]},
            "2026-12-18:174": {"80.0": [{"symbol": "B", "strikePrice": 80.0, "daysToExpiration": 174,
                                         "bid": 22.0, "ask": 22.6, "mark": 22.3, "delta": 0.93}]},
            "2026-07-02:5": {"100.0": [{"symbol": "W", "strikePrice": 100.0, "daysToExpiration": 5,
                                        "bid": 1.4, "ask": 1.8, "mark": 1.6, "delta": 0.5}]},
        }})
    monkeypatch.setattr(log, "load_state", lambda: {"extrinsic_payback": {"ON": {"remaining_to_payback": 1000.0}}})
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "ticker": "ON", "short_calls": [],
        "leap": {"strike": 80.0, "contracts": 5, "cost_basis": 16500.0, "expiration": "2026-09-18"}})

    out = oc.option_chain("ON")
    el = out["position"]["existing_leap"]
    assert el["current_dte"] == 90 and el["current_mark"] == 21.2  # the Sep contract, not Dec


def test_iv_view_flags_rich_vs_cheap():
    import option_chain as oc
    assert oc._iv_view(weekly_iv=44.0, leap_iv=33.0, hv=20.0)["premium"] == "rich"
    assert oc._iv_view(weekly_iv=15.0, leap_iv=14.0, hv=20.0)["premium"] == "cheap"
    assert oc._iv_view(weekly_iv=21.0, leap_iv=20.0, hv=20.0)["premium"] == "fair"
    assert oc._iv_view(weekly_iv=None, leap_iv=None, hv=20.0)["premium"] == "unknown"


def test_insufficient_history_returns_none():
    df = _frame([1, 2, 3])
    assert ind.sma(df, 21) is None
    assert ind.atr(df, 9) is None


# ---- execute / ledger flow -------------------------------------------------
def test_execute_flow_builds_ledger(monkeypatch, tmp_path):
    # Isolate state to this test.
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib
    import logging_handler
    importlib.reload(logging_handler)
    import executor
    importlib.reload(executor)

    executor.execute({"action": "buy_leap", "ticker": "ON", "strike": 130,
                      "contracts": 5, "execution_price": 3300, "stock_price": 145,
                      "expiration": "2026-12-18"})
    executor.execute({"action": "sell_short", "ticker": "ON", "strike": 140.5,
                      "contracts": 5, "premium_per_share": 6.0, "stock_price": 145})
    res = executor.execute({"action": "close_short", "ticker": "ON", "strike": 140.5,
                            "contracts": 5, "close_price_per_share": 2.5, "stock_price": 142})

    state = logging_handler.load_state()
    # buy_leap: extrinsic_at_entry = (3300 - (145-130)*100) * 5 = (3300-1500)*5 = 9000
    assert state["extrinsic_payback"]["ON"]["leap_extrinsic_at_entry"] == 9000.0
    # sell short extrinsic = 6.0 - (145-140.5)=4.5 -> 1.5; close paid back = 2.5 - 1.5 = 1.0
    # net juice/share 0.5 * 5 * 100 = 250
    assert res["execution"]["net_juice_total"] == 250.0
    assert state["theta_ledger"]["totals"]["ytd"] == 250.0
    assert state["extrinsic_payback"]["ON"]["collected_to_date"] == 250.0
    # short was closed -> removed from the position
    pos = logging_handler.find_position(state, "ON")
    assert pos["short_calls"] == []
    # LEAP expiration is persisted at entry for exact close matching later.
    assert pos["leap"]["expiration"] == "2026-12-18"

    # The LEAP extrinsic is folded into the ledger as the income hurdle: only
    # $250 of the $9000 is filled, so the book is not yet income-positive.
    summary = state["theta_ledger"]["extrinsic_summary"]
    assert summary["leap_extrinsic_at_entry"] == 9000.0
    assert summary["remaining_to_payback"] == 8750.0
    assert summary["net_income"] == -8750.0
    assert summary["income_positive"] is False


def test_close_leap_clears_position_and_records_pnl(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib
    import logging_handler
    importlib.reload(logging_handler)
    import executor
    importlib.reload(executor)

    executor.execute({"action": "buy_leap", "ticker": "ON", "strike": 130,
                      "contracts": 5, "execution_price": 3300, "stock_price": 145})
    # Sell the LEAP to close: proceeds 3600*5=18000 vs cost basis 3300*5=16500.
    res = executor.execute({"action": "close_leap", "ticker": "ON", "strike": 130,
                            "contracts": 5, "close_price": 3600, "stock_price": 150})
    assert res["execution"]["realized_pnl"] == 1500.0
    # intrinsic/contract = (150-130)*100 = 2000; extrinsic remaining = (3600-2000)*5 = 8000
    assert res["execution"]["extrinsic_remaining"] == 8000.0

    state = logging_handler.load_state()
    pos = logging_handler.find_position(state, "ON")
    assert pos["leap"] is None
    assert pos["status"] == "closed"  # no shares or shorts left


def test_execute_rejects_bad_action():
    import executor
    with pytest.raises(ValueError):
        executor.execute({"action": "nope", "ticker": "ON"})


def test_rs3m_returns_native_float():
    # round() on a numpy scalar yields numpy.float64, whose comparisons produce
    # numpy.bool_ (not JSON serializable). rs3m must return a native float.
    n = 100
    bench = _frame([100.0] * n)
    strong = _frame([100 + i for i in range(n)])
    assert type(ind.rs3m(strong, bench)) is float


def test_entry_gate_is_json_serializable(monkeypatch):
    # Feed real numbers through the whole gate and assert the response has no
    # numpy types left (regression for "Object of type bool is not JSON
    # serializable").
    import json
    import data_handler
    import screening

    n = 120
    spy = _frame([100.0] * n)
    strong = _frame([100 + i * 0.8 for i in range(n)])

    def fake_get_daily(symbol, force=False):
        return spy if symbol.upper() == "SPY" else strong

    monkeypatch.setattr(data_handler, "get_daily", fake_get_daily)
    monkeypatch.setattr(data_handler, "get_many", lambda syms, force=False: {s.upper(): strong for s in syms})
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    screening._results.clear()  # bypass the TTL cache

    gate = screening.entry_gate("NVDA")
    # Must not raise:
    json.dumps(gate)
    assert all(isinstance(lv["pass"], bool) for lv in gate["levels"])


def test_entry_gate_level3_splits_spy_and_sector_legs(monkeypatch):
    # The user's scenario: a stock beats its sector (+3) but not SPY by enough
    # (+2 <= +5). Level 3 must FAIL overall, yet the sector sub-check must show
    # PASS — so the UI never reads a SPY-leg miss as "not beating the sector".
    import screening
    screening._results.clear()
    monkeypatch.setattr(screening, "regime",
                        lambda: {"status": "green", "breadth": 70, "vix": 15, "spy_trend": "up"})
    monkeypatch.setattr(screening, "sectors",
                        lambda: {"XLK": {"name": "Technology", "rs3m": 20, "breadth": 70,
                                         "atr_expanding": True, "status": "green"}})
    monkeypatch.setattr(screening, "_stock_row", lambda *a, **k: {
        "ticker": "NVDA", "sector": "XLK", "rs3m_vs_spy": 2.0, "rs3m_vs_sector": 3.0,
        "atr_pct": 3.0, "consolidating": True, "status": "wait"})

    gate = screening.entry_gate("NVDA")
    l3 = next(l for l in gate["levels"] if l["level"] == 3)
    spy_check, sector_check = l3["checks"]
    assert l3["pass"] is False         # combined fails
    assert spy_check["pass"] is False  # vs SPY +2 is not > +5
    assert sector_check["pass"] is True  # vs Sector +3 IS > 0 — the leg that confused the user


def test_filter_ready_requires_regime_and_sector(monkeypatch):
    # A stock can be strong + consolidating (gate Levels 3/4) yet not entry-ready
    # because the market regime or its sector isn't green. The filter's "ready"
    # must agree with the gate's READY TO ENTER, naming what blocks it.
    import data_handler
    import indicators
    import screening

    df = _frame([100.0] * 70)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(indicators, "rs3m", lambda d, b, **k: 12.0)   # stock vs SPY +12
    monkeypatch.setattr(indicators, "atr_pct", lambda d, **k: 2.0)
    monkeypatch.setattr(indicators, "consolidating", lambda d: True)

    # rs_vs_sector = 12 - 2 = +10 (> 0); stock leg passes, consolidating passes.
    weak_regime = screening._stock_row("NVDA", df, 2.0, "XLK", regime_green=False, sector_strong=True)
    assert weak_regime["stock_strong"] is True
    assert weak_regime["status"] == "wait"
    assert "regime" in weak_regime["blocked_by"]

    all_green = screening._stock_row("NVDA", df, 2.0, "XLK", regime_green=True, sector_strong=True)
    assert all_green["status"] == "ready"
    assert all_green["blocked_by"] == []
