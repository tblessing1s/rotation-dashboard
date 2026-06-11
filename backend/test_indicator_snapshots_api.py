import db
import app as app_mod


def _payload(symbol, as_of, rs3m=1.0):
    return {
        "asOf": as_of,
        "price": 100,
        "ma21": 99,
        "priceAboveMA21": True,
        "rsi": 55,
        "obv": "rising",
        "accDist": "rising",
        "volRatio": 120,
        "volAccel": 115,
        "mfi": 60,
        "rs3m": rs3m,
        "rs3mMom": 2,
        "rs3mTrend": "up",
    }


def test_indicator_snapshots_are_entry_universe_only_and_datastore_backed(fresh_db, monkeypatch):
    db.save_snapshot("indicators", "XLV", _payload("XLV", "2026-06-10", 2), "2026-06-10")
    db.save_snapshot("indicators", "AAPL", _payload("AAPL", "2026-06-10", 3), "2026-06-10")
    db.save_snapshot("indicators", "NOTWATCHED", _payload("NOTWATCHED", "2026-06-10", 9), "2026-06-10")
    db.save_snapshot("indicators", "XLV", _payload("XLV", "2026-06-09", 1), "2026-06-09")

    monkeypatch.setattr(app_mod.ingest, "is_stale", lambda: False)
    client = app_mod.app.test_client()
    out = client.get("/api/indicator-snapshots?symbols=XLV,NOTWATCHED,AAPL&limit=2").get_json()

    assert out["historyState"] == "ok"
    assert [s["asOf"] for s in out["sessions"]] == ["2026-06-10", "2026-06-09"]
    latest = out["sessions"][0]
    assert set(latest["symbols"]) == {"XLV", "AAPL"}
    assert latest["symbols"]["XLV"]["candidateStrategies"] == ["CFM"]
    assert latest["symbols"]["AAPL"]["sectorProxy"] == "XLK"
    assert latest["symbols"]["XLV"]["indicators"]["rs3m"] == 2
    assert "NOTWATCHED" not in latest["symbols"]


def test_indicator_snapshots_as_of_filter_and_bad_limit(fresh_db, monkeypatch):
    db.save_snapshot("indicators", "XLV", _payload("XLV", "2026-06-09"), "2026-06-09")
    db.save_snapshot("indicators", "XLV", _payload("XLV", "2026-06-10"), "2026-06-10")
    monkeypatch.setattr(app_mod.ingest, "is_stale", lambda: False)
    client = app_mod.app.test_client()

    out = client.get("/api/indicator-snapshots?symbols=XLV&as_of=2026-06-09").get_json()
    assert [s["asOf"] for s in out["sessions"]] == ["2026-06-09"]

    bad = client.get("/api/indicator-snapshots?limit=nope")
    assert bad.status_code == 400
