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
    # 11 SPDR sectors + SPY (the Broad Market ETF group: QQQ/IWM/DIA).
    assert "XLK" in etfs and len(etfs) == 12
    assert "NVDA" in sector_data.constituents("XLK")
    assert sector_data.sector_for("NVDA") == "XLK"
    assert sector_data.sector_for("XLK") == "XLK"  # ETFs map to themselves


def test_all_tickers_includes_sector_etfs_as_candidates():
    # Sector ETFs are liquid, weekly-optionable tickers in their own right —
    # every scan (Scorecard, Ready-to-Enter, calibration) sweeps all_tickers(),
    # so including them here is what makes them selectable everywhere.
    names = sector_data.all_tickers()
    for etf in sector_data.sector_etfs():
        assert etf in names
    assert "NVDA" in names  # constituents are still present too
    assert len(names) == len(set(names))  # de-duplicated


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


def test_short_strike_from_table_takes_the_deeper_candidate():
    # price 150, ATR 4: ATR leg 1.0x -> 146; ITM leg 5% -> 142.5. ITM is deeper -> wins.
    assert ind.short_strike_from_table(150.0, 4.0, 1.0, 0.05) == 142.5
    # ATR leg 3.0x -> 138; ITM leg 1% -> 148.5. ATR is deeper -> wins.
    assert ind.short_strike_from_table(150.0, 4.0, 3.0, 0.01) == 138.0
    # 0 ATR / 0% ITM -> both candidates equal price -> ATM.
    assert ind.short_strike_from_table(150.0, 4.0, 0.0, 0.0) == 150.0


def test_black_scholes_delta_and_implied_vol_roundtrip():
    # ATM, 1y, r=0, sigma=0.20 -> delta = N(0.1) ≈ 0.5398
    assert ind.bs_call_delta(100, 100, 1.0, 0.0, 0.20) == pytest.approx(0.5398, abs=1e-3)
    # Price -> implied vol -> back to the same sigma.
    price = ind._bs_call_price(100, 100, 1.0, 0.0, 0.20)
    assert ind.implied_vol_call(price, 100, 100, 1.0, 0.0) == pytest.approx(0.20, abs=1e-3)
    # Below-intrinsic / nonsensical price -> no solution.
    assert ind.implied_vol_call(0.0, 100, 100, 1.0, 0.0) is None


def test_call_greeks_matches_tos_not_schwab_raw_delta():
    # AMD-like deep-ITM LEAP: Schwab's chain returned delta 0.88 (wrong); with its
    # reported IV (~77%) the Black–Scholes delta lands near TOS's ~0.93.
    d, iv = ind.call_greeks(521.58, 260, 174, 275.10, reported_iv=76.8)
    assert 0.92 < d < 0.96 and iv == 76.8
    # Delta must DECREASE as strike rises (the flat ~0.88 across strikes was the bug).
    d260, _ = ind.call_greeks(521.58, 260, 174, 275.10, reported_iv=76.8)
    d300, _ = ind.call_greeks(521.58, 300, 174, 241.38, reported_iv=76.1)
    assert d260 > d300
    # Falls back to mark-implied vol when no IV is reported.
    d_mark, iv_mark = ind.call_greeks(521.58, 260, 174, 275.10, reported_iv=None)
    assert d_mark is not None and iv_mark is not None
    # Insufficient inputs -> (None, None).
    assert ind.call_greeks(None, 260, 174, 275.10) == (None, None)


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


def test_get_leap_strikes_offers_band_and_flags_suggested():
    # Deeper ITM = higher delta / lower strike. Band is 0.88–0.91.
    contracts = [
        {"strike": 110.0, "dte": 178, "delta": 0.95, "bid": 36.0, "ask": 36.4},
        {"strike": 120.0, "dte": 178, "delta": 0.90, "bid": 27.0, "ask": 27.4},
        {"strike": 125.0, "dte": 178, "delta": 0.88, "bid": 22.0, "ask": 22.4},
        {"strike": 130.0, "dte": 178, "delta": 0.84, "bid": 18.0, "ask": 18.4},
        {"strike": 130.0, "dte": 30, "delta": 0.90, "bid": 16.0, "ask": 16.4},  # wrong DTE
    ]
    rows = ind.get_leap_strikes(contracts, 145.0)
    assert [r["strike"] for r in rows] == sorted(r["strike"] for r in rows)  # ascending
    assert all(r["dte"] == 178 for r in rows)                                # right expiration
    # The two in-band strikes (0.90, 0.88) are present; suggested is nearest 0.90.
    assert 120.0 in [r["strike"] for r in rows] and 125.0 in [r["strike"] for r in rows]
    sug = [r for r in rows if r["suggested"]]
    assert len(sug) == 1 and sug[0]["strike"] == 120.0 and sug[0]["delta"] == 0.90


