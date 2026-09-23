"""schwab_api.get_intraday_bars must bound its request to TODAY explicitly
via startDate/endDate, not periodType=day + period=1 — see the method's own
docstring for why: that combination leaves "which day" to Schwab's own
judgment, observed in production to silently hand back a prior trading
day's candles instead of today's still-in-progress session, under no error
at all (daytrade/bars.py's own date check is what finally surfaced it).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import schwab_api

ET = ZoneInfo("America/New_York")


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.text = ""
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _client(monkeypatch):
    c = schwab_api.SchwabClient()
    monkeypatch.setattr(c, "_token", lambda: "tok")  # skip the real OAuth refresh
    return c


def test_intraday_bars_bounds_the_request_to_todays_et_calendar_day(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params or {}
        now_ms = int(datetime.now(ET).timestamp() * 1000)
        candle = {"datetime": now_ms, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        return _Resp(200, payload={"candles": [candle]})

    monkeypatch.setattr(schwab_api.requests, "get", fake_get)
    _client(monkeypatch).get_intraday_bars("XLK", minutes=5)

    params = captured["params"]
    assert "period" not in params  # no ambiguous "1 day" left to Schwab's own judgment
    assert params["periodType"] == "day"
    assert params["frequencyType"] == "minute"
    assert params["frequency"] == 5

    midnight_et = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    assert params["startDate"] == int(midnight_et.timestamp() * 1000)
    now_ms = int(datetime.now(ET).timestamp() * 1000)
    assert params["startDate"] <= params["endDate"] <= now_ms


def test_intraday_bars_index_stays_et_aware(monkeypatch):
    now_ms = int(datetime.now(ET).timestamp() * 1000)
    candle = {"datetime": now_ms, "open": 1, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100}

    def fake_get(url, headers=None, params=None, timeout=None):
        return _Resp(200, payload={"candles": [candle]})

    monkeypatch.setattr(schwab_api.requests, "get", fake_get)
    df = _client(monkeypatch).get_intraday_bars("XLK", minutes=5)

    assert str(df.index.tz) == "America/New_York"
    assert df.iloc[-1]["Close"] == 1.05
