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