def test_get_leap_strikes_pads_when_band_empty():
    # AMD-like: chain only lists 0.93 and 0.85 around the band — offer both so the
    # user can choose, suggesting the one nearest 0.90.
    contracts = [
        {"strike": 100.0, "dte": 180, "delta": 0.93, "bid": 44.0, "ask": 44.5},
        {"strike": 115.0, "dte": 180, "delta": 0.85, "bid": 30.0, "ask": 30.5},
    ]
    rows = ind.get_leap_strikes(contracts, 140.0)
    assert {r["strike"] for r in rows} == {100.0, 115.0}
    sug = next(r for r in rows if r["suggested"])
    assert sug["delta"] == 0.93  # closest available to 0.90


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


def test_schwab_account_methods_are_class_methods():
    # Regression: the chain parsers were once inserted mid-class, orphaning the
    # account/order methods as dead nested functions. They must be real methods.
    import schwab_api
    for name in ("account_numbers", "get_accounts", "preview_order", "place_order", "get_order"):
        assert hasattr(schwab_api.SchwabClient, name), name
    assert hasattr(schwab_api, "parse_call_chain") and hasattr(schwab_api, "parse_put_iv")


def test_parse_put_iv_maps_expiration_strike_to_iv():
    import schwab_api
    payload = {"putExpDateMap": {"2026-06-29:2": {
        "180.0": [{"strikePrice": 180.0, "volatility": 90.0}],
        "175.0": [{"strikePrice": 175.0, "volatility": "NaN"}],  # dropped
    }}}
    m = schwab_api.parse_put_iv(payload)
    assert m[("2026-06-29", 180.0)] == 90.0
    assert ("2026-06-29", 175.0) not in m


def test_itm_call_delta_uses_otm_put_iv(monkeypatch):
    # A 2-DTE ITM call with thin time value: its own IV collapses (delta -> ~1.0).
    # The recompute must instead use the OTM put's skew IV, pulling delta down to
    # a realistic level (this is what TOS shows).
    import option_chain as oc
    import data_handler
    import logging_handler as log
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "green"})
    df = _frame([192.5] * 60)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 192.5, "source": "t"})
    monkeypatch.setattr(log, "load_state", lambda: {"extrinsic_payback": {}})
    monkeypatch.setattr(log, "find_position", lambda s, t: None)
    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {
        "status": "SUCCESS", "underlyingPrice": 192.5,
        "callExpDateMap": {"2026-06-29:2": {"180.0": [
            {"symbol": "C", "strikePrice": 180.0, "daysToExpiration": 2,
             "bid": 11.20, "ask": 15.10, "mark": 13.15, "volatility": 15.0}]}},
        "putExpDateMap": {"2026-06-29:2": {"180.0": [
            {"symbol": "P", "strikePrice": 180.0, "daysToExpiration": 2,
             "bid": 0.50, "ask": 0.80, "mark": 0.65, "volatility": 90.0}]}},
    })
    out = oc.option_chain("ON")
    s180 = next(s for s in out["weekly"]["strikes"] if s["strike"] == 180.0)
    assert 0.80 < s180["delta"] < 0.90  # ~0.85 from the 90% put IV, not ~1.0


