"""Per-name Genius stock lights, the right-spot gate, the vetoes, and the sector
RS1M gate — the stock-level-lights refactor. Fully offline: OHLCV is synthesized
or read from the committed regime fixtures; no provider is ever called.

The SAME four Genius lights power the market regime and the per-name stock lights
(one indicator system, fractal across market and stock). ``test_regime_regression``
already pins that the shared engine reproduces the SPY regime traces byte-for-byte
(Test 1); ``test_shared_engine_matches_regime_light_math`` below adds a direct
cross-check. The remaining tests cover the stock-level verdict mapping, the
separate right-spot gate, the vetoes, and the sector gate.
"""
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-stock-lights-"))

import config  # noqa: E402
import genius_lights  # noqa: E402
import indicators  # noqa: E402
import regime_genius  # noqa: E402
import stock_lights  # noqa: E402

FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regime")


def _frame(closes, hi=0.5, lo=0.5, highs=None, lows=None, start="2023-01-02"):
    """Ascending business-day OHLCV frame from a close path. High/Low default to a
    tight symmetric band around the close (they drive the Parabolic SAR)."""
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start=start, periods=len(closes))
    high = closes + hi if highs is None else np.asarray(highs, float)
    low = closes - lo if lows is None else np.asarray(lows, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({"Open": opens, "High": high, "Low": low, "Close": closes,
                         "Volume": np.full(len(closes), 1e6)}, index=idx)


def _load_fixture(name):
    return pd.read_parquet(os.path.join(FIX_DIR, f"{name}.parquet"))


# Canonical synthetic shapes, each hand-tuned to one light/spot pattern.
def _consolidation_frame():
    """4/4 green AND in the right spot: a slow steady rise (low extension,
    contracting/flat ATR, low ATR%) — enterable."""
    return _frame(100 + np.cumsum(np.full(230, 0.08)), hi=1.2, lo=1.2)


def _breakout_frame():
    """4/4 green but > 1.5 ATR extended above MA21 (a steep advance) — the lights
    are GREEN, the right-spot gate blocks."""
    return _frame(100 + np.cumsum(np.full(230, 0.35)), hi=0.4, lo=0.4)


def _three_green_frame():
    """Exactly 3 green: a long advance that goes flat for the last 12 bars, so
    ROC(10) == 0 (momentum RED) while the other three lights stay green."""
    c = list(100 + np.cumsum(np.full(220, 0.3)))
    return _frame(c + [c[-1]] * 12, hi=0.5, lo=0.5)


# ---------------------------------------------------------------------------
# Test 1 (companion) — the shared engine IS the regime light math.
# ---------------------------------------------------------------------------
def test_shared_engine_matches_regime_light_math():
    df = _load_fixture("distribution_rollover")
    eng = genius_lights.compute(df)
    # regime_genius re-exports the shared functions, so the lights/vote it computes
    # are the very same objects the shared engine returns — identical, not merely
    # equal. (test_regime_regression pins the full published-trace byte-identity.)
    assert regime_genius.compute_lights(df) == eng["lights"]
    v = regime_genius.vote(eng["lights"])
    assert v["green_count"] == eng["greens"]
    assert v["raw_condition"] == eng["color"]


# ---------------------------------------------------------------------------
# Test 2 — July 6 XLK fixture through the ETF path: two independent layers.
# ---------------------------------------------------------------------------
def test_july6_xlk_rollover_caught_by_both_layers():
    df = _load_fixture("xlk_july6_rollover")

    # Layer 1 — the four-light vote: the rollover flips SAR above price and drives
    # ROC(10) negative, so the last bar is not 4/4 green => verdict != GREEN.
    eng = genius_lights.compute(df)
    assert eng["lights"]["sar"]["signal"] == "red" or eng["lights"]["momentum"]["signal"] == "red"
    assert eng["greens"] < 4

    # Layer 2 — the ATR/IVR veto fires INDEPENDENTLY: the wide selloff bars expand
    # ATR, and paired with a rich IVR the veto trips -> RED. Run through the ETF
    # path (is_etf=True), where the rs3m-vs-sector veto is waived, so this veto is
    # the one that must catch it.
    assert indicators.atr_expanding(df) is True
    res = stock_lights.compute(df, sector_df=None, ivr_percentile=95.0, is_etf=True)
    assert res["verdict"] == stock_lights.RED
    assert "veto:atr_expanding_high_ivr" in res["veto_reasons"]
    # Both layers reach RED on their own: even with a benign IVR (veto disarmed),
    # the lights alone still deny GREEN.
    no_veto = stock_lights.compute(df, sector_df=None, ivr_percentile=10.0, is_etf=True)
    assert no_veto["verdict"] != stock_lights.GREEN


# ---------------------------------------------------------------------------
# Test 3 — breakout: 4/4 green lights, but the right-spot gate blocks.
# ---------------------------------------------------------------------------
def test_breakout_lights_green_but_right_spot_blocks():
    df = _breakout_frame()
    res = stock_lights.compute(df, sector_df=None, ivr_percentile=None, is_etf=False)
    assert res["greens"] == 4
    assert res["verdict"] == stock_lights.GREEN          # the LIGHTS are green...
    assert res["right_spot"]["pass"] is False            # ...but it's extended
    assert "spot:extension" in res["right_spot"]["blocked_by"]
    assert res["enterable"] is False                     # so it is not enterable


# ---------------------------------------------------------------------------
# Test 4 — consolidation: 4/4 green, in the right spot => enterable.
# ---------------------------------------------------------------------------
def test_consolidation_lights_green_and_in_right_spot_enterable():
    df = _consolidation_frame()
    res = stock_lights.compute(df, sector_df=None, ivr_percentile=None, is_etf=False)
    assert res["greens"] == 4
    assert res["verdict"] == stock_lights.GREEN
    assert res["right_spot"]["pass"] is True
    assert res["right_spot"]["blocked_by"] == []
    assert res["enterable"] is True                      # given a Level-5 pass, enterable end-to-end


# ---------------------------------------------------------------------------
# Test 5 — 3-green, no veto => YELLOW (watchlist), and absent from /api/scan/ready.
# ---------------------------------------------------------------------------
def test_three_green_no_veto_is_yellow_watchlist():
    df = _three_green_frame()
    res = stock_lights.compute(df, sector_df=None, ivr_percentile=None, is_etf=False)
    assert res["greens"] == 3
    assert res["vetoed"] is False
    assert res["verdict"] == stock_lights.YELLOW
    assert res["enterable"] is False                     # YELLOW is never enterable


def test_yellow_stock_absent_from_scan_ready(monkeypatch):
    """A YELLOW (3-green) name never appears on /api/scan/ready: the scorecard
    verdict short-circuits to AVOID on the Level-3 (stock lights) miss, so it is
    not a GO row and the ready endpoint never emits it."""
    import data_handler
    import screening
    import sector_data
    import weeklies
    import app as app_module
    from metrics import scorecard as sc

    yellow = _three_green_frame()
    spy = _frame(100 + np.cumsum(np.full(230, 0.05)), hi=0.5, lo=0.5)
    frames = {"SPY": spy, "XLK": spy, "NVDA": yellow}
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: frames.get(s.upper(), yellow))
    monkeypatch.setattr(data_handler, "prefetch", lambda *a, **k: None)
    monkeypatch.setattr(weeklies, "prefetch", lambda *a, **k: None)
    monkeypatch.setattr(weeklies, "has_weeklies", lambda t: True)
    monkeypatch.setattr(sector_data, "sector_for", lambda t: "XLK")
    monkeypatch.setattr(sector_data, "is_etf", lambda t: False)
    # A green regime + strong sector so ONLY the stock lights can block.
    green_lights = genius_lights.compute(spy)["lights"]
    monkeypatch.setattr(screening, "regime",
                        lambda: {"status": "green", "published_regime": "green", "lights": green_lights})
    monkeypatch.setattr(screening, "sectors",
                        lambda: {"XLK": {"name": "Tech", "rs1m": 5.0, "rs3m": 12.0,
                                         "breadth": 80.0, "atr_expanding": False, "status": "green"}})
    screening.clear_cache()

    # The gate does not clear Level 3 (stock lights), and the scorecard verdicts AVOID.
    gate = screening.entry_gate("NVDA")
    l3 = next(lv for lv in gate["levels"] if lv["level"] == 3)
    assert l3["pass"] is False
    row = sc.scorecard(["NVDA"])["results"][0]
    assert row["suitability"] != "GO"

    client = app_module.app.test_client()
    body = client.get("/api/scan/ready?tickers=NVDA").get_json()
    names = {e["ticker"] for e in body["ready"] + body["near_misses"] + body.get("stale_blocked", [])}
    assert "NVDA" not in names


