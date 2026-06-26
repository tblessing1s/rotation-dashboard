"""`cli.py macro` is the operator's macro-verification tool: it must print each
series' raw stored observations AND the value the calculators derive from them,
so the Fed/Growth/Inflation regime feed can be confirmed after an ingest."""
import argparse

import pandas as pd

import cli
import db


def _series(dates, values):
    return pd.Series(values, index=pd.to_datetime(dates), dtype=float)


def _seed_all(monkeypatch, stamp="2026-06-26T00:00:00Z"):
    monkeypatch.setattr(db, "utcnow", lambda: stamp)
    rate = list(pd.date_range("2026-03-01", periods=70, freq="D").strftime("%Y-%m-%d"))
    db.append_macro_series("DFF", _series(rate, [4.33] * 70), "alphavantage")
    cpi = list(pd.date_range("2025-03-01", periods=16, freq="MS").strftime("%Y-%m-%d"))
    db.append_macro_series("CPIAUCSL", _series(cpi, [round(300 * 1.002 ** i, 3) for i in range(16)]), "alphavantage")
    gdp = list(pd.date_range("2025-04-01", periods=4, freq="QS").strftime("%Y-%m-%d"))
    db.append_macro_series("GDPC1", _series(gdp, [22000, 22150, 22320, 22500]), "alphavantage")
    un = list(pd.date_range("2026-01-01", periods=6, freq="MS").strftime("%Y-%m-%d"))
    db.append_macro_series("UNRATE", _series(un, [4.2, 4.2, 4.1, 4.1, 4.1, 4.0]), "alphavantage")


def test_macro_command_prints_raw_obs_and_interpretation(fresh_db, capsys, monkeypatch):
    _seed_all(monkeypatch)
    rc = cli.cmd_macro(argparse.Namespace(observations=4))
    out = capsys.readouterr().out

    assert rc == 0
    # Raw observations are shown (the latest GDP quarter level + its date).
    assert "GDPC1" in out and "2026-01-01" in out and "22500" in out
    # And each series' derived value the calculators produce.
    assert "growth:" in out and "qoqAnnualized" in out
    assert "inflation YoY:" in out
    assert "funds rate:" in out
    assert "unemployment:" in out
    # Fed policy is a derived score across all four series — votes are shown.
    assert "Fed policy model:" in out
    assert "hawkish" in out or "dovish" in out


def test_macro_command_reports_missing_series(fresh_db, capsys, monkeypatch):
    # Only one series stored: the rest must report MISSING, not crash.
    monkeypatch.setattr(db, "utcnow", lambda: "2026-06-26T00:00:00Z")
    db.append_macro_series("DFF", _series(["2026-06-01"], [4.33]), "alphavantage")
    rc = cli.cmd_macro(argparse.Namespace(observations=6))
    out = capsys.readouterr().out

    assert rc == 0  # at least one series present
    assert "MISSING" in out  # the unseeded series are flagged, no traceback
    assert "GDPC1  (AV REAL_GDP):  MISSING" in out