def test_weekly_short_skips_0dte_expiration(monkeypatch):
    # A chain that includes a same-day (0 DTE) expiration alongside a proper
    # week-out one must never pick the 0-DTE leg as "this week's short" — its
    # near-zero time value collapses delta to ~1.0 and is useless to sell fresh.
    import option_chain as oc
    import data_handler
    import logging_handler as log
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "green"})
    df = _frame([112.5] * 60)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 112.5, "source": "t"})
    monkeypatch.setattr(log, "load_state", lambda: {"extrinsic_payback": {}})
    monkeypatch.setattr(log, "find_position", lambda s, t: None)
    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {
        "status": "SUCCESS", "underlyingPrice": 112.5,
        "callExpDateMap": {
            "2026-07-02:0": {"105.0": [
                {"symbol": "C0", "strikePrice": 105.0, "daysToExpiration": 0,
                 "bid": 6.85, "ask": 8.30, "mark": 7.58, "delta": 1.0, "volatility": 45.1}]},
            "2026-07-10:5": {"105.0": [
                {"symbol": "C5", "strikePrice": 105.0, "daysToExpiration": 5,
                 "bid": 7.10, "ask": 8.50, "mark": 7.80, "volatility": 42.0}]},
        },
        "putExpDateMap": {},
    })
    out = oc.option_chain("ON")
    assert out["weekly"]["expiration"] == "2026-07-10"
    assert out["weekly"]["dte"] == 5


def test_weekly_offers_current_and_next_week(monkeypatch):
    # A fresh entry offers the current week AND the next week (capped at
    # WEEKLY_EXPIRATIONS_SHOWN) so the operator can choose which to sell; the
    # top-level weekly fields still mirror the current (first) week.
    import option_chain as oc
    import data_handler
    import logging_handler as log
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "green"})
    df = _frame([112.5] * 60)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 112.5, "source": "t"})
    monkeypatch.setattr(log, "load_state", lambda: {"extrinsic_payback": {}})
    monkeypatch.setattr(log, "find_position", lambda s, t: None)

    def _leg(sym, dte, bid, ask, mark):
        return {"symbol": sym, "strikePrice": 105.0, "daysToExpiration": dte,
                "bid": bid, "ask": ask, "mark": mark, "volatility": 42.0}

    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {
        "status": "SUCCESS", "underlyingPrice": 112.5,
        "callExpDateMap": {
            "2026-07-10:5": {"105.0": [_leg("C5", 5, 7.10, 8.50, 7.80)]},
            "2026-07-17:12": {"105.0": [_leg("C12", 12, 8.40, 9.90, 9.15)]},
            "2026-07-24:19": {"105.0": [_leg("C19", 19, 9.10, 10.60, 9.85)]},
        },
        "putExpDateMap": {},
    })
    out = oc.option_chain("ON")
    exps = out["weekly"]["expirations"]
    # Nearest-first, capped at 2 (the third week is dropped).
    assert [e["expiration"] for e in exps] == ["2026-07-10", "2026-07-17"]
    assert exps[0]["dte"] == 5 and exps[1]["dte"] == 12
    assert all(e["strikes"] for e in exps)
    # Back-compat: top-level mirrors the current (first) week.
    assert out["weekly"]["expiration"] == "2026-07-10"
    assert out["weekly"]["dte"] == 5


def test_implied_vol_put_roundtrip():
    p = ind._bs_put_price(117.7, 108.0, 2 / 365, 0.04, 0.90)
    assert ind.implied_vol_put(p, 117.7, 108.0, 2 / 365, 0.04) == pytest.approx(0.90, abs=1e-3)
    # Above the no-arbitrage ceiling -> no solution.
    assert ind.implied_vol_put(10_000, 117.7, 108.0, 2 / 365, 0.04) is None


def test_itm_call_delta_implied_from_put_mark_when_iv_missing(monkeypatch):
    # Off-hours (CSCO-like): Schwab returns NaN IV on the put, so the skew can't
    # come from its IV field. We imply it from the put's mark, or the deep-ITM
    # call's delta collapses toward 1.0 on the flat call IV. Spot 117.7, a 2-DTE
    # 108 call near intrinsic — TOS shows ~0.92, not ~0.99.
    import option_chain as oc
    import data_handler
    import logging_handler as log
    import screening

    monkeypatch.setattr(screening, "regime", lambda: {"status": "yellow"})
    df = _frame([117.7] * 60)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": 117.7, "source": "t"})
    monkeypatch.setattr(log, "load_state", lambda: {"extrinsic_payback": {}})
    monkeypatch.setattr(log, "find_position", lambda s, t: None)
    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {
        "status": "SUCCESS", "underlyingPrice": 117.7,
        "callExpDateMap": {"2026-07-02:2": {"108.0": [
            {"symbol": "C", "strikePrice": 108.0, "daysToExpiration": 2,
             "bid": 9.50, "ask": 9.85, "mark": 9.68, "volatility": 48.0}]}},
        "putExpDateMap": {"2026-07-02:2": {"108.0": [
            {"symbol": "P", "strikePrice": 108.0, "daysToExpiration": 2,
             "bid": 0.30, "ask": 0.34, "mark": 0.32, "volatility": "NaN"}]}},
    })
    out = oc.option_chain("CSCO")
    s108 = next(s for s in out["weekly"]["strikes"] if s["strike"] == 108.0)
    assert 0.85 < s108["delta"] < 0.95  # skew-aware (~0.91), not ~0.99 off a flat 48% IV