# ---------------------------------------------------------------------------
# Test 6 — 4-green + rs3m_vs_sector < 0 (a stock) => RED via the veto.
# ---------------------------------------------------------------------------
def test_four_green_stock_vetoed_by_negative_rs3m_vs_sector():
    # The stock is in a clean uptrend (4/4 green) but has UNDER-performed its
    # sector over 63 bars (the sector ran harder), so rs3m(stock, sector) < 0.
    stock = _frame(100 + np.cumsum(np.full(230, 0.10)), hi=1.0, lo=1.0)
    sector = _frame(100 + np.cumsum(np.full(230, 0.50)), hi=1.0, lo=1.0)
    assert indicators.rs3m(stock, sector) < 0                   # underperformed its sector
    res = stock_lights.compute(stock, sector_df=sector, ivr_percentile=None, is_etf=False)
    assert res["greens"] == 4                                    # lights all green...
    assert res["vetoed"] is True
    assert "veto:rs3m_vs_sector" in res["veto_reasons"]
    assert res["verdict"] == stock_lights.RED                    # ...but the veto forces RED


# ---------------------------------------------------------------------------
# Test 7 — a stock and an ETF on IDENTICAL series get identical lights/spot.
# ---------------------------------------------------------------------------
def test_stock_and_etf_identical_series_identical_verdicts():
    df = _consolidation_frame()
    as_stock = stock_lights.compute(df, sector_df=None, ivr_percentile=None, is_etf=False)
    as_etf = stock_lights.compute(df, sector_df=None, ivr_percentile=None, is_etf=True)
    assert as_stock["lights"] == as_etf["lights"]
    assert as_stock["greens"] == as_etf["greens"]
    assert as_stock["verdict"] == as_etf["verdict"]
    assert as_stock["right_spot"] == as_etf["right_spot"]
    assert as_stock["enterable"] == as_etf["enterable"]


