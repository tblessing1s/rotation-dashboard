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
                      "contracts": 5, "execution_price": 3300, "stock_price": 145})
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


def test_execute_rejects_bad_action():
    import executor
    with pytest.raises(ValueError):
        executor.execute({"action": "nope", "ticker": "ON"})