def test_dividend_yield_lowers_call_delta_and_roundtrips():
    # LEAP-like deep ITM: a 3% dividend yield should pull the call delta down a
    # bit (forgone dividends), and implying vol back must stay q-consistent.
    S, K, T, r, sigma = 117.7, 85.0, 171 / 365, 0.04, 0.45
    d_nodiv = ind.bs_call_delta(S, K, T, r, sigma, 0.0)
    d_div = ind.bs_call_delta(S, K, T, r, sigma, 0.03)
    assert d_div < d_nodiv and 0.005 < (d_nodiv - d_div) < 0.05
    # q defaults to 0 -> identical to the legacy no-dividend delta.
    assert ind.bs_call_delta(S, K, T, r, sigma) == d_nodiv
    # Dividend-consistent implied-vol roundtrips (call and put).
    cprice = ind._bs_call_price(S, K, T, r, sigma, 0.03)
    assert ind.implied_vol_call(cprice, S, K, T, r, 0.03) == pytest.approx(sigma, abs=1e-3)
    pprice = ind._bs_put_price(S, K, T, r, sigma, 0.03)
    assert ind.implied_vol_put(pprice, S, K, T, r, 0.03) == pytest.approx(sigma, abs=1e-3)


def test_dividend_yield_override_and_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib
    import dividends
    importlib.reload(dividends)
    # A hand-entered override given as a percent normalizes to a decimal.
    monkeypatch.setattr(dividends.log, "load_state",
                        lambda: {"metadata": {"dividend_overrides": {"CSCO": 3.0}}})
    assert dividends.yield_for("CSCO") == pytest.approx(0.03)
    # No override and no providers configured -> 0.0 (the safe no-op).
    monkeypatch.setattr(dividends.log, "load_state", lambda: {"metadata": {}})
    monkeypatch.setattr(dividends.schwab_api, "configured", lambda: False)
    monkeypatch.setattr(dividends.alpha_vantage, "configured", lambda: False)
    assert dividends.yield_for("ZZZZ", refresh=True) == 0.0


def test_coverage_floor_and_cover_checks(monkeypatch):
    # Delta guardrails for the PMCC diagonal: the LEAP must hold the 0.50 floor,
    # and the long's total delta must stay >= the short's (or the short is
    # uncovered). Stub the BS augmentation so we drive exact deltas.
    import option_chain as oc
    import schwab_api
    import logging_handler as log

    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(oc, "_fetch_chain", lambda t: {})
    contracts = [
        {"strike": 85.0, "expiration": "2026-12-18", "dte": 171, "delta": None},
        {"strike": 115.0, "expiration": "2026-07-02", "dte": 2, "delta": None},
    ]
    monkeypatch.setattr(schwab_api, "parse_call_chain", lambda p: (117.7, contracts))
    monkeypatch.setattr(log, "load_state", lambda: {})
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "ticker": "CSCO", "status": "active",
        "leap": {"strike": 85.0, "contracts": 5, "expiration": "2026-12-18"},
        "short_calls": [{"strike": 115.0, "contracts": 5, "expiration": "2026-07-02"}]})

    def set_deltas(leap_d, short_d):
        def fake(payload, cs, underlying, ticker):
            for c in cs:
                c["delta"] = leap_d if c["strike"] == 85.0 else short_d
        monkeypatch.setattr(oc, "_augment_call_greeks", fake)

    # Healthy: LEAP 0.90 ≥ floor and ≥ short 0.70 -> covered, green.
    set_deltas(0.90, 0.70)
    ok = oc.coverage("CSCO")
    assert ok["status"] == "green" and ok["covered"] is True
    assert ok["leap"]["delta"] == 0.90 and ok["shorts"][0]["delta"] == 0.70

    # LEAP below the 0.50 floor -> red (even though still nominally covering).
    set_deltas(0.45, 0.40)
    floor = oc.coverage("CSCO")
    assert floor["status"] == "red" and floor["alert"] is True
    assert "below 0.50" in floor["message"]

    # Short delta exceeds the LEAP's -> uncovered, red.
    set_deltas(0.80, 0.85)
    unc = oc.coverage("CSCO")
    assert unc["status"] == "red" and unc["covered"] is False
    assert "exceeds the LEAP" in unc["message"]


