"""Day-trade what-if replay: parameter overrides change the outcome, write
nothing, and never leak into config or the account's real logs."""
from __future__ import annotations

import pytest

import config
from daytrade import params, store, whatif

DAY = "2026-09-14"


def _bar(hm, o, h, l, c, v):
    return {"symbol": "ABC", "date": DAY, "datetime": f"{DAY}T{hm}:00-04:00",
            "open": o, "high": h, "low": l, "close": c, "volume": v}


@pytest.fixture
def day_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", str(tmp_path / "daytrade_log"))
    monkeypatch.setattr(config, "DAYTRADE_MAX_POSITION_PCT", 20.0)
    monkeypatch.setattr(config, "DAYTRADE_MAX_DAILY_DEPLOY_PCT", 1000.0)
    store.save_screen({"schema_version": 1, "date": DAY, "computed_at": "x", "screened": [],
                       "picks": [{"symbol": "ABC", "prior_day_high": 100, "prior_day_low": 90, "atr14": 4.0}]})
    # setup, trigger, then a +1R tag and a fade back through breakeven
    store.append_bars(DAY, [
        _bar("09:30", 95, 96, 94, 95, 100_000),
        _bar("09:35", 100.5, 101, 100.2, 101, 200_000),
        _bar("09:40", 101, 102.2, 100.9, 102, 150_000),
        _bar("09:45", 102, 102.1, 99, 99.5, 150_000),
    ])
    return tmp_path


def test_override_changes_result_and_baseline_is_untouched(day_store):
    out = whatif.compare(DAY, {"DAYTRADE_STOP_ATR_DIVISOR": 2.0})  # 2R-wide stop
    assert out["ran"]
    assert out["baseline"]["summary"]["trades"] == 1
    assert out["baseline"]["summary"] != out["modified"]["summary"]
    assert config.DAYTRADE_STOP_ATR_DIVISOR == 4.0
    assert params.P.DAYTRADE_STOP_ATR_DIVISOR == 4.0


def test_writes_nothing(day_store):
    whatif.compare(DAY, {"DAYTRADE_MAX_TRADES_PER_DAY": 1})
    assert store.load_signals(DAY, "primary") == []
    assert store.load_trades(DAY, "primary") == {}


def test_no_screen_for_day(day_store):
    assert whatif.compare("2026-01-02", {}) == {"date": "2026-01-02", "ran": False}


def test_rejects_non_replayable_and_bad_values():
    with pytest.raises(ValueError):
        params.coerce_overrides({"DAYTRADE_MIN_PRICE": 5})
    with pytest.raises(ValueError):
        params.coerce_overrides({"DAYTRADE_RISK_PCT": "abc"})
    with pytest.raises(ValueError):
        params.coerce_overrides({"DAYTRADE_STOP_ATR_DIVISOR": 0})


def test_catalog_covers_every_config_constant():
    keys = {c["key"] for c in params.catalog()}
    live = {k for k in dir(config) if k.startswith("DAYTRADE_") and k.isupper()
            and not k.endswith("_PATH")}
    assert live <= keys, live - keys
