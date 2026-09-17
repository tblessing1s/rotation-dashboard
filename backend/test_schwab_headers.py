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


def test_option_chain_400_wrapped_404_raises_the_retryable_subclass(monkeypatch):
    # Schwab's "no chain for this symbol" shape: an outer 400 carrying an inner
    # errors[].status of 404. Confirmed live (SPCX) that this does NOT mean "no
    # options at all" — Schwab's own site showed both calls and puts — so the
    # message points at the requested date window, and the error is the
    # SchwabChainNotFoundError subclass a caller can retry narrower on.
    body = '{"errors":[{"id":"44d6b68c-48e6-4037-aa09-4949495f6b88","status":"404","title":"Not Found"}]}'
    monkeypatch.setattr(schwab_api.requests, "get", lambda *a, **k: _Resp(400, text=body))
    with pytest.raises(schwab_api.SchwabChainNotFoundError, match="404 Not Found for SPCX"):
        _client(monkeypatch).get_option_chain("SPCX", from_date="2026-09-17", to_date="2027-06-14")


def test_option_chain_other_400_keeps_raw_body(monkeypatch):
    # A 400 that isn't the wrapped-404 shape stays the generic, raw-body error.
    body = '{"errors":[{"id":"x","status":"400","title":"Bad Request"}]}'
    monkeypatch.setattr(schwab_api.requests, "get", lambda *a, **k: _Resp(400, text=body))
    with pytest.raises(schwab_api.SchwabError, match="HTTP 400"):
        _client(monkeypatch).get_option_chain("XLK")