def test_coverage_unknown_without_schwab(monkeypatch):
    import option_chain as oc
    import schwab_api
    import logging_handler as log
    monkeypatch.setattr(log, "load_state", lambda: {})
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "ticker": "ON", "status": "active", "leap": {"strike": 80.0, "contracts": 5},
        "short_calls": []})
    monkeypatch.setattr(schwab_api, "configured", lambda: False)
    assert oc.coverage("ON")["status"] == "unknown"


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
                      "expiration": "2026-12-18", "override_reason": "test fixture"})
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
                      "contracts": 5, "execution_price": 3300, "stock_price": 145,
                      "override_reason": "test fixture"})
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


def test_execute_reports_filled_status(monkeypatch, tmp_path):
    # The paper/logged path commits immediately, so the response status is
    # "filled" — the frontend toasts a success on it (a future live path returns
    # "working" and resolves the fill / auto-cancel asynchronously).
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib
    import logging_handler
    importlib.reload(logging_handler)
    import executor
    importlib.reload(executor)

    res = executor.execute({"action": "buy_leap", "ticker": "ON", "strike": 130,
                            "contracts": 5, "execution_price": 3300, "stock_price": 145,
                            "override_reason": "test fixture"})
    assert res["status"] == "filled" and res["mode"] == "logged"


# ---- live order ticket + place/poll/cancel lifecycle -----------------------
def test_occ_symbol_and_order_ticket():
    import schwab_api
    assert schwab_api.occ_option_symbol("AAPL", "2024-09-20", 250) == "AAPL  240920C00250000"
    sym = schwab_api.occ_option_symbol("ON", "2026-07-10", 139.5)
    assert sym == "ON    260710C00139500"  # 6-char root, half-strike ×1000
    order = schwab_api.build_single_leg_order("SELL_TO_OPEN", 5, sym, 6.0)
    assert order["orderType"] == "LIMIT" and order["price"] == "6.00" and order["duration"] == "DAY"
    leg = order["orderLegCollection"][0]
    assert leg["instruction"] == "SELL_TO_OPEN" and leg["quantity"] == 5
    assert leg["instrument"] == {"symbol": sym, "assetType": "OPTION"}


class _FakeSchwab:
    """Minimal stand-in for the live Schwab client used by the order lifecycle."""
    def __init__(self, status="WORKING", fill_price=None):
        self._status, self._fill_price = status, fill_price
        self.placed = self.canceled = None
    def primary_account_hash(self):
        return "HASH"
    def place_order(self, account_hash, order):
        self.placed = (account_hash, order)
        return {"orderId": "ORD1"}
    def get_order(self, account_hash, order_id):
        out = {"status": self._status}
        if self._fill_price is not None:
            out["orderActivityCollection"] = [{"executionLegs": [{"price": self._fill_price}]}]
        return out
    def cancel_order(self, account_hash, order_id):
        self.canceled = (account_hash, order_id)
        return {"canceled": True}