# ---------------------------------------------------------------------------
# Test 8 — sector gate: a STALE sector (RS3M high, RS1M < 0) => sector blocks.
# ---------------------------------------------------------------------------
def test_sector_gate_blocks_stale_sector_on_rs1m(monkeypatch):
    import data_handler
    import screening
    import sector_data

    # SPY flat. XLK rose hard over the 63-day window (RS3M high) but has turned
    # DOWN over the last month (RS1M < 0) — the laggy RS3M would keep it "strong",
    # the fresh RS1M gate catches the rollover.
    spy = _frame(np.full(108, 100.0), hi=0.2, lo=0.2)
    xlk_path = np.concatenate([
        np.linspace(100, 105, 46),         # gentle warm-up (history for the 63d window)
        np.linspace(105, 150, 42)[1:],     # steep advance INTO ~1 month ago (RS3M high)
        np.linspace(150, 130, 22)[1:],     # last ~month rolls over hard (RS1M < 0)
    ])
    xlk = _frame(xlk_path, hi=0.4, lo=0.4)
    frames = {"SPY": spy, "XLK": xlk}
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: frames.get(s.upper()))
    monkeypatch.setattr(data_handler, "get_many", lambda syms: {})
    monkeypatch.setattr(data_handler, "prefetch", lambda *a, **k: None)
    monkeypatch.setattr(sector_data, "sector_etfs", lambda: ["XLK"])
    monkeypatch.setattr(sector_data, "constituents", lambda e: [])
    monkeypatch.setattr(sector_data, "all_tickers", lambda: [])

    class _S:
        name = "Technology"
    monkeypatch.setattr(sector_data, "sectors", lambda: {"XLK": _S()})
    screening.clear_cache()

    out = screening.sectors()["XLK"]
    assert out["rs3m"] is not None and out["rs3m"] > config.SECTOR_RS3M_MIN   # would look "strong" on RS3M
    assert out["rs1m"] is not None and out["rs1m"] < 0                        # but RS1M has rolled over
    assert out["status"] != "green"                                          # so the sector gate blocks


# ---------------------------------------------------------------------------
# Test 9 — SAR determinism under canonical-start seeding.
# ---------------------------------------------------------------------------
def _light_colors_prefix(df, warm):
    """The stock light verdict color at each bar past the warm-up, each computed
    from the prefix df.iloc[:i+1] anchored at bar 0 (the canonical-start convention
    the backfill uses)."""
    out = []
    for i in range(warm, len(df)):
        out.append(stock_lights.compute(df.iloc[: i + 1])["verdict"])
    return out


def test_sar_determinism_same_seeding_convention_identical_colors():
    """Same series + same seeding convention (always anchor SAR at the earliest
    bar) => identical light colors over the overlap window, regardless of where a
    backfill run STOPS. Two runs that both anchor at bar 0 but end at different
    points agree on every shared bar — the property the per-name backfill relies on
    (mirrors the regime prefix-causality guarantee)."""
    df = _load_fixture("distribution_rollover")
    warm = config.STOCK_LIGHTS_WARMUP_BARS
    full = _light_colors_prefix(df, warm)
    assert full  # produced samples
    for cut in (len(df), len(df) - 20, len(df) - 40):
        partial = _light_colors_prefix(df.iloc[:cut], warm)
        assert partial == full[: len(partial)]


def test_sar_shifted_start_documents_the_boundary():
    """The determinism holds ONLY for the SAME first bar. A run that re-seeds from
    a LATER first bar (a rolling cache that dropped old bars) diverges right after
    the shift — which is exactly WHY the stock backfill must always anchor at each
    name's earliest cached bar, never a rolling sub-window."""
    df = _load_fixture("distribution_rollover")
    full = indicators.parabolic_sar(df)
    k = 120
    shifted = indicators.parabolic_sar(df.iloc[k:])
    assert abs(shifted[2] - full[k + 2]) > 0.5
