"""Kill switch tests — the RS3M-vs-SPY rule.

The RS3M-vs-Sector exit-now trigger, its YELLOW thinning half, and the sector-ETF
self-comparison waiver that existed to protect them were all removed 2026-08-21
(docs/decision-2026-08-21-remove-sector-rs.md). Their ABSENCE is asserted
positively in test_sector_rs_removed.py, not merely left uncovered."""
import os
import tempfile

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config  # noqa: E402
import data_handler  # noqa: E402
import kill_switch  # noqa: E402


def _ramp(n, slope):
    """A steady advance at ``slope`` per bar — the relative gradient between two
    of these is what rs3m reads over its 63-bar window."""
    return [100.0 + slope * i for i in range(n)]


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


def test_rs_spy_negative_triggers_red(monkeypatch):
    """The confirm-on-close SPY exit — the kill switch's only RED path since the
    sector trigger was removed (docs/decision-2026-08-21-remove-sector-rs.md)."""
    stock = _frame(_ramp(230, 0.02))          # lags SPY over the 63-bar window
    spy = _frame(_ramp(230, 0.20))
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda t, force=False: spy if t == config.BENCHMARK else stock)
    out = kill_switch.evaluate("AAPL")
    assert out["rs3m_vs_spy"] is not None and out["rs3m_vs_spy"] < 0
    assert out["status"] == "red" and out["alert"] is True
    assert "within 1-2 days" in out["suggested_action"]
    assert kill_switch.exit_reason_code(out) == "KILL_SWITCH_SPY"
    # The pair is gone: no sector value is computed or reported anywhere.
    assert "rs3m_vs_sector" not in out


def test_an_etf_position_needs_no_special_casing_now(monkeypatch):
    """Was test_rs_pair_waives_self_comparison_for_a_sector_etf_position. The
    self-comparison guard existed because a sector ETF compared against itself
    computed a tautological 0.0 that permanently tripped the YELLOW thinning
    leg. With the sector leg gone there is nothing to guard: an ETF is measured
    against SPY exactly like any other name."""
    df = _frame(_ramp(230, 0.20))
    spy = _frame(_ramp(230, 0.05))
    monkeypatch.setattr(data_handler, "get_daily", lambda t, force=False: df if t == "XLK" else spy)
    out = kill_switch.evaluate("XLK")
    assert "rs3m_vs_sector" not in out
    assert out["rs3m_vs_spy"] is not None
    assert out["status"] == "green"          # outperforming SPY -> hold, not a stuck YELLOW


def test_evaluate_all_skips_closed_positions():
    state = {"positions": [{"ticker": "AAPL", "status": "closed"}]}
    assert kill_switch.evaluate_all(state) == []
