"""Level 1 macro via Alpha Vantage (FRED dependency removed)."""
import pandas as pd
import pytest

import ingest
import db
from providers import alphavantage
from providers.base import ProviderError


def _series():
    return pd.Series([4.0, 4.1], index=pd.to_datetime(["2026-04-01", "2026-05-01"]))


def test_ingest_macro_uses_alphavantage(monkeypatch, fresh_db):
    """Macro ingestion fetches all series from Alpha Vantage."""
    monkeypatch.setattr(alphavantage, "configured", lambda: True)
    monkeypatch.setattr(alphavantage, "economic_series", lambda sid, **k: _series())
    detail = {}
    ingest.ingest_macro_series(detail)
    # Should have successfully ingested 4 series (DFF, CPIAUCSL, GDPC1, UNRATE)
    assert len(detail["macro"]["ok"]) == 4
    assert len(detail["macro"]["failed"]) == 0


def test_ingest_macro_fails_when_av_unconfigured(monkeypatch, fresh_db):
    """Macro ingestion fails gracefully when Alpha Vantage is not configured."""
    monkeypatch.setattr(alphavantage, "configured", lambda: False)
    detail = {}
    ingest.ingest_macro_series(detail)
    # Should fail all series
    assert len(detail["macro"]["ok"]) == 0
    assert len(detail["macro"]["failed"]) == 4


def test_ingest_macro_partial_failure(monkeypatch, fresh_db):
    """Macro ingestion handles partial failures (some series available, some not)."""
    monkeypatch.setattr(alphavantage, "configured", lambda: True)
    call_count = {"count": 0}
    def av_series(sid, **k):
        call_count["count"] += 1
        if sid == "UNRATE":
            raise ProviderError("unavailable")
        return _series()
    monkeypatch.setattr(alphavantage, "economic_series", av_series)
    detail = {}
    ingest.ingest_macro_series(detail)
    # Should have 3 ok, 1 failed
    assert len(detail["macro"]["ok"]) == 3
    assert len(detail["macro"]["failed"]) == 1
    assert "UNRATE" in detail["macro"]["failed"]
