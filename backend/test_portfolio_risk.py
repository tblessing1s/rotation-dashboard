"""Phase 4 tests — portfolio risk aggregation (delta/beta/theta/vega, sector
exposure, reserve), the nightly maintenance refresh, its schedule trigger, and
the data-health surfaces."""
import os
import tempfile
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import alert_scheduler  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402
import maintenance  # noqa: E402
import portfolio_risk as pr  # noqa: E402


def _trend_frame(n=300, mu=0.0008, sigma=0.012, seed=5, base=100.0):
    rng = np.random.RandomState(seed)
    close = base * np.exp(np.cumsum(rng.normal(mu, sigma, n)))
    idx = pd.bdate_range("2024-01-01", periods=n)
    c = pd.Series(close, index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": 1e6}, index=idx)


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _scaled_frame(spy, factor, base=100.0):
    """A ticker whose returns are exactly factor x SPY's -> beta == factor."""
    ret = spy["Close"].pct_change().fillna(0.0)
    scaled = base * (1 + factor * ret).cumprod()
    return pd.DataFrame({"Open": scaled, "High": scaled * 1.01, "Low": scaled * 0.99,
                         "Close": scaled, "Volume": 1e6}, index=spy.index)


def test_beta_of_scaled_series_is_two():
    spy = _trend_frame(seed=7)
    assert pr.beta(_scaled_frame(spy, 2.0), spy) == pytest.approx(2.0, abs=0.01)
    assert pr.beta(None, spy) is None


def test_position_risk_diagonal_signs(monkeypatch):
    import data_handler
    spy = _trend_frame(seed=7)
    frames = {"SPY": spy, "NVDA": _scaled_frame(spy, 1.5, base=128.0)}
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: frames.get(s.upper()))
    price = float(frames["NVDA"]["Close"].iloc[-1])
    # Deep-ITM LEAP marked near intrinsic + time value; ITM weekly short.
    leap_mark = max(price - 90, 0) + 4.0
    short_mark = max(price - (price - 5), 0) + 1.0
    p = {"ticker": "NVDA", "sector": "XLK", "status": "active",
         "leap": {"strike": 90, "contracts": 5, "dte": 150,
                  "current_bid": leap_mark * 500, "cost_basis": 12000},
         "short_calls": [{"strike": price - 5, "contracts": 5, "dte": 5,
                          "current_bid": short_mark}],
         "shares": {"count": 100}}
    row = pr.position_risk(p, spy)
    assert row["greeks_complete"] is True
    # Long deep LEAP delta ~0.9+ minus short ITM weekly ~0.8 -> net positive,
    # plus the 100 shares.
    assert row["delta_shares"] > 100
    assert row["delta_dollars"] == pytest.approx(row["delta_shares"] * row["price"], rel=1e-3)
    # A diagonal with a shorter-dated short collects net theta.
    assert row["theta_per_day"] > 0
    # Long vega dominates (the LEAP has far more vega than the weekly).
    assert row["vega"] > 0
    assert row["beta"] == pytest.approx(1.5, abs=0.01)
    assert row["delta_dollars_spy_adj"] == pytest.approx(row["delta_dollars"] * 1.5, rel=0.01)


def test_portfolio_view_aggregates_and_sectors(isolated_state, monkeypatch):
    import data_handler
    spy = _trend_frame(seed=7)
    frames = {"SPY": spy,
              "NVDA": _scaled_frame(spy, 1.4, base=128.0),
              "XOM": _scaled_frame(spy, 0.7, base=110.0)}
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: frames.get(s.upper()))
    nvda_px = float(frames["NVDA"]["Close"].iloc[-1])
    xom_px = float(frames["XOM"]["Close"].iloc[-1])
    state = log.load_state()
    state["metadata"].update({"capital_deployed": 24000, "operating_cash": 20000})
    state["positions"] = [
        {"ticker": "NVDA", "sector": "XLK", "status": "active",
         "leap": {"strike": 90, "contracts": 5, "dte": 150,
                  "current_bid": (max(nvda_px - 90, 0) + 4.0) * 500, "cost_basis": 18000},
         "short_calls": [], "shares": {"count": 0}},
        {"ticker": "XOM", "sector": "XLE", "status": "active",
         "leap": {"strike": 80, "contracts": 5, "dte": 140,
                  "current_bid": (max(xom_px - 80, 0) + 3.0) * 500, "cost_basis": 6000},
         "short_calls": [], "shares": {"count": 0}},
        {"ticker": "OLD", "sector": "XLF", "status": "closed", "leap": None},
    ]
    log.save_state(state)

    view = pr.portfolio_view(log.load_state())
    assert len(view["positions"]) == 2  # closed position excluded
    assert view["totals"]["delta_dollars"] > 0
    assert view["capital"]["deployed"] == 24000
    assert view["capital"]["cap"] == config.MAX_DEPLOYED_CAPITAL
    sectors = {s["sector"]: s for s in view["sector_exposure"]}
    assert sectors["XLK"]["capital"] == 18000 and sectors["XLE"]["capital"] == 6000
    assert sectors["XLK"]["pct"] == 75.0
    assert view["capital"]["reserve_required"] > 0


# ---- maintenance ---------------------------------------------------------------
def test_nightly_refresh_updates_position_dividends(isolated_state, monkeypatch):
    import dividends
    import earnings
    monkeypatch.setattr(earnings, "next_earnings",
                        lambda t, refresh=False: {"ticker": t, "date": "2026-08-01"})
    monkeypatch.setattr(dividends, "next_dividend",
                        lambda t, refresh=False: {"ex_date": "2026-07-15", "amount": 0.41,
                                                  "source": "test"})
    state = log.load_state()
    state["positions"] = [
        {"ticker": "NVDA", "status": "active", "dividend": None},
        {"ticker": "GONE", "status": "closed", "dividend": None},
    ]
    log.save_state(state)

    report = maintenance.nightly_refresh()
    assert [e["ticker"] for e in report["tickers"]] == ["NVDA"]  # open only
    assert report["errors"] == []
    pos = log.find_position(log.load_state(), "NVDA")
    assert pos["dividend"]["ex_date"] == "2026-07-15"
    gone = log.find_position(log.load_state(), "GONE")
    assert gone["dividend"] is None  # closed positions untouched


def test_nightly_refresh_skips_demo(monkeypatch):
    monkeypatch.setattr(config, "_demo_mode", True)
    assert maintenance.nightly_refresh()["skipped"] == "demo mode"


def test_maintenance_due_once_per_day():
    et = alert_scheduler.ET
    evening = datetime(2026, 7, 1, 18, 0, tzinfo=et)
    assert alert_scheduler.maintenance_due(evening, None) is True
    assert alert_scheduler.maintenance_due(evening, date(2026, 7, 1)) is False
    assert alert_scheduler.maintenance_due(evening, date(2026, 6, 30)) is True
    morning = datetime(2026, 7, 1, 9, 0, tzinfo=et)
    assert alert_scheduler.maintenance_due(morning, None) is False
    # Weekend still runs (calendar updates publish any day).
    saturday = datetime(2026, 7, 4, 18, 0, tzinfo=et)
    assert alert_scheduler.maintenance_due(saturday, None) is True


# ---- data health ----------------------------------------------------------------
def test_data_handler_health_records_sources(monkeypatch):
    import data_handler
    data_handler._last_success.clear()
    data_handler._record_success("schwab_bars", "SPY")
    h = data_handler.health()
    assert h["sources"]["schwab_bars"]["symbol"] == "SPY"
    assert "fallback_events" in h and "recent_errors" in h
