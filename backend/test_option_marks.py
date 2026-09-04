"""Live short-call marks: OCC symbols, freshness, the (strike, expiration) view."""
from datetime import datetime, timedelta, timezone

import pytest

import config
import option_marks as om

NOW = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh():
    om.reset()
    yield
    om.reset()


def test_symbol_and_option_detection():
    sc = {"strike": 126.0, "expiration": "2026-09-16"}
    sym = om.symbol_for("NVDA", sc)
    assert sym == "NVDA  260916C00126000" and om.is_option_symbol(sym)
    assert not om.is_option_symbol("NVDA") and not om.is_option_symbol("SPY")
    assert om.symbol_for("NVDA", {"strike": 126.0}) is None          # legacy leg, no expiry
    assert om.symbol_for("NVDA", {"strike": "x", "expiration": "2026-09-16"}) is None


def test_short_symbols_covers_open_quotable_legs_only():
    st = {"positions": [
        {"ticker": "NVDA", "status": "active",
         "short_calls": [{"strike": 124, "expiration": "2026-09-09"}, {"strike": 200}]},
        {"ticker": "PG", "status": "closed", "short_calls": [{"strike": 130, "expiration": "2026-09-09"}]},
    ]}
    assert om.short_symbols(st) == {"NVDA  260909C00124000": ("NVDA", 124, "2026-09-09")}


def test_remember_prefers_mark_then_bid_then_last_and_expires(monkeypatch):
    monkeypatch.setattr(config, "OPTION_MARK_MAX_AGE_SECONDS", 600)
    sc = {"strike": 124.0, "expiration": "2026-09-09"}
    sym = om.symbol_for("NVDA", sc)
    assert om.remember(sym, {"mark": 1.35, "bid": 1.30, "last": 1.4}, at=NOW) == 1.35
    assert om.mark_for("NVDA", sc, now=NOW + timedelta(minutes=5)) == 1.35
    assert om.mark_for("NVDA", sc, now=NOW + timedelta(minutes=11)) is None   # stale = absent
    assert om.remember(sym, {"bid": 1.30, "last": 1.4}, at=NOW) == 1.30
    assert om.remember(sym, {"last": 1.4}, at=NOW) == 1.4
    assert om.remember(sym, {}, at=NOW) is None
    assert om.marks_for("NVDA", [sc, {"strike": 200}], now=NOW) == {(124.0, "2026-09-09"): 1.4}
    s = om.status(now=NOW)
    assert s["cached"] == 1 and s["fresh"] == 1 and s["last_at"]
