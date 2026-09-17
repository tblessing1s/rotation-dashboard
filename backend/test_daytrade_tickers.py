"""Day-trade sleeve's own ticker roster (daytrade/tickers.py) — a separate,
independently-editable store from CFM's universe (sector_data.py), seeded
from it exactly once.
"""
import json
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import config          # noqa: E402
import sector_data     # noqa: E402
from daytrade import tickers as daytrade_tickers  # noqa: E402


@pytest.fixture()
def roster(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "UNIVERSE_PATH", str(tmp_path / "universe.json"))
    monkeypatch.setattr(config, "DAYTRADE_TICKERS_PATH", str(tmp_path / "daytrade_universe.json"))
    sector_data._clear_caches()
    yield tmp_path
    sector_data._clear_caches()


def test_seeds_from_cfm_universe_once(roster):
    assert not os.path.exists(config.DAYTRADE_TICKERS_PATH)
    cfm_universe = set(sector_data.all_tickers())
    dt_universe = daytrade_tickers.all_tickers()
    assert os.path.exists(config.DAYTRADE_TICKERS_PATH)
    assert set(dt_universe) == cfm_universe


def test_independent_from_cfm_after_seeding(roster):
    daytrade_tickers.all_tickers()  # seed
    sector_data.add_ticker("TSM", "XLK")  # grows CFM's universe
    # The day-trade roster does not follow CFM's change.
    assert "TSM" not in daytrade_tickers.all_tickers()

    daytrade_tickers.add_ticker("tsm")  # a day-trade-only addition
    assert "TSM" in daytrade_tickers.all_tickers()
    on_disk = json.load(open(config.DAYTRADE_TICKERS_PATH, encoding="utf-8"))
    assert "TSM" in on_disk["tickers"]


def test_add_rejects_blank_and_duplicate(roster):
    daytrade_tickers.all_tickers()
    with pytest.raises(ValueError, match="required"):
        daytrade_tickers.add_ticker("  ")
    existing = daytrade_tickers.all_tickers()[0]
    with pytest.raises(ValueError, match="already"):
        daytrade_tickers.add_ticker(existing)


def test_remove_persists_and_rejects_unknown(roster):
    daytrade_tickers.add_ticker("ZZZZ")
    assert "ZZZZ" in daytrade_tickers.all_tickers()
    daytrade_tickers.remove_ticker("zzzz")
    assert "ZZZZ" not in daytrade_tickers.all_tickers()
    with pytest.raises(ValueError, match="not in"):
        daytrade_tickers.remove_ticker("ZZZZ")


def test_remove_tickers_bulk_skips_unknown(roster):
    daytrade_tickers.add_ticker("ZZZZ")
    daytrade_tickers.add_ticker("YYYY")
    out = daytrade_tickers.remove_tickers(["zzzz", "yyyy", "NOPE"])
    assert set(out["removed"]) == {"ZZZZ", "YYYY"}
    assert "ZZZZ" not in daytrade_tickers.all_tickers()
    assert "YYYY" not in daytrade_tickers.all_tickers()


def test_add_tickers_bulk_skips_blank_and_duplicate(roster):
    daytrade_tickers.add_ticker("ZZZZ")
    out = daytrade_tickers.add_tickers(["zzzz", "yyyy", "  ", "yyyy", "wwww"])
    assert out["added"] == ["YYYY", "WWWW"]
    assert out["skipped"] == ["ZZZZ"]
    tickers = daytrade_tickers.all_tickers()
    assert {"ZZZZ", "YYYY", "WWWW"} <= set(tickers)


def test_add_tickers_bulk_persists(roster):
    daytrade_tickers.all_tickers()  # seed
    daytrade_tickers.add_tickers(["QQQQ", "RRRR"])
    on_disk = json.load(open(config.DAYTRADE_TICKERS_PATH, encoding="utf-8"))
    assert "QQQQ" in on_disk["tickers"] and "RRRR" in on_disk["tickers"]


def test_universe_screen_defaults_to_daytrade_roster(roster, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from daytrade import universe

    # An empty list is treated as "no store yet" (self-heals by reseeding —
    # see test_seeds_from_cfm_universe_once), so assert the wiring by
    # replacing the roster wholesale rather than emptying it via remove_*.
    monkeypatch.setattr(daytrade_tickers, "all_tickers", lambda: ["ONLYME"])

    seen = []
    monkeypatch.setattr(universe, "_evaluate", lambda t: seen.append(t) or
                        {"symbol": t, "qualified": False, "reason": "no data"})
    universe.screen(now=datetime(2026, 9, 14, tzinfo=ZoneInfo("America/New_York")))
    assert seen == ["ONLYME"]
