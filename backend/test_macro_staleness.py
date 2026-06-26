"""Macro freshness must reflect the last successful ingestion, not the date a
slow-moving observation was first stored. Regression guard for the bug where
quarterly GDP / monthly CPI read as red-stale forever because their value
never changes between daily ingestion runs."""
import pandas as pd

import db


def _series(dates, values):
    return pd.Series(values, index=pd.to_datetime(dates), dtype=float)


def test_unchanged_series_refreshes_fetched_at(fresh_db, monkeypatch):
    stamps = iter([
        "2026-01-01T00:00:00Z",  # first ingest
        "2026-06-26T00:00:00Z",  # re-ingest months later, same value
    ])
    monkeypatch.setattr(db, "utcnow", lambda: next(stamps))

    # Quarterly GDP: one observation, stored back in January.
    written = db.append_macro_series("GDPC1", _series(["2026-01-01"], [23000.0]), "alphavantage")
    assert written == 1
    assert db.get_macro_series("GDPC1").attrs["fetched_at"] == "2026-01-01T00:00:00Z"

    # Daily ingestion in June re-fetches the same value: no new row, but the
    # recency stamp must advance so the staleness gate stays fresh.
    written = db.append_macro_series("GDPC1", _series(["2026-01-01"], [23000.0]), "alphavantage")
    assert written == 0
    assert db.get_macro_series("GDPC1").attrs["fetched_at"] == "2026-06-26T00:00:00Z"


def test_new_observation_still_appends(fresh_db, monkeypatch):
    stamps = iter(["2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z"])
    monkeypatch.setattr(db, "utcnow", lambda: next(stamps))

    db.append_macro_series("CPIAUCSL", _series(["2026-03-01"], [300.0]), "alphavantage")
    written = db.append_macro_series(
        "CPIAUCSL", _series(["2026-03-01", "2026-04-01"], [300.0, 301.5]), "alphavantage"
    )
    assert written == 1  # only the new April observation
    s = db.get_macro_series("CPIAUCSL")
    assert list(s.values) == [300.0, 301.5]
    assert s.attrs["fetched_at"] == "2026-07-01T00:00:00Z"  # latest date's stamp


def test_active_source_wins_over_stale_legacy_rows(fresh_db, monkeypatch):
    """After the FRED->Alpha Vantage migration the store can still hold legacy
    FRED rows. Alpha Vantage's economic endpoints often lag FRED by a period, so
    a dead FRED observation can carry a *newer* date than Alpha Vantage's latest.
    The series must follow the freshly ingested source, not let that stale FRED
    row win the most-recent slot and freeze both the value and its fetched_at."""
    # Legacy FRED GDP, ingested long ago, with a date AHEAD of Alpha Vantage's.
    monkeypatch.setattr(db, "utcnow", lambda: "2026-01-15T00:00:00Z")
    db.append_macro_series("GDPC1", _series(["2026-01-01"], [22000.0]), "fred")

    # Alpha Vantage now ingests current quarterly GDP (one quarter behind FRED's
    # last print) with a fresh fetched_at.
    monkeypatch.setattr(db, "utcnow", lambda: "2026-06-26T00:00:00Z")
    db.append_macro_series(
        "GDPC1", _series(["2025-07-01", "2025-10-01"], [21800.0, 21950.0]), "alphavantage"
    )

    s = db.get_macro_series("GDPC1")
    # The Alpha Vantage observations win wholesale — the stale 2026-01-01 FRED
    # value is NOT the latest, and the recency stamp reflects the AV ingestion.
    assert list(s.values) == [21800.0, 21950.0]
    assert str(s.index[-1].date()) == "2025-10-01"
    assert s.attrs["source"] == "alphavantage"
    assert s.attrs["fetched_at"] == "2026-06-26T00:00:00Z"