def _live_executor(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib
    import logging_handler
    importlib.reload(logging_handler)
    import executor
    importlib.reload(executor)
    import data_handler
    import schwab_api
    monkeypatch.setattr(executor, "live_enabled", lambda: True)
    monkeypatch.setattr(schwab_api, "configured", lambda: True)
    monkeypatch.setattr(data_handler, "client", lambda: fake)
    return executor, logging_handler


def test_live_order_places_then_fills_and_commits(monkeypatch, tmp_path):
    fake = _FakeSchwab(status="FILLED", fill_price=5.0)
    executor, log = _live_executor(monkeypatch, tmp_path, fake)

    res = executor.execute({"action": "sell_short", "ticker": "ON", "strike": 139.5,
                            "contracts": 5, "premium_per_share": 6.0, "stock_price": 142,
                            "expiration": "2026-07-10"})
    # Placed a working order — nothing committed to state yet.
    assert res["status"] == "working" and res["order_id"] == "ORD1"
    assert res["option_symbol"] == "ON    260710C00139500"
    assert fake.placed[1]["orderLegCollection"][0]["instruction"] == "SELL_TO_OPEN"
    state = log.load_state()
    assert state["executions"] == [] and "ORD1" in state["pending_orders"]

    # Poll: it filled → commit at the real 5.0 fill (not the 6.0 limit) and clear.
    st = executor.order_status("ORD1")
    assert st["status"] == "filled"
    state = log.load_state()
    assert "ORD1" not in state["pending_orders"]
    pos = log.find_position(state, "ON")
    assert len(pos["short_calls"]) == 1 and pos["short_calls"][0]["strike"] == 139.5
    assert state["executions"][-1]["premium_per_share"] == 5.0


def test_live_order_cancel_clears_pending(monkeypatch, tmp_path):
    fake = _FakeSchwab(status="WORKING")
    executor, log = _live_executor(monkeypatch, tmp_path, fake)

    res = executor.execute({"action": "sell_short", "ticker": "ON", "strike": 139.5,
                            "contracts": 5, "premium_per_share": 6.0, "stock_price": 142,
                            "expiration": "2026-07-10"})
    assert res["status"] == "working"
    assert executor.order_status("ORD1")["status"] == "working"  # not filled yet

    cancelled = executor.cancel_order("ORD1")
    assert cancelled["status"] == "canceled"
    assert fake.canceled == ("HASH", "ORD1")
    state = log.load_state()
    assert "ORD1" not in state["pending_orders"] and state["executions"] == []


def test_roll_short_closes_old_and_opens_new(monkeypatch, tmp_path):
    # A roll is one operation: buy to close the old short and sell a new one at a
    # freely chosen week + strike. Both legs are logged; the position ends with a
    # single short at the new strike/expiration and the close books net juice.
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib
    import logging_handler
    importlib.reload(logging_handler)
    import executor
    importlib.reload(executor)

    executor.execute({"action": "buy_leap", "ticker": "ON", "strike": 130,
                      "contracts": 5, "execution_price": 3300, "stock_price": 145,
                      "override_reason": "test fixture"})
    executor.execute({"action": "sell_short", "ticker": "ON", "strike": 140.5,
                      "contracts": 5, "premium_per_share": 6.0, "stock_price": 145,
                      "expiration": "2026-07-03"})
    res = executor.execute({
        "action": "roll_short", "ticker": "ON", "contracts": 5,
        "from_strike": 140.5, "close_price_per_share": 2.5,
        "to_strike": 139.0, "premium_per_share": 5.0,
        "to_expiration": "2026-07-10", "to_dte": 7, "stock_price": 142})

    # net credit = new premium total (5.0*5*100=2500) − buyback (2.5*5*100=1250)
    assert res["net_credit"] == 1250.0
    assert [e["roll_leg"] for e in res["executions"]] == ["close", "open"]

    state = logging_handler.load_state()
    pos = logging_handler.find_position(state, "ON")
    assert len(pos["short_calls"]) == 1
    new = pos["short_calls"][0]
    assert new["strike"] == 139.0 and new["expiration"] == "2026-07-10" and new["dte"] == 7
    # Closing the 140.5 (sold extrinsic 1.5, paid back 1.0) books 0.5/sh*5*100=250.
    assert state["theta_ledger"]["totals"]["ytd"] == 250.0
    assert state["extrinsic_payback"]["ON"]["collected_to_date"] == 250.0


def test_roll_short_requires_both_strikes(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    import importlib
    import logging_handler
    importlib.reload(logging_handler)
    import executor
    importlib.reload(executor)
    with pytest.raises(ValueError):
        executor.execute({"action": "roll_short", "ticker": "ON", "contracts": 5,
                          "from_strike": 140.5, "stock_price": 142})


# ---- earnings --------------------------------------------------------------
def test_earnings_summary_flags_warning_window():
    import earnings
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=3)).isoformat()
    far = (date.today() + timedelta(days=40)).isoformat()
    assert earnings._summary("ON", soon, "x")["warning"] is True
    assert earnings._summary("ON", soon, "x")["days_until"] == 3
    assert earnings._summary("ON", far, "x")["warning"] is False
    assert earnings._summary("ON", None, "x")["date"] is None


