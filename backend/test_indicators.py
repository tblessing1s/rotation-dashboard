from types import SimpleNamespace

import pandas as pd

from indicators import compute_all, rsi, rs3m_momentum, rs3m_series, volume_acceleration, volume_ratio


def test_rs3m_matches_supplied_90_day_formula():
    stock = pd.Series([130.0] + [140.0] * 89 + [153.0])
    spy = pd.Series([480.0] + [500.0] * 89 + [510.0])

    value = rs3m_series(stock, spy, lookback=90).iloc[-1]

    assert round(value, 2) == 11.44


def test_rs3m_momentum_matches_supplied_10_day_average_formula():
    values = pd.Series([-15, -14, -12, -10, -8, -5, 0, 3, 8, 16.88])

    value = rs3m_momentum(values)

    assert round(value, 0) == 567


def test_volume_ratio_uses_latest_volume_over_prior_20_day_average():
    volumes = pd.Series([
        5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075,
        5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075, 5.075,
        8.2,
    ])

    value = volume_ratio(volumes)

    assert round(value, 1) == 161.6


def test_volume_acceleration_matches_supplied_current_vs_previous_5_day_formula():
    volumes = pd.Series([5.0, 5.1, 4.9, 5.0, 5.2, 5.1, 5.3, 5.5, 5.7, 5.9])

    value = volume_acceleration(volumes)

    assert round(value, 1) == 109.1


def test_rsi_matches_supplied_simple_average_gain_loss_formula():
    closes = pd.Series([
        150.00, 150.00, 151.50, 151.20, 152.80, 151.90, 153.50, 152.40,
        154.20, 153.60, 155.30, 154.50, 156.20, 155.80, 157.50,
    ])

    value = rsi(closes)

    assert round(value, 1) == 73.9


def test_compute_all_exposes_all_five_key_indicators():
    index = pd.date_range("2026-01-01", periods=100, freq="D")
    close = pd.Series(range(100, 200), index=index, dtype=float)
    spy_close = pd.Series(range(400, 500), index=index, dtype=float)
    volume = pd.Series([100.0] * 90 + [100.0, 101.0, 99.0, 100.0, 102.0, 110.0, 112.0, 114.0, 116.0, 118.0], index=index)
    bars = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": volume}, index=index)
    spy_bars = pd.DataFrame({"Open": spy_close, "High": spy_close + 1, "Low": spy_close - 1, "Close": spy_close, "Volume": volume}, index=index)
    cfg = SimpleNamespace(RS3M_LOOKBACK=90, RS3M_MOM_WINDOW=10, MOM_SMOOTH=1, RS3M_METHOD="return_spread", RS3M_EMA_SPAN=1)

    result = compute_all(bars, spy_bars, cfg)

    assert result["rs3m"] is not None
    assert result["rs3mMom"] is not None
    assert result["volRatio"] is not None
    assert result["volAccel"] is not None
    assert result["rsi"] is not None
