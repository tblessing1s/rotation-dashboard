import pandas as pd

from macro import classify_fed_policy


def _daily(values):
    return pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values), freq="D"), dtype=float)


def _monthly(values):
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="MS"), dtype=float)


def _quarterly(values):
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="QS"), dtype=float)


def test_fed_policy_turns_hawkish_when_current_conditions_are_hot():
    rates = _daily([5.25] * 70)
    cpi = _monthly([300, 301, 302, 303, 304, 305, 306, 307, 308, 309, 310, 311, 312, 314, 316, 318, 320])
    gdp = _quarterly([23000, 23100, 23220, 23450, 23750])
    unemployment = _monthly([4.0] * 17)

    result = classify_fed_policy(rates, cpi, gdp, unemployment)

    assert result["value"] == "hawkish"
    assert "inflation above 3%" in result["hawkishConditions"]
    assert "labor market tight" in result["hawkishConditions"]


def test_fed_policy_turns_dovish_when_current_conditions_are_cooling():
    rates = _daily([5.0] + [4.5] * 63)
    cpi = _monthly([300, 303, 306, 309, 312, 315, 318, 321, 324, 327, 330, 333, 337, 339, 340, 341, 342])
    gdp = _quarterly([24000, 24100, 24200, 24250, 24260])
    unemployment = _monthly([4.0] * 13 + [4.2, 4.4, 4.6, 4.8])

    result = classify_fed_policy(rates, cpi, gdp, unemployment)

    assert result["value"] == "dovish"
    assert "fed funds rate falling" in result["dovishConditions"]
    assert "labor market softening" in result["dovishConditions"]