def test_earnings_override_beats_provider(monkeypatch):
    import earnings
    from datetime import date, timedelta
    d = (date.today() + timedelta(days=2)).isoformat()
    monkeypatch.setattr(earnings, "_override", lambda t: d)
    out = earnings.next_earnings("ON")
    assert out["source"] == "override" and out["date"] == d and out["warning"] is True


def test_earnings_unknown_when_provider_unconfigured(monkeypatch):
    import alpha_vantage
    import earnings
    monkeypatch.setattr(earnings, "_override", lambda t: None)
    monkeypatch.setattr(alpha_vantage, "configured", lambda: False)
    out = earnings.next_earnings("ZZZZ", refresh=True)
    assert out["date"] is None and out["source"] == "alpha_vantage"


def test_earnings_calendar_parses_soonest_first(monkeypatch):
    import alpha_vantage
    csv_text = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
                "ON,ON Semiconductor,2026-07-28,2026-06-30,1.10,USD\n"
                "ON,ON Semiconductor,2026-10-27,2026-09-30,1.20,USD\n")
    monkeypatch.setattr(alpha_vantage, "_get_csv", lambda params, timeout=20: csv_text)
    rows = alpha_vantage.earnings_calendar("ON")
    assert len(rows) == 2 and rows[0]["reportDate"] == "2026-07-28"


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


def test_stock_row_waives_self_sector_leg_for_a_sector_etf(monkeypatch):
    # XLK entered as its own CFM candidate has no distinct peer sector to beat
    # — comparing it to itself would otherwise compute to a tautological 0
    # every time, permanently failing the "beats sector" leg. That leg must be
    # waived (N/A), not scored as a fail, and rs3m_vs_sector shown as None
    # rather than a misleading 0.00%.
    import data_handler
    import screening

    df = _frame([100.0] * 70)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(ind, "rs3m", lambda d, b, **k: 8.0)  # beats SPY either way
    monkeypatch.setattr(ind, "atr_pct", lambda d, **k: 2.0)
    monkeypatch.setattr(ind, "consolidating", lambda d: True)

    row = screening._stock_row("XLK", df, 8.0, "XLK", regime_green=True, sector_strong=True)
    assert row["is_sector_etf"] is True
    assert row["rs3m_vs_sector"] is None
    assert row["stock_strong"] is True    # waived, not failed
    assert "stock" not in row["blocked_by"]
    assert row["status"] == "ready"

    # A regular constituent (ticker != sector_etf) is unaffected.
    normal = screening._stock_row("NVDA", df, 8.0, "XLK", regime_green=True, sector_strong=True)
    assert normal["is_sector_etf"] is False
    assert normal["rs3m_vs_sector"] == 0.0  # 8 - 8, a REAL (if coincidental) number here


def test_entry_gate_level3_waives_sector_leg_for_a_sector_etf(monkeypatch):
    import data_handler
    import screening
    screening._results.clear()

    n = 260
    spy = _frame([100.0] * n)
    xlk = _frame([100 + i * 0.6 for i in range(n)])  # trending, consolidating-ish

    def fake_get_daily(symbol, force=False):
        return spy if symbol.upper() == "SPY" else xlk

    monkeypatch.setattr(data_handler, "get_daily", fake_get_daily)
    monkeypatch.setattr(data_handler, "get_many", lambda syms, force=False: {s.upper(): xlk for s in syms})
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    monkeypatch.setattr(screening, "regime",
                        lambda: {"status": "green", "breadth": 70, "vix": 15, "spy_trend": "up"})
    monkeypatch.setattr(screening, "sectors",
                        lambda: {"XLK": {"name": "Technology", "rs3m": 20, "breadth": 70,
                                         "atr_expanding": False, "status": "green"}})

    gate = screening.entry_gate("XLK")
    l3 = next(l for l in gate["levels"] if l["level"] == 3)
    spy_check, sector_check = l3["checks"]
    assert sector_check["pass"] is True            # waived, not a real fail
    assert "N/A" in sector_check["label"]
    assert spy_check["pass"] is True                # XLK genuinely beats SPY here
    assert l3["pass"] is True
    assert l3["detail"]["is_sector_etf"] is True
    assert l3["detail"]["rs3m_vs_sector"] is None


