"""Kill switch tests — RS3M vs SPY / vs Sector thresholds, and the sector-ETF
self-comparison waiver (a position IN the sector ETF itself has no distinct
peer sector to compare against)."""
import os
import tempfile

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import kill_switch  # noqa: E402


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


def test_rs_pair_negative_sector_triggers_red(monkeypatch):
    # RS3M measures relative PERFORMANCE (return) over the lookback, not price
    # level — a declining series vs a flat benchmark, not merely a lower price.
    import data_handler
    import earnings
    import sector_data
    spy = _frame([100.0] * 70)
    weak = _frame([100 - i * 0.5 for i in range(70)])  # declining vs flat SPY/sector

    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: spy if s.upper() in ("SPY", "XLK") else weak)
    monkeypatch.setattr(sector_data, "sector_for", lambda t: "XLK")
    monkeypatch.setattr(earnings, "next_earnings", lambda t: {"date": None, "warning": False})

    out = kill_switch.evaluate("NVDA")
    assert out["rs3m_vs_sector"] is not None
    assert out["status"] == "red"
    assert "immediately" in out["suggested_action"]


def test_rs_pair_waives_self_comparison_for_a_sector_etf_position(monkeypatch):
    # A position IN the sector ETF itself (e.g. holding XLK via CFM) has no
    # distinct peer sector to compare against — the un-fixed math would
    # compute rs_vs_sector = rs_vs_spy - rs_vs_spy = 0 every time (same frame
    # vs itself), which is a REAL bug: the YELLOW "thinning" leg
    # (rs_vs_sector < STOCK_RS_VS_SECTOR_MIN + 2) would then fire permanently
    # since 0 is always < 2, flagging every healthy ETF position as thinning.
    import data_handler
    import earnings
    import sector_data
    strong = _frame([100 + i * 0.5 for i in range(70)])  # clearly outperforming SPY
    spy = _frame([100.0] * 70)

    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: spy if s.upper() == "SPY" else strong)
    monkeypatch.setattr(sector_data, "sector_for", lambda t: "XLK")  # XLK maps to itself
    monkeypatch.setattr(earnings, "next_earnings", lambda t: {"date": None, "warning": False})

    out = kill_switch.evaluate("XLK")
    assert out["rs3m_vs_sector"] is None       # waived, not a tautological 0
    assert out["status"] == "green"            # not falsely flagged yellow/red
    assert out["alert"] is False


def test_evaluate_all_skips_closed_positions():
    state = {"positions": [{"ticker": "AAPL", "status": "closed"}]}
    assert kill_switch.evaluate_all(state) == []
