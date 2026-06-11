from types import SimpleNamespace

import pandas as pd

from indicators import accumulation_distribution, accumulation_distribution_trend, compute_all, moving_average, rsi, rs3m_momentum_from_closes, rs3m_series, support_resistance, volume_acceleration, volume_ratio


def test_rs3m_matches_supplied_tos_ratio_formula():
    stock = pd.Series([130.0] + [140.0] * 62 + [153.0])
    spy = pd.Series([480.0] + [500.0] * 62 + [510.0])

    value = rs3m_series(stock, spy).iloc[-1]

    assert round(value, 2) == 10.77


def test_rs3m_momentum_matches_tos_rs3m_plot_lag_formula():
    spy = pd.Series([100.0] * 69)
    stock = pd.Series([100.0] * 69)
    stock.iloc[0] = 100.0    # rs[68], prior RS3M denominator
    stock.iloc[5] = 110.0    # rs[63], current RS3M denominator
    stock.iloc[-6] = 120.0   # rs[5], prior RS3M numerator
    stock.iloc[-1] = 132.0   # rs, current RS3M numerator

    value = rs3m_momentum_from_closes(stock, spy)

    assert round(value, 0) == 0


def test_rs3m_momentum_can_show_large_tos_cross_zero_values():
    spy = pd.Series([100.0] * 69)
    stock = pd.Series([100.0] * 69)
    stock.iloc[0] = 100.0
    stock.iloc[5] = 100.0
    stock.iloc[-6] = 99.0    # prior RS3M = -1
    stock.iloc[-1] = 119.0   # current RS3M = +19

    value = rs3m_momentum_from_closes(stock, spy)

    assert round(value, 0) == -2000


def test_volume_ratio_uses_latest_volume_over_current_20_day_average():
    volumes = pd.Series([
        5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075,
        5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075,
        8.2,
    ])

    value = volume_ratio(volumes)

    assert round(value, 1) == 156.8


def test_volume_acceleration_uses_latest_volume_over_current_5_day_average():
    volumes = pd.Series([100.0] * 15 + [110.0, 112.0, 114.0, 116.0, 118.0])

    value = volume_acceleration(volumes)

    assert round(value, 1) == 103.5


def test_accumulation_distribution_uses_standard_chaikin_formula():
    high = pd.Series([10.0, 12.0, 14.0])
    low = pd.Series([8.0, 10.0, 12.0])
    close = pd.Series([9.0, 11.5, 13.5])
    volume = pd.Series([100.0, 100.0, 100.0])

    values = accumulation_distribution(high, low, close, volume)

    assert values.round(1).tolist() == [0.0, 50.0, 100.0]


def test_accumulation_distribution_trend_reports_rising_state():
    index = pd.RangeIndex(30)
    high = pd.Series([10.0] * 30, index=index)
    low = pd.Series([8.0] * 30, index=index)
    close = pd.Series([9.0] * 20 + [9.8] * 10, index=index)
    volume = pd.Series([100.0] * 30, index=index)

    assert accumulation_distribution_trend(high, low, close, volume) == "rising"


def test_rsi_defaults_to_wilder_average_like_thinkorswim():
    closes = pd.Series([
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    ])

    value = rsi(closes)

    assert round(value, 2) == 70.46


def test_rsi_simple_method_preserves_latest_plain_average_formula():
    closes = pd.Series([
        150.00, 150.00, 151.50, 151.20, 152.80, 151.90, 153.50, 152.40,
        154.20, 153.60, 155.30, 154.50, 156.20, 155.80, 157.50,
    ])

    value = rsi(closes, method="simple")

    assert round(value, 1) == 73.9


def test_ma21_defaults_to_simple_moving_average():
    closes = pd.Series(range(1, 22), dtype=float)

    value = moving_average(closes, 21).iloc[-1]

    assert value == 11.0


def test_compute_all_exposes_all_five_key_indicators():
    index = pd.date_range("2026-01-01", periods=160, freq="D")
    close = pd.Series(range(100, 260), index=index, dtype=float)
    spy_close = pd.Series(range(400, 560), index=index, dtype=float)
    volume = pd.Series([100.0] * 150 + [100.0, 101.0, 99.0, 100.0, 102.0, 110.0, 112.0, 114.0, 116.0, 118.0], index=index)
    bars = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": volume}, index=index)
    spy_bars = pd.DataFrame({"Open": spy_close, "High": spy_close + 1, "Low": spy_close - 1, "Close": spy_close, "Volume": volume}, index=index)
    cfg = SimpleNamespace(RS3M_LOOKBACK=63, RS3M_MOM_WINDOW=10, MOM_SMOOTH=1, MOM_SCALE=1.0, RS3M_METHOD="ratio", RS3M_EMA_SPAN=1, RS3M_MOM_PAST_END_LAG=68, RS3M_MOM_PAST_LOOKBACK=131, RSI_METHOD="wilder", MA21_METHOD="sma")

    result = compute_all(bars, spy_bars, cfg)

    assert result["rs3m"] is not None
    assert result["rs3mMom"] is not None
    assert result["volRatio"] is not None
    assert result["volAccel"] is not None
    assert result["rsi"] is not None


def test_support_resistance_splits_zones_around_price():
    # Price oscillates between a ~100 floor and ~120 ceiling, then settles at 110.
    index = pd.date_range("2025-01-01", periods=160, freq="D")
    wave = [100, 105, 112, 120, 113, 106, 100, 107, 114, 120, 112, 104] * 14
    close = pd.Series(wave[:160], index=index, dtype=float)
    close.iloc[-1] = 110.0
    bars = pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1_000_000.0},
        index=index,
    )

    result = support_resistance(bars)

    assert result["price"] == 110.0
    assert result["support"] and all(z["center"] < 110.0 for z in result["support"])
    assert result["resistance"] and all(z["center"] >= 110.0 for z in result["resistance"])
    # Nearest-first ordering: closest zone leads each list.
    assert result["nearestSupport"] == result["support"][0]
    assert result["nearestResistance"] == result["resistance"][0]
    assert result["stop"] < result["nearestSupport"]["low"]
    assert result["breakoutTrigger"] > result["nearestResistance"]["high"]


def test_support_resistance_reports_insufficient_history():
    index = pd.date_range("2025-01-01", periods=8, freq="D")
    close = pd.Series(range(100, 108), index=index, dtype=float)
    bars = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1.0}, index=index)

    assert support_resistance(bars) == {"error": "insufficient history"}