def test_rs_vs_spy_min_uses_a_lower_bar_for_etfs():
    import config
    assert config.rs_vs_spy_min(is_etf=False) == config.STOCK_RS_VS_SPY_MIN
    assert config.rs_vs_spy_min(is_etf=True) == config.STOCK_RS_VS_SPY_MIN_ETF
    assert config.STOCK_RS_VS_SPY_MIN_ETF < config.STOCK_RS_VS_SPY_MIN


def test_etf_clears_level3_spy_leg_on_the_lower_income_sleeve_bar(monkeypatch):
    # An ETF that merely leads SPY (+2%, below the +5% growth bar) must now clear
    # the Level 3 "beats SPY" leg — it runs as an income sleeve, not a growth
    # leader — while a regular stock at the same +2% still fails that leg.
    import data_handler
    import screening
    screening._results.clear()

    n = 260
    spy = _frame([100.0] * n)
    trend = _frame([100 + i * 0.3 for i in range(n)])
    monkeypatch.setattr(data_handler, "get_daily",
                        lambda s, force=False: spy if s.upper() == "SPY" else trend)
    monkeypatch.setattr(data_handler, "get_many", lambda syms, force=False: {s.upper(): trend for s in syms})
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    monkeypatch.setattr(ind, "rs3m", lambda d, b, **k: 2.0)  # +2% vs SPY for everything
    monkeypatch.setattr(ind, "atr_pct", lambda d, **k: 2.0)
    monkeypatch.setattr(ind, "consolidating", lambda d: True)
    monkeypatch.setattr(screening, "regime",
                        lambda: {"status": "green", "breadth": 70, "vix": 15, "spy_trend": "up"})
    monkeypatch.setattr(screening, "sectors",
                        lambda: {"XLK": {"name": "Technology", "rs3m": 20, "breadth": 70,
                                         "atr_expanding": False, "status": "green"}})

    # XLK (an ETF) clears the SPY leg on the >0% bar; the label reflects it.
    etf_l3 = next(l for l in screening.entry_gate("XLK")["levels"] if l["level"] == 3)
    spy_check = etf_l3["checks"][0]
    assert spy_check["pass"] is True
    assert "+0%" in spy_check["label"]

    # NVDA (a stock) at the same +2% still fails — the growth bar is unchanged.
    stock_l3 = next(l for l in screening.entry_gate("NVDA")["levels"] if l["level"] == 3)
    stock_spy_check = stock_l3["checks"][0]
    assert stock_spy_check["pass"] is False
    assert "+5%" in stock_spy_check["label"]


def test_stock_filter_includes_the_sector_etf_alongside_constituents(monkeypatch):
    import data_handler
    import screening
    screening._results.clear()

    df = _frame([100.0] * 70)
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "prefetch", lambda syms, force=False: None)
    monkeypatch.setattr(screening, "regime", lambda: {"status": "green"})
    monkeypatch.setattr(screening, "sectors", lambda: {"XLK": {"status": "green"}})
    monkeypatch.setattr(ind, "rs3m", lambda d, b, **k: 5.0)
    monkeypatch.setattr(ind, "atr_pct", lambda d, **k: 2.0)
    monkeypatch.setattr(ind, "consolidating", lambda d: True)

    rows = screening._compute_stock_filter("XLK")
    tickers = [r["ticker"] for r in rows]
    assert "XLK" in tickers  # the ETF is a row in its own sector's filter view
    etf_row = next(r for r in rows if r["ticker"] == "XLK")
    assert etf_row["is_sector_etf"] is True
    assert "NVDA" in tickers  # constituents are still present


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
