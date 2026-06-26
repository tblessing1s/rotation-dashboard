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
