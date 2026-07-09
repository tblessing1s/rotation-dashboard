"""Schwab request hygiene: every market-data/trader call must carry a browser
User-Agent (Schwab's Akamai edge 403s a default requests UA from a cloud host),
and an edge 403 must surface a clean, actionable message rather than raw HTML.
"""
import pytest

import schwab_api


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload if payload is not None else {}
        self.headers = {}

    def json(self):
        return self._payload


def _client(monkeypatch):
    c = schwab_api.SchwabClient()
    monkeypatch.setattr(c, "_token", lambda: "tok")  # skip the real OAuth refresh
    return c


def test_option_chain_sends_browser_user_agent(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["headers"] = headers or {}
        return _Resp(200, payload={"status": "SUCCESS"})

    monkeypatch.setattr(schwab_api.requests, "get", fake_get)
    _client(monkeypatch).get_option_chain("XLK")
    assert captured["headers"].get("User-Agent") == schwab_api.USER_AGENT
    assert "python-requests" not in captured["headers"].get("User-Agent", "")


def test_quotes_send_browser_user_agent(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["headers"] = headers or {}
        return _Resp(200, payload={})

    monkeypatch.setattr(schwab_api.requests, "get", fake_get)
    _client(monkeypatch).get_quotes(["XLK"])
    assert captured["headers"].get("User-Agent") == schwab_api.USER_AGENT


def test_option_chain_akamai_403_gives_clean_message(monkeypatch):
    body = "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>Access Denied</BODY></HTML>"
    monkeypatch.setattr(schwab_api.requests, "get", lambda *a, **k: _Resp(403, text=body))
    with pytest.raises(schwab_api.SchwabError, match="Akamai edge"):
        _client(monkeypatch).get_option_chain("XLK")


def test_cancel_retries_past_a_transient_429(monkeypatch):
    # A cancel is safety-critical: a 429 from the fill-poll burst must not abandon
    # a working order. It should back off and retry until the DELETE lands.
    calls = {"n": 0}
    sleeps = []

    def fake_delete(url, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(429) if calls["n"] < 3 else _Resp(204)

    monkeypatch.setattr(schwab_api.requests, "delete", fake_delete)
    monkeypatch.setattr(schwab_api.time, "sleep", lambda s: sleeps.append(s))

    out = _client(monkeypatch).cancel_order("acct", "OID")
    assert out == {"orderId": "OID", "canceled": True}
    assert calls["n"] == 3            # two 429s, then a 204 success
    assert sleeps == [0.5, 1.0]       # exponential backoff between the retries


def test_cancel_honors_retry_after_header(monkeypatch):
    calls = {"n": 0}

    def fake_delete(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            r = _Resp(429)
            r.headers = {"Retry-After": "2"}
            return r
        return _Resp(204)

    seen = []
    monkeypatch.setattr(schwab_api.requests, "delete", fake_delete)
    monkeypatch.setattr(schwab_api.time, "sleep", lambda s: seen.append(s))
    _client(monkeypatch).cancel_order("acct", "OID")
    assert seen == [2.0]  # server-supplied Retry-After wins over the exponential default


def test_cancel_surfaces_error_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(schwab_api.requests, "delete", lambda *a, **k: _Resp(429, text="Too many requests"))
    monkeypatch.setattr(schwab_api.time, "sleep", lambda s: None)
    with pytest.raises(schwab_api.SchwabError, match="HTTP 429"):
        _client(monkeypatch).cancel_order("acct", "OID")
