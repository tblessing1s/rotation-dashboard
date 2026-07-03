"""Universe store tests — volume-backed JSON, seeded from the repo file,
runtime add/remove with validation and persistence.
"""
import json
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config          # noqa: E402
import sector_data     # noqa: E402


@pytest.fixture()
def universe(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "UNIVERSE_PATH", str(tmp_path / "universe.json"))
    sector_data._clear_caches()
    yield tmp_path
    sector_data._clear_caches()


def test_seeds_from_repo_file_on_first_load(universe):
    assert not os.path.exists(config.UNIVERSE_PATH)
    tickers = sector_data.all_tickers()
    # Seed written to the volume, and it contains the fixed universe.
    assert os.path.exists(config.UNIVERSE_PATH)
    assert len(sector_data.sector_etfs()) == 12   # 11 SPDR sectors + SPY (Broad Market)
    assert "NVDA" in tickers and "XLK" in tickers
    # Curated CFM-fit ETFs are seeded with sensible sector homes.
    assert sector_data.sector_for("SMH") == "XLK" and sector_data.sector_for("GDX") == "XLB"
    assert sector_data.sector_for("QQQ") == "SPY" and "IWM" in tickers
    # The stale tickers we removed are gone; the renames are present.
    for dead in ("Q", "FISV", "MRSH", "FDXF", "ECHO"):
        assert dead not in tickers
    assert "FI" in tickers and "MMC" in tickers


def test_add_and_remove_persist(universe):
    sector_data.all_tickers()  # seed
    sector_data.add_ticker("tsm", "xlk")
    assert sector_data.sector_for("TSM") == "XLK"
    # Persisted to disk (survives a cache clear / fresh load).
    sector_data._clear_caches()
    assert "TSM" in sector_data.all_tickers()
    on_disk = json.load(open(config.UNIVERSE_PATH, encoding="utf-8"))
    xlk = next(s for s in on_disk["sectors"] if s["etf"] == "XLK")
    assert "TSM" in xlk["tickers"]

    sector_data.remove_ticker("TSM")
    sector_data._clear_caches()
    assert sector_data.sector_for("TSM") is None


def test_validation(universe):
    sector_data.all_tickers()
    with pytest.raises(ValueError, match="already in the universe"):
        sector_data.add_ticker("NVDA", "XLK")
    with pytest.raises(ValueError, match="unknown sector"):
        sector_data.add_ticker("FOO", "ZZZ")
    with pytest.raises(ValueError, match="sector ETF"):
        sector_data.remove_ticker("XLK")
    with pytest.raises(ValueError, match="not in the universe"):
        sector_data.remove_ticker("NOPE")


def test_self_heals_when_store_deleted(universe):
    sector_data.all_tickers()
    os.remove(config.UNIVERSE_PATH)      # lose the volume copy
    sector_data._clear_caches()
    assert len(sector_data.all_tickers()) > 100   # re-seeds from the repo file
    assert os.path.exists(config.UNIVERSE_PATH)


def test_bulk_remove_skips_etfs_and_absent(universe):
    sector_data.all_tickers()
    sector_data.add_ticker("DEAD1", "XLK")
    sector_data.add_ticker("DEAD2", "XLE")
    r = sector_data.remove_tickers(["DEAD1", "DEAD2", "XLK", "NOPE"])
    assert set(r["removed"]) == {"DEAD1", "DEAD2"}
    reasons = {s["ticker"]: s["reason"] for s in r["skipped"]}
    assert reasons["XLK"] == "sector ETF" and reasons["NOPE"] == "not in universe"
    sector_data._clear_caches()
    assert sector_data.sector_for("DEAD1") is None
    assert sector_data.sector_for("XLK") == "XLK"   # ETF preserved


def test_reseed_discards_runtime_edits(universe):
    sector_data.all_tickers()
    sector_data.add_ticker("TSM", "XLK")
    assert "TSM" in sector_data.all_tickers()
    sector_data.reseed_from_file()
    assert "TSM" not in sector_data.all_tickers()   # back to the baked-in list
