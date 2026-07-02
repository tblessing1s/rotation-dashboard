"""Tests for weekly-options detection. Offline: the Schwab client is faked."""
import os
import tempfile
from datetime import date, datetime, timedelta

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-weeklies-test-"))

import weeklies  # noqa: E402


def _reset():
    weeklies._cache.clear()
    weeklies._ov_cache = (0.0, {})


def test_third_friday_known_values():
    assert weeklies._third_friday(2026, 1) == date(2026, 1, 16)
    assert weeklies._third_friday(2026, 7) == date(2026, 7, 17)
    assert weeklies._third_friday(2026, 2) == date(2026, 2, 20)


def _fridays_ahead(n):
    """The next `n` Fridays starting after today."""
    d = datetime.now().date() + timedelta(days=1)
    while d.weekday() != 4:  # 4 = Friday
        d += timedelta(days=1)
    return [d + timedelta(days=7 * i) for i in range(n)]


def _monthlies_in_window():
    """The 3rd-Friday monthlies that fall within the detection lookahead window."""
    today = datetime.now().date()
    out = []
    for off in range(0, 3):
        y = today.year + (today.month - 1 + off) // 12
        m = (today.month - 1 + off) % 12 + 1
        tf = weeklies._third_friday(y, m)
        if today <= tf <= today + timedelta(days=weeklies._LOOKAHEAD_DAYS):
            out.append(tf)
    return out


class _FakeClient:
    def __init__(self, dates):
        self._dates = dates

    def get_option_chain(self, symbol, expiry_date=None, strike_count=50,
                         from_date=None, to_date=None):
        today = datetime.now().date()
        exp_map = {f"{d.isoformat()}:{(d - today).days}": {"100.0": [{}]} for d in self._dates}
        return {"status": "SUCCESS", "callExpDateMap": exp_map}


def test_detect_true_when_a_weekly_expiration_exists(monkeypatch):
    _reset()
    monkeypatch.setattr(weeklies.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(weeklies.data_handler, "client", lambda: _FakeClient(_fridays_ahead(3)))
    # Three consecutive Fridays: at most one is a 3rd-Friday monthly, so a weekly
    # (non-3rd-Friday Friday) is always present.
    assert weeklies._detect("ABC") is True


def test_detect_false_when_only_monthlies(monkeypatch):
    _reset()
    monthlies = _monthlies_in_window()
    assert monthlies, "expected at least one monthly in the lookahead window"
    monkeypatch.setattr(weeklies.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(weeklies.data_handler, "client", lambda: _FakeClient(monthlies))
    assert weeklies._detect("ABC") is False


def test_detect_none_when_schwab_not_configured(monkeypatch):
    _reset()
    monkeypatch.setattr(weeklies.schwab_api, "configured", lambda: False)
    assert weeklies._detect("ABC") is None


def test_detect_none_on_empty_chain(monkeypatch):
    _reset()
    monkeypatch.setattr(weeklies.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(weeklies.data_handler, "client", lambda: _FakeClient([]))
    assert weeklies._detect("ABC") is None


def test_has_weeklies_override_wins(monkeypatch):
    _reset()
    monkeypatch.setattr(weeklies.log, "load_state",
                        lambda: {"metadata": {"weeklies_overrides": {"JBHT": False, "AAPL": True}}})
    # Override is authoritative even though Schwab would say otherwise / is absent.
    monkeypatch.setattr(weeklies.schwab_api, "configured", lambda: True)
    monkeypatch.setattr(weeklies.data_handler, "client", lambda: _FakeClient(_fridays_ahead(3)))
    assert weeklies.has_weeklies("JBHT") is False
    assert weeklies.has_weeklies("AAPL") is True


def test_has_weeklies_caches_detection(monkeypatch):
    _reset()
    monkeypatch.setattr(weeklies.log, "load_state", lambda: {"metadata": {}})
    monkeypatch.setattr(weeklies.schwab_api, "configured", lambda: True)
    calls = {"n": 0}

    def client():
        calls["n"] += 1
        return _FakeClient(_fridays_ahead(3))

    monkeypatch.setattr(weeklies.data_handler, "client", client)
    assert weeklies.has_weeklies("ABC") is True
    assert weeklies.has_weeklies("ABC") is True   # served from cache
    assert calls["n"] == 1


def test_has_weeklies_disabled_returns_none(monkeypatch):
    _reset()
    monkeypatch.setattr(weeklies.log, "load_state", lambda: {"metadata": {}})
    monkeypatch.setenv("SCORECARD_CHECK_WEEKLIES", "0")
    assert weeklies.has_weeklies("ABC") is None
