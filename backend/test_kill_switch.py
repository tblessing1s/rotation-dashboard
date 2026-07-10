"""Kill switch tests — RS3M vs SPY / vs Sector thresholds, and the sector-ETF
self-comparison waiver (a position IN the sector ETF itself has no distinct
peer sector to compare against)."""
import os
import tempfile

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
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


def _lin(v0, v1, n=64):
    """A frame whose close moves linearly v0 -> v1 over n bars. rs3m only reads
    the endpoints (now = last, then = 63 bars back), so this fixes the growth
    factor exactly."""
    return _frame(list(pd.Series(range(n)).apply(lambda i: v0 + (v1 - v0) * i / (n - 1))))


def test_direct_vs_approx_rs_sector_parity_on_small_moves():
    """R2: on small moves the direct rs3m(stock, sector) and the old vs-SPY
    difference approximation agree to within a tight tolerance — the switch to
    direct changes nothing when moves are small."""
    import indicators as ind
    spy, xlk, stk = _lin(100, 101), _lin(100, 102), _lin(100, 103)
    approx = round(ind.rs3m(stk, spy) - ind.rs3m(xlk, spy), 2)
    direct = ind.rs3m(stk, xlk)
    assert abs(approx - direct) < 0.1   # parity: ~0.99 vs ~0.98


def test_kill_switch_direct_rs_sector_fires_where_approx_is_late(monkeypatch):
    """R2 [HARD_CFM_RULE]: on a LARGE sector move the difference approximation
    diverges from the true ratio, and the kill switch must consume the true
    ratio. Construct a red-hot sector (+30%) that the stock only barely leads:

      SPY -10%, sector +30%, stock +32.16%
        rs3m(stock, SPY)  = +46.84%   rs3m(sector, SPY) = +44.44%
        approx = 46.84 - 44.44 = +2.40%   (>= 2.0 -> the switch would read GREEN)
        direct = rs3m(stock, sector) = +1.66%   (< 2.0 -> YELLOW, thinning)

    The stock is only 1.66% ahead of its own sector — genuinely thinning toward
    the kill line — but the approximation says +2.40% and stays green. The kill
    switch now fires YELLOW on the true ratio where the approximation was late.
    (The RED zero-crossing sign always agrees between the two forms; the
    divergence that matters is exactly this thinning-band early warning.)"""
    import data_handler
    import earnings
    import indicators as ind
    import sector_data
    spy, xlk = _lin(100, 90), _lin(100, 130)
    stk = _lin(100, 132.16)

    frames = {"SPY": spy, "XLK": xlk, "ABC": stk}
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: frames[s.upper()])
    monkeypatch.setattr(sector_data, "sector_for", lambda t: "XLK")
    monkeypatch.setattr(earnings, "next_earnings", lambda t: {"date": None, "warning": False})

    out = kill_switch.evaluate("ABC")
    # The kill switch consumes the DIRECT figure and fires the thinning warning.
    assert out["rs3m_vs_sector"] == 1.66
    assert out["status"] == "yellow"
    assert "thinning" in out["suggested_action"].lower()

    # Prove the approximation would have been LATE: recomputed here, it reads
    # +2.40% which does NOT clear the < 2.0 thinning band -> it would have stayed
    # green while the position was really thinning against a red-hot sector.
    approx = round(ind.rs3m(stk, spy) - ind.rs3m(xlk, spy), 2)
    assert approx == 2.40 and approx >= config.STOCK_RS_VS_SECTOR_MIN + 2
    assert out["rs3m_vs_sector"] < config.STOCK_RS_VS_SECTOR_MIN + 2


def test_evaluate_all_skips_closed_positions():
    state = {"positions": [{"ticker": "AAPL", "status": "closed"}]}
    assert kill_switch.evaluate_all(state) == []
