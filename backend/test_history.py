"""Phase 3 tests — closed-cycle derivation (hand-computed fixture), wash-sale
flagging, history aggregates, juice-journal export, and the calibration
harness over synthetic cached history."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import calibration  # noqa: E402
import config  # noqa: E402
import executor  # noqa: E402
import history  # noqa: E402
import logging_handler as log  # noqa: E402
import position_manager as pm  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _run_cycle(ticker="NVDA", exec_price=2400, close_price=2600, weeks=2,
               sold=0.80, paid=0.30, exit_reason="target hit", with_roll=False):
    executor.execute({"action": "buy_leap", "ticker": ticker, "strike": 75,
                      "contracts": 5, "execution_price": exec_price, "stock_price": 95,
                      "override_reason": "fixture"})
    for _ in range(weeks):
        executor.execute({"action": "sell_short", "ticker": ticker, "strike": 95,
                          "contracts": 5, "premium_per_share": sold, "stock_price": 95})
        executor.execute({"action": "close_short", "ticker": ticker, "strike": 95,
                          "contracts": 5, "close_price_per_share": paid,
                          "stock_price": 94, "extrinsic_sold": sold})
    if with_roll:
        executor.execute({"action": "sell_short", "ticker": ticker, "strike": 95,
                          "contracts": 5, "premium_per_share": 0.50, "stock_price": 95})
        executor.execute({"action": "roll_short", "ticker": ticker, "contracts": 5,
                          "from_strike": 95, "close_price_per_share": 2.00,
                          "to_strike": 90, "to_dte": 7, "premium_per_share": 1.50,
                          "stock_price": 93, "roll_reason": "defend"})
        # close the rolled short flat so the position can fully close
        executor.execute({"action": "close_short", "ticker": ticker, "strike": 90,
                          "contracts": 5, "close_price_per_share": 1.50,
                          "stock_price": 89, "extrinsic_sold": 1.50})
    executor.execute({"action": "close_leap", "ticker": ticker, "strike": 75,
                      "contracts": 5, "close_price": close_price, "stock_price": 98,
                      "exit_reason": exit_reason})


def test_cycle_record_hand_computed(isolated_state):
    _run_cycle()
    state = log.load_state()
    assert log.find_position(state, "NVDA")["status"] == "closed"
    cycles = state["cycles"]
    assert len(cycles) == 1
    c = cycles[0]
    # Hand-computed: capital = 2400*5 = 12000; juice = (0.80-0.30)*5*100*2 = 500;
    # LEAP P&L = (2600-2400)*5 = 1000; net = 1500 -> 12.5% (< 15% target).
    assert c["ticker"] == "NVDA"
    assert c["capital_deployed"] == 12000.0
    assert c["gross_juice"] == 500.0
    assert c["leap_pnl"] == 1000.0
    assert c["net_result"] == 1500.0
    assert c["net_return_pct"] == 12.5
    assert c["target_met"] is False and c["target_range_pct"] == [15.0, 25.0]
    assert c["exit_reason"] == "target hit"
    assert c["days_held"] == 0  # same-day fixture
    assert c["roll_count"] == 0 and c["roll_drag"] == 0.0
    assert "entry_snapshot" in c  # captured at entry (None-fields offline is fine)
    assert c["wash_sale"] is None  # profitable exit


def test_cycle_includes_roll_drag(isolated_state):
    _run_cycle(with_roll=True, close_price=2400, exit_reason="kill switch")
    c = log.load_state()["cycles"][0]
    # Roll: buyback 2.00*500=1000, new premium 1.50*500=750 -> net -250 (drag).
    assert c["roll_count"] == 1
    assert c["roll_net"] == -250.0 and c["roll_drag"] == -250.0
    assert c["exit_reason"] == "kill switch"
    # Juice: 2 weekly closes at +250 each, the roll's buyback close nets
    # (0.50-2.00)= -1.50/sh -> -750, the final close nets 0. Total = -250.
    assert c["gross_juice"] == -250.0
    assert c["leap_pnl"] == 0.0
    assert c["net_result"] == -250.0


def test_invalid_exit_reason_normalizes(isolated_state):
    _run_cycle(exit_reason="because I felt like it")
    assert log.load_state()["cycles"][0]["exit_reason"] == "discretionary"


def test_wash_sale_flagged_on_reentry(isolated_state):
    _run_cycle(close_price=2000, exit_reason="circuit breaker")  # LEAP P&L -2000 (loss)
    # Re-enter the same underlying (same day -> inside the 30d window).
    executor.execute({"action": "buy_leap", "ticker": "NVDA", "strike": 70,
                      "contracts": 5, "execution_price": 2500, "stock_price": 92,
                      "override_reason": "fixture reentry"})
    state = log.load_state()
    c = state["cycles"][0]
    assert c["leap_pnl"] == -2000.0
    assert c["wash_sale"]["status"] == "flagged"
    assert c["wash_sale"]["loss"] == -2000.0
    # The OPEN position carries the flag forward too.
    view = {p["ticker"]: p for p in pm.positions_view(state)}
    assert view["NVDA"]["wash_sale_flag"] is not None


def test_wash_sale_window_open_without_reentry(isolated_state):
    _run_cycle(close_price=2000, exit_reason="circuit breaker")
    c = log.load_state()["cycles"][0]
    assert c["wash_sale"]["status"] == "window_open"
    exit_d = datetime.now(timezone.utc).date()
    assert c["wash_sale"]["window_ends"] == (exit_d + timedelta(days=30)).isoformat()


def test_history_aggregates_and_export(isolated_state):
    _run_cycle(ticker="NVDA", close_price=2600)                    # +12.5% win
    _run_cycle(ticker="AMD", close_price=2000, exit_reason="kill switch")  # loss
    state = log.load_state()
    view = history.view(state)
    agg = view["aggregates"]
    assert agg["count"] == 2
    assert agg["win_rate"] == 50.0
    # returns: +12.5% and (500-2000)/12000 = -12.5% -> avg 0
    assert agg["avg_return_pct"] == 0.0
    assert agg["target_hit_rate"] == 0.0
    assert view["cycles"][0]["ticker"] == "AMD"  # newest first

    csv_text = history.juice_journal_csv(state)
    assert "# closed cycles" in csv_text and "NVDA" in csv_text and "kill switch" in csv_text
    md_text = history.juice_journal_markdown(state)
    assert "# CFM Juice Journal" in md_text and "| NVDA |" in md_text

    # The weekly-juice target band is 1-2%/wk of deployed capital, derived from
    # the open book (not a hand-set metadata figure). Add an open LEAP and the
    # band tracks its cost basis.
    state["positions"] = [{"ticker": "TSLA", "sector": "XLY", "status": "active",
                           "leap": {"strike": 100, "contracts": 5, "cost_basis": 20000},
                           "short_calls": [], "shares": {"count": 0}}]
    log.save_state(state)
    chart = history.view(log.load_state())["weekly_juice"]
    assert chart["capital_deployed"] == 20000
    assert chart["target_low"] == round(20000 * config.WEEKLY_JUICE_TARGET_PCT_MIN / 100, 2)


# ---- calibration harness --------------------------------------------------------
def _trend_frame(n=300, mu=0.001, seed=3, base=100.0):
    rng = np.random.RandomState(seed)
    close = base * np.exp(np.cumsum(rng.normal(mu, 0.01, n)))
    idx = pd.bdate_range("2024-01-01", periods=n)
    c = pd.Series(close, index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": rng.randint(1_000_000, 5_000_000, n).astype(float)}, index=idx)


def test_calibration_replays_cached_history(tmp_path, monkeypatch):
    import data_handler
    frames = {"SPY": _trend_frame(mu=0.0004, seed=1),
              "XLK": _trend_frame(mu=0.0006, seed=2),
              "NVDA": _trend_frame(mu=0.0012, seed=3),
              "AMD": _trend_frame(mu=-0.0008, seed=4)}
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: frames.get(s.upper()))
    rows = calibration.collect_rows(["NVDA", "AMD"], step=10)
    assert rows, "expected samples from 300 bars of history"
    assert {"fwd_4w", "fwd_8w", "verdict", "metrics"} <= set(rows[0])
    # Every sampled as-of leaves a full 8w forward window.
    assert all(isinstance(r["fwd_8w"], float) for r in rows)

    out = tmp_path / "report.md"
    text = calibration.run(["NVDA", "AMD"], step=10, out_path=str(out))
    assert out.exists()
    assert "# CFM Scorecard Calibration Report" in text
    assert "Forward returns by verdict" in text
    assert "ATR-extension cutoff" in text and "MFI band" in text
    # Threshold overrides restore cleanly.
    from metrics import thresholds as T
    assert T.ATR_EXTENSION_MAX == 3.0 and T.MFI_MIN == 40.0 and T.MFI_MAX == 60.0
